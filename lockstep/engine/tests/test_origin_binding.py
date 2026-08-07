"""Task 8: origin binding — a fractal child run is drivable ONLY by the
session the engine spawned for it (the process holding the matching
LOCKSTEP_CHILD_RUN + LOCKSTEP_CHILD_NONCE pair). Without this, a worker
simply calls scenario_done on the child's run_id (readable via
scenario_status/list_runs) and walks the "independent reviewer" to any
verdict it wants. Also pins nonce redaction: the credential never rides
out through list_runs."""

from pathlib import Path

import pytest

from lockstep_mcp import server
from lockstep_mcp.runs import RunIndex

GOOD = Path(__file__).parent / "fixtures" / "recipes" / "good"    # absolute, not cwd-dependent (m8.4)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(GOOD))
    monkeypatch.delenv("LOCKSTEP_CHILD_RUN", raising=False)
    monkeypatch.delenv("LOCKSTEP_CHILD_NONCE", raising=False)
    server._reset_engine()
    return RunIndex(tmp_path / "state")


def test_worker_cannot_drive_a_child_run(tmp_path, monkeypatch):
    idx = _setup(tmp_path, monkeypatch)
    parent = idx.create("parent", str(tmp_path))
    child = idx.create("child-review", str(tmp_path), parent_run=parent.run_id, nonce="secret")
    for call in (lambda: server.scenario_done(child.run_id, "review", {"x": 1}),
                 lambda: server.scenario_escalate(child.run_id, "mine now"),
                 lambda: server.scenario_abort(child.run_id)):
        with pytest.raises(Exception) as e:
            call()
        assert "credential" in str(e.value).lower()


def test_wrong_nonce_is_refused(tmp_path, monkeypatch):
    idx = _setup(tmp_path, monkeypatch)
    parent = idx.create("parent", str(tmp_path))
    child = idx.create("child-review", str(tmp_path), parent_run=parent.run_id, nonce="secret")
    monkeypatch.setenv("LOCKSTEP_CHILD_RUN", child.run_id)
    monkeypatch.setenv("LOCKSTEP_CHILD_NONCE", "guessed")
    with pytest.raises(Exception):
        server.scenario_abort(child.run_id)


def test_parented_record_with_no_nonce_refuses_even_with_empty_env(tmp_path, monkeypatch):
    # I8.3's fail-open trap: compare_digest("", "") MATCHES — a record with
    # parent_run set but nonce None must refuse UNCONDITIONALLY.
    idx = _setup(tmp_path, monkeypatch)
    parent = idx.create("parent", str(tmp_path))
    child = idx.create("child-review", str(tmp_path), parent_run=parent.run_id, nonce=None)
    monkeypatch.setenv("LOCKSTEP_CHILD_RUN", child.run_id)
    monkeypatch.setenv("LOCKSTEP_CHILD_NONCE", "")
    with pytest.raises(Exception) as e:
        server.scenario_abort(child.run_id)
    assert "credential" in str(e.value).lower()


def test_credentialed_child_session_may_drive_its_own_run(tmp_path, monkeypatch):
    idx = _setup(tmp_path, monkeypatch)
    parent = idx.create("parent", str(tmp_path))
    child = idx.create("child-review", str(tmp_path), parent_run=parent.run_id, nonce="secret")
    monkeypatch.setenv("LOCKSTEP_CHILD_RUN", child.run_id)
    monkeypatch.setenv("LOCKSTEP_CHILD_NONCE", "secret")
    assert server.scenario_abort(child.run_id)["status"] == "aborted"


def test_parentless_runs_are_unaffected(tmp_path, monkeypatch):
    idx = _setup(tmp_path, monkeypatch)
    solo = idx.create("solo", str(tmp_path))
    assert server.scenario_abort(solo.run_id)["status"] == "aborted"


def test_list_runs_never_exposes_the_nonce(tmp_path, monkeypatch):
    idx = _setup(tmp_path, monkeypatch)
    parent = idx.create("parent", str(tmp_path))
    idx.create("child-review", str(tmp_path), parent_run=parent.run_id, nonce="secret")
    rows = server.list_runs()
    assert len(rows) == 2
    for d in rows:                                          # exact key set: nothing secret rides along
        assert set(d) == {"run_id", "recipe", "project", "status", "step",
                          "brief", "started", "updated", "parent_run"}
