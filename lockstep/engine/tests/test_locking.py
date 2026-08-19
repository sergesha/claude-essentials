import json, os, time
from pathlib import Path
import pytest
from lockstep.runtime.locking import file_lock, LockTimeout

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

def test_four_plus_way_stale_break_mutual_exclusion(tmp_path):
    # Required property test: N>=4 threads racing to break the SAME
    # pre-created stale lock must admit at most one holder at a time.
    # Each holder appends enter/exit markers under the lock; any depth>1
    # in the event stream proves a double-admit. Repeated rounds because
    # the failure is interleaving-dependent.
    import threading

    target = tmp_path / "runs.json"
    lock = tmp_path / "runs.json.lock"
    nthreads = 5
    rounds = 80

    for _ in range(rounds):
        lock.write_text(json.dumps({"pid": 999999, "ts": time.time() - 3600}))
        old = time.time() - 3600
        os.utime(lock, (old, old))

        events = []
        events_lock = threading.Lock()
        barrier = threading.Barrier(nthreads)
        errors = []

        def worker():
            try:
                barrier.wait(timeout=5.0)
                with file_lock(target, timeout=10.0, stale_after=60.0):
                    with events_lock:
                        events.append("enter")
                    time.sleep(0.001)
                    with events_lock:
                        events.append("exit")
            except Exception as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        ts = [threading.Thread(target=worker) for _ in range(nthreads)]
        [t.start() for t in ts]
        [t.join(timeout=30.0) for t in ts]

        assert not errors, f"workers failed: {errors}"
        assert len(events) == nthreads * 2, f"livelock/lost workers: {events}"
        depth = 0
        for kind in events:
            depth += 1 if kind == "enter" else -1
            assert depth <= 1, f"overlap detected: {events}"

def test_crashed_holder_lock_is_eventually_acquirable(tmp_path):
    # Crash recovery: a lock left behind with a back-dated mtime (holder
    # died) must be acquirable well within a reasonable timeout.
    target = tmp_path / "runs.json"
    lock = tmp_path / "runs.json.lock"
    lock.write_text("half-written garbage from a dead process")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    t0 = time.monotonic()
    with file_lock(target, timeout=5.0, stale_after=60.0):
        pass
    assert time.monotonic() - t0 < 2.0

def test_crashed_breaker_orphaned_break_mutex_recovers(tmp_path):
    # Double-crash recovery: holder died (stale lock) AND a breaker died
    # mid-session (stale orphaned break-mutex). Both must be recovered;
    # the lock must still be acquirable.
    target = tmp_path / "runs.json"
    lock = tmp_path / "runs.json.lock"
    brk = tmp_path / "runs.json.lock.break"
    old = time.time() - 3600
    lock.write_text("dead holder")
    os.utime(lock, (old, old))
    brk.write_text(json.dumps({"token": "dead:1", "pid": 999999}))
    os.utime(brk, (old, old))
    with file_lock(target, timeout=5.0, stale_after=60.0):
        pass
    assert not lock.exists()
    assert not brk.exists()

def test_live_hold_within_stale_after_is_not_stolen(tmp_path):
    # A legitimate hold shorter than stale_after must never be broken,
    # however hard a waiter tries: the holder's lock file must survive
    # the whole hold with its owner unchanged.
    import threading

    target = tmp_path / "runs.json"
    lock = tmp_path / "runs.json.lock"
    holder_in = threading.Event()
    state = {}

    def holder():
        with file_lock(target, timeout=5.0, stale_after=30.0):
            owner_at_entry = json.loads(lock.read_text())["owner"]
            holder_in.set()
            time.sleep(0.6)  # long hold, but well within stale_after
            state["owner_stable"] = json.loads(lock.read_text())["owner"] == owner_at_entry

    th = threading.Thread(target=holder)
    th.start()
    assert holder_in.wait(timeout=5.0)
    with pytest.raises(LockTimeout):
        with file_lock(target, timeout=0.3, stale_after=30.0):
            pass
    th.join(timeout=5.0)
    assert state.get("owner_stable") is True
    # and once released, it is acquirable normally
    with file_lock(target, timeout=1.0, stale_after=30.0):
        pass

def test_no_break_mutex_residue(tmp_path):
    # Normal operation must not leave the break-mutex sidecar behind.
    target = tmp_path / "runs.json"
    for _ in range(5):
        with file_lock(target):
            pass
    assert not (tmp_path / "runs.json.lock").exists()
    assert not (tmp_path / "runs.json.lock.break").exists()

def test_breaker_session_does_not_unlink_foreign_session_in_its_empty_window(tmp_path, monkeypatch):
    # Deterministic regression for the level-1 empty-window fix (adjudication
    # hardening 2): if OUR payload write succeeded (no exception) but, by
    # the time our `finally` looks at the mutex file, it reads empty because
    # it was vacated and a THIRD PARTY's new session is caught in its own
    # empty window (between its O_EXCL and its own json.dump), our session
    # must not conclude "empty means ours" and unlink that live foreign
    # session's mutex. Force the exact interleaving deterministically —
    # no repetition, no timing — by making the payload write itself perform
    # the vacate-and-foreign-recreate before returning successfully.
    import lockstep.runtime.locking as locking

    brk = tmp_path / "runs.json.lock.break"

    def fake_dump(obj, fh, *a, **k):
        # our own write "succeeds" (returns normally, no exception) ...
        fh.close()
        # ... but a foreign process vacated our mutex and recreated it for
        # its own session, still in ITS empty window, before we return.
        os.unlink(brk)
        os.close(os.open(brk, os.O_CREAT | os.O_EXCL | os.O_WRONLY))

    monkeypatch.setattr(json, "dump", fake_dump)
    with locking._breaker_session(brk, stale_after=60.0) as in_session:
        assert in_session is True

    # the foreign session's (still-empty) mutex must survive our `finally`
    assert brk.exists()
