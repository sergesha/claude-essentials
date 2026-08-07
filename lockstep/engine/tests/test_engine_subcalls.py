import time
from pathlib import Path

import pytest

from lockstep_mcp.engine import Engine, LockstepError
from _subcall_helpers import FIX, make_engine, pass_plan


def _wait_status(e, run_id, want, deadline_s=10.0):
    deadline = time.time() + deadline_s
    st = e.status(run_id)
    while st.get("status") != want and time.time() < deadline:
        time.sleep(0.05)
        st = e.status(run_id)
    return st


def test_subcall_runs_and_completes(tmp_path, monkeypatch):
    e, proj = make_engine(tmp_path, monkeypatch)
    r = e.start("subcall-one-shot", vars={}, project=str(proj))
    out = pass_plan(e, proj, r)
    assert out["passed"] is True and out["step"] == "_subcall"
    assert out["subcall"]["node"] == "review"              # the worker is told a subcall started
    assert _wait_status(e, r["run_id"], "done")["status"] == "done"


def test_done_refused_while_subcall_in_flight(tmp_path, monkeypatch):
    e, proj = make_engine(tmp_path, monkeypatch, sleep=5.0)
    r = e.start("subcall-one-shot", vars={}, project=str(proj))
    pass_plan(e, proj, r)
    out = e.done(r["run_id"], "review", {"anything": 1})
    assert out["accepted"] is False and "subcall in progress" in out["errors"][0]
    st = e.status(r["run_id"])
    assert st["step"] == "_subcall"
    assert st["subcall"]["node"] == "review" and st["subcall"]["runner"] == "claude"
    assert isinstance(st["subcall"]["running_minutes"], int)   # started_at persisted


def test_abort_and_escalate_refused_while_parked(tmp_path, monkeypatch):
    e, proj = make_engine(tmp_path, monkeypatch, sleep=5.0)
    r = e.start("subcall-one-shot", vars={}, project=str(proj))
    pass_plan(e, proj, r)
    with pytest.raises(LockstepError) as exc:
        e.abort(r["run_id"])
    assert "subcall in progress" in str(exc.value)
    with pytest.raises(LockstepError) as exc:
        e.escalate(r["run_id"], "mine now")
    assert "subcall in progress" in str(exc.value)


def test_refusal_lifts_when_the_subcall_resolves(tmp_path, monkeypatch):
    # The escape hatch, observed end-to-end: the runner FAILS (rc=3),
    # the next abort() entry auto-polls, sees the error envelope, the run
    # escalates — and abort's error is now the TERMINAL message, never the
    # subcall refusal. The refusal is not sticky.
    e, proj = make_engine(tmp_path, monkeypatch, mode="fail")
    r = e.start("subcall-one-shot", vars={}, project=str(proj))
    pass_plan(e, proj, r)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            e.abort(r["run_id"])
        except LockstepError as exc:
            if "subcall in progress" in str(exc):          # runner not reaped yet — retry
                time.sleep(0.05)
                continue
            assert "escalated" in str(exc)                 # terminal error, not the refusal
            break
        else:
            pytest.fail("abort succeeded — the error subcall should have escalated the run")
    assert e.status(r["run_id"])["status"] == "escalated"


def test_missing_runner_refused_before_resume(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCKSTEP_RUNNER", raising=False)
    state = tmp_path / "state"
    state.mkdir(parents=True)                              # no runners.yaml at all
    proj = tmp_path / "proj"
    proj.mkdir()
    e = Engine(state_dir=state, recipes_dir=FIX / "good", memory_only=False)
    r = e.start("subcall-one-shot", vars={}, project=str(proj))
    out = pass_plan(e, proj, r)
    assert out["accepted"] is True and out["passed"] is False and out.get("error") is True
    assert any("runner" in reason for reason in out["reasons"])
    assert e.status(r["run_id"])["step"] == "plan"         # v1 no-resume path: loop budget untouched


def test_subcall_budget_refused_before_resume(tmp_path, monkeypatch):
    e, proj = make_engine(tmp_path, monkeypatch, max_subcalls=0)
    r = e.start("subcall-one-shot", vars={}, project=str(proj))
    out = pass_plan(e, proj, r)
    assert out.get("error") is True
    assert any("budget" in reason for reason in out["reasons"])
    assert e.status(r["run_id"])["step"] == "plan"


def test_pass_at_loop_cap_escalates_never_spawns(tmp_path, monkeypatch):
    # m5.12: _predict_spawn must replicate the pre-execution loop guard —
    # a PASS arriving AT the cap routes to escalate, never the spawn.
    # max_subcalls=0 makes a mispredicted spawn observable: prediction that
    # ignores the cap would refuse on budget instead of escalating.
    e, proj = make_engine(tmp_path, monkeypatch, max_subcalls=0)
    r = e.start("subcall-one-shot", vars={}, project=str(proj))
    for _ in range(2):                                     # burn the loop cap (plan.md absent)
        out = e.done(r["run_id"], "plan", {"plan_path": ".lockstep/plan.md"})
        assert out["accepted"] is True and out["passed"] is False and not out.get("error")
    out = pass_plan(e, proj, r)                            # pass at the cap
    assert out.get("error") is not True                    # never the budget refusal
    assert out.get("escalated") is True and out["passed"] is True
    assert e.status(r["run_id"])["status"] == "escalated"


def test_envelope_visible_via_peek_state(tmp_path, monkeypatch):
    # This proves the envelope written by the poll node IS in graph state
    # after completion. The engine->run_checks `_state` WIRING test lives in
    # Task 7 (test_verify_step_hash_check_wired_through_engine) — it needs
    # the fractal fixture (I6.2).
    e, proj = make_engine(tmp_path, monkeypatch)
    r = e.start("subcall-one-shot", vars={}, project=str(proj))
    pass_plan(e, proj, r)
    assert _wait_status(e, r["run_id"], "done")["status"] == "done"
    env = e._peek_state(r["run_id"])["_subcall_envelope"]
    assert env["session_id"] == "fake-session-1" and env["exit_code"] == 0
    assert env["artifact_hashes"] == {}                    # one-shot: the envelope IS the artifact (I6.3)
