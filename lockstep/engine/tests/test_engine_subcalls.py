import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from lockstep_mcp import subcalls
from lockstep_mcp.engine import Engine, LockstepError
from _subcall_helpers import FAKE, FIX, make_engine, pass_plan


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


# --- Task 7: fractal child runs ----------------------------------------------


def test_fractal_child_run_is_created_exactly_once(tmp_path, monkeypatch):
    # C7.1: polling repeatedly must NOT mint children — the id is persisted
    # in the spawn workdir and read back.
    e, proj = make_engine(tmp_path, monkeypatch, sleep=5.0)
    r = e.start("subcall-fractal", vars={}, project=str(proj))
    pass_plan(e, proj, r)
    kids = e._runs.children(r["run_id"])
    assert len(kids) == 1 and kids[0].recipe == "child-review" and kids[0].nonce
    for _ in range(3):
        e.status(r["run_id"])                              # each entry auto-polls
    assert len(e._runs.children(r["run_id"])) == 1
    child_file = (tmp_path / "state" / "runs" / f"{r['run_id']}.subcalls" / "review" / "child.json")
    assert json.loads(child_file.read_text())["child_run"] == kids[0].run_id


def test_ensure_child_is_race_safe_under_concurrent_claims(tmp_path, monkeypatch):
    # Fresh finding A: a plain read-check-create on child.json is
    # last-writer-wins under two concurrent scenario_done calls on the same
    # parked marker — each racer would mint its own child, and the runner
    # already spawned with the LOSING child's nonce can never drive the
    # winner's child (stall to the runner timeout, plus an orphan child).
    # The O_CREAT|O_EXCL claim must make this race-free: exactly one child,
    # and the loser returns the winner's (child_run, nonce) unchanged.
    import threading
    e, proj = make_engine(tmp_path, monkeypatch)
    parent = e._runs.create("subcall-fractal", str(proj))
    workdir = tmp_path / "state" / "runs" / f"{parent.run_id}.subcalls" / "review"
    results: list[tuple[str, str]] = []
    barrier = threading.Barrier(2)

    def racer():
        barrier.wait()                                       # maximize the race window
        results.append(e._ensure_child(parent, "child-review", workdir))

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(e._runs.children(parent.run_id)) == 1          # exactly one child minted
    assert results[0] == results[1]                           # both racers agree on (child_run, nonce)
    child_file = json.loads((workdir / "child.json").read_text())
    assert child_file["child_run"] == results[0][0]


def test_fractal_poll_completes_on_child_terminal_and_pins_hashes(tmp_path, monkeypatch):
    e, proj = make_engine(tmp_path, monkeypatch, sleep=5.0)
    r = e.start("subcall-fractal", vars={}, project=str(proj))
    pass_plan(e, proj, r)
    child = e._runs.children(r["run_id"])[0]
    assert e.status(r["run_id"])["subcall"]["node"] == "review"    # child awaiting, process alive -> running
    (proj / ".lockstep" / "review.md").write_text("Verdict: PASS\n")
    # engine-direct done bypasses origin binding BY DESIGN (documented
    # same-user residual); Task 10 exercises the credentialed server path.
    assert e.done(child.run_id, "review", {"review_path": ".lockstep/review.md"})["done"] is True
    st = e.status(r["run_id"])                             # auto-poll collects
    assert st["step"] == "verify"
    env = e._peek_state(r["run_id"])["_subcall_envelope"]
    digest = hashlib.sha256((proj / ".lockstep" / "review.md").read_bytes()).hexdigest()
    assert env["child_status"] == "done" and env["artifact_hashes"] == {"review": digest}


def test_verify_step_hash_check_wired_through_engine(tmp_path, monkeypatch):
    # I6.2: the ONE test that fails if done() forgets `"_state":
    # self._peek_state(run_id)` — both Task-6 unit tests pass without it.
    e, proj = make_engine(tmp_path, monkeypatch, sleep=5.0)
    r = e.start("subcall-fractal", vars={}, project=str(proj))
    pass_plan(e, proj, r)
    child = e._runs.children(r["run_id"])[0]
    (proj / ".lockstep" / "review.md").write_text("Verdict: PASS\n")
    e.done(child.run_id, "review", {"review_path": ".lockstep/review.md"})
    assert e.status(r["run_id"])["step"] == "verify"
    (proj / ".lockstep" / "review.md").write_text("Verdict: PASS\nforged addendum\n")
    out = e.done(r["run_id"], "verify", {"review_path": ".lockstep/review.md"})
    assert out["passed"] is False and any("hash" in reason for reason in out["reasons"])
    (proj / ".lockstep" / "review.md").write_text("Verdict: PASS\n")   # restore the pinned bytes
    out = e.done(r["run_id"], "verify", {"review_path": ".lockstep/review.md"})
    assert out["done"] is True


def test_cascade_terminates_descendants_recursively(tmp_path, monkeypatch):
    # C7.3/I7.7: unit-shaped — hand-built depth-2 chain + one live workdir;
    # never depends on abort being allowed mid-subcall.
    e, proj = make_engine(tmp_path, monkeypatch)
    parent = e._runs.create("feature-dev", str(proj))
    child = e._runs.create("child-review", str(proj), parent_run=parent.run_id, nonce="n1")
    grand = e._runs.create("child-review", str(proj), parent_run=child.run_id, nonce="n2")
    done_child = e._runs.create("child-review", str(proj), parent_run=parent.run_id, nonce="n3")
    e._runs.update(done_child.run_id, status="done")
    wd = tmp_path / "state" / "runs" / f"{child.run_id}.subcalls" / "review"
    subcalls.start_process([sys.executable, str(FAKE), "--sleep", "30"], cwd=str(proj),
                           env=dict(os.environ), workdir=wd, timeout_minutes=5)
    e._cascade_terminate(parent.run_id)
    assert e._runs.get(child.run_id).status == "aborted"
    assert e._runs.get(grand.run_id).status == "aborted"   # recursive: grandchild flipped too
    assert e._runs.get(done_child.run_id).status == "done" # terminal CAS holds
    res = subcalls.probe(wd)
    assert res["status"] == "error" and any("cancel" in x.lower() for x in res["reasons"])


def test_poisoned_child_cannot_kill_parent_but_dead_runner_escalates_and_cascades(tmp_path, monkeypatch):
    # end-to-end: the fake runner dies without driving its child run ->
    # fractal poll rule 4 -> error -> parent escalates via _auto_poll ->
    # cascade aborts the orphaned child. Also proves C5.4's refusal lifts.
    e, proj = make_engine(tmp_path, monkeypatch)           # mode=ok: exits immediately, child left awaiting
    r = e.start("subcall-fractal", vars={}, project=str(proj))
    pass_plan(e, proj, r)
    child = e._runs.children(r["run_id"])[0]
    deadline = time.time() + 10
    st = e.status(r["run_id"])
    while st.get("status") == "awaiting" and time.time() < deadline:
        time.sleep(0.05)
        st = e.status(r["run_id"])
    assert st["status"] == "escalated"
    assert e._runs.get(child.run_id).status == "aborted"
