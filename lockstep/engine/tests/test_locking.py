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
