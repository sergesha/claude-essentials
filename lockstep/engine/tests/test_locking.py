import json, os, time
from pathlib import Path
import pytest
from lockstep_mcp.locking import file_lock, LockTimeout

def test_lock_is_sidecar_and_released(tmp_path):
    target = tmp_path / "runs.json"
    target.write_text("{}")
    with file_lock(target):
        assert (tmp_path / "runs.json.lock").exists()
    assert not (tmp_path / "runs.json.lock").exists()

def test_second_acquire_times_out_while_held(tmp_path):
    target = tmp_path / "runs.json"
    with file_lock(target):
        with pytest.raises(LockTimeout):
            with file_lock(target, timeout=0.3):
                pass

def test_serialized_updates_lose_nothing(tmp_path):
    # two "processes" (threads) doing read-modify-write under the lock
    import threading
    target = tmp_path / "counter.json"
    target.write_text(json.dumps({"n": 0}))
    def bump():
        for _ in range(50):
            with file_lock(target):
                data = json.loads(target.read_text())
                data["n"] += 1
                tmp = target.with_suffix(".tmp")
                tmp.write_text(json.dumps(data))
                os.replace(tmp, target)
    ts = [threading.Thread(target=bump) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert json.loads(target.read_text())["n"] == 200

def test_stale_lock_is_broken(tmp_path):
    target = tmp_path / "runs.json"
    lock = tmp_path / "runs.json.lock"
    lock.write_text(json.dumps({"pid": 999999, "ts": time.time() - 3600}))
    # backdate mtime too: staleness must be judged by age, not parsed content
    old = time.time() - 3600
    os.utime(lock, (old, old))
    with file_lock(target, timeout=1.0, stale_after=60.0):
        pass  # must not raise: the lock is older than stale_after

def test_empty_lock_window_is_not_stolen(tmp_path):
    # Regression: a lock file that exists but is still EMPTY (the window
    # between O_CREAT|O_EXCL and the json.dump that fills it) must NOT be
    # treated as stale just because it fails to parse. Only age may decide
    # staleness. A fresh empty lock is young -> a waiter must keep waiting
    # and time out, never steal it.
    target = tmp_path / "runs.json"
    lock = tmp_path / "runs.json.lock"
    lock.write_text("")  # simulate the just-created, not-yet-written lock
    with pytest.raises(LockTimeout):
        with file_lock(target, timeout=0.3):
            pass
    # the empty lock must still be there: it was never stolen
    assert lock.exists()

def test_lock_released_on_exception(tmp_path):
    target = tmp_path / "runs.json"
    with pytest.raises(ValueError):
        with file_lock(target):
            raise ValueError("boom")
    assert not (tmp_path / "runs.json.lock").exists()

def test_concurrent_stale_break_is_exclusive(tmp_path):
    # Regression: two waiters racing to break the SAME stale lock must not
    # both end up inside the critical section. Pre-create a stale sidecar,
    # then let two threads race the acquire loop simultaneously (a barrier
    # synchronizes their start to maximize the chance of hitting the
    # unlink-after-recreate window); each records enter/exit under a
    # shared, lock-protected event log — any overlap proves a double-admit.
    # Repeated over many rounds: the race is timing-dependent (a single
    # round rarely lands in the exact narrow window against the unfixed
    # `_break_stale`, which did a naive `os.unlink(lock)`), so one round is
    # not enough to reliably surface a regression.
    import threading

    target = tmp_path / "runs.json"
    lock = tmp_path / "runs.json.lock"
    nthreads = 2
    rounds = 300

    for _ in range(rounds):
        lock.write_text(json.dumps({"pid": 999999, "ts": time.time() - 3600}))
        old = time.time() - 3600
        os.utime(lock, (old, old))

        events = []
        events_lock = threading.Lock()
        barrier = threading.Barrier(nthreads)

        def worker():
            barrier.wait(timeout=5.0)
            with file_lock(target, timeout=3.0, stale_after=60.0):
                with events_lock:
                    events.append(("enter", threading.get_ident()))
                time.sleep(0.01)
                with events_lock:
                    events.append(("exit", threading.get_ident()))

        ts = [threading.Thread(target=worker) for _ in range(nthreads)]
        [t.start() for t in ts]
        [t.join(timeout=5.0) for t in ts]

        assert len(events) == nthreads * 2
        # depth must never exceed 1: two "enter"s without an intervening
        # "exit" means two threads were inside the critical section at once
        depth = 0
        for kind, _ident in events:
            depth += 1 if kind == "enter" else -1
            assert depth <= 1, f"overlap detected: {events}"

def test_stale_break_survives_original_holder_release(tmp_path):
    # Regression: if a legitimate hold outlives stale_after, a waiter
    # breaks it and acquires its own lock. The ORIGINAL holder's `finally`
    # must not then delete the new holder's live lock file.
    import threading

    target = tmp_path / "runs.json"
    lock = tmp_path / "runs.json.lock"

    holder_entered = threading.Event()
    release_holder = threading.Event()
    breaker_holding = threading.Event()
    state = {}

    def holder():
        with file_lock(target, timeout=5.0, stale_after=0.2):
            holder_entered.set()
            # simulate a hold that outlives stale_after
            release_holder.wait(timeout=5.0)
        state["holder_finally_ran"] = True

    def breaker():
        assert holder_entered.wait(timeout=5.0)
        time.sleep(0.3)  # let the holder's lock age past stale_after
        with file_lock(target, timeout=5.0, stale_after=0.2):
            breaker_holding.set()
            release_holder.set()  # let the original holder's finally run now
            time.sleep(0.2)  # give it time to execute
            state["lock_exists_after_holder_release"] = lock.exists()

    th = threading.Thread(target=holder)
    tb = threading.Thread(target=breaker)
    th.start(); tb.start()
    th.join(timeout=5.0); tb.join(timeout=5.0)

    assert breaker_holding.is_set()
    assert state.get("holder_finally_ran") is True
    assert state.get("lock_exists_after_holder_release") is True
