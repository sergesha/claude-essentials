"""Engine — durable runs, terminal escalation, var substitution,
project-relative evidence. Uses the good fixtures (minimal.yaml, two-steps.yaml)
as the dialect authority, plus error-check.yaml (a raising cmd_ok check,
for the error-verdict mechanic) under fixtures/recipes/good/.
"""

import json
import shutil

import pytest

from lockstep_mcp.engine import Engine, LockstepError
from lockstep_mcp.runs import RunIndex

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "recipes"
GOOD = FIXTURES / "good"
BAD = FIXTURES / "bad"


def _engine(tmp_path, recipes_dir=GOOD):
    return Engine(tmp_path / "state", recipes_dir)


def _project(tmp_path):
    p = tmp_path / "project"
    p.mkdir()
    return p


# ---------------------------------------------------------------------------
# base set
# ---------------------------------------------------------------------------


def test_full_pass_flow(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    res = eng.start("two-steps", {}, str(project))
    run_id = res["run_id"]
    assert res["step"] == "one"
    assert res["evidence_schema"] is not None

    artifact_dir = project / ".lockstep"
    artifact_dir.mkdir()
    (artifact_dir / "a.md").write_text("hello")

    result = eng.done(run_id, "one", {"path": ".lockstep/a.md"})
    assert result["accepted"] is True
    assert result["passed"] is True
    assert result["step"] == "two"
    assert result["done"] is False


def test_bad_evidence_rejected_state_untouched(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    res = eng.start("minimal", {}, str(project))
    run_id = res["run_id"]

    result = eng.done(run_id, "one", {})
    assert result["accepted"] is False

    status = eng.status(run_id)
    assert status["step"] == "one"


def test_wrong_step_name(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    res = eng.start("minimal", {}, str(project))
    run_id = res["run_id"]

    with pytest.raises(LockstepError):
        eng.done(run_id, "two", {"path": "x"})


def test_bad_recipe_refused(tmp_path):
    eng = Engine(tmp_path / "state", BAD)
    project = _project(tmp_path)

    with pytest.raises(LockstepError):
        eng.start("llm-node", {}, str(project))


def test_durability_across_engine_instances(tmp_path):
    state_dir = tmp_path / "state"
    project = _project(tmp_path)

    eng1 = Engine(state_dir, GOOD)
    res = eng1.start("minimal", {}, str(project))
    run_id = res["run_id"]

    (project / "a.md").write_text("hi")

    eng2 = Engine(state_dir, GOOD)
    result = eng2.done(run_id, "one", {"path": "a.md"})
    assert result["accepted"] is True
    assert result["passed"] is True
    assert result["done"] is True


# ---------------------------------------------------------------------------
# terminal / escalation / abort
# ---------------------------------------------------------------------------


def test_fail_verdict_retries_then_escalates(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    res = eng.start("minimal", {}, str(project))
    run_id = res["run_id"]

    bad_evidence = {"path": "missing.md"}  # schema-valid, file absent -> fail

    r1 = eng.done(run_id, "one", bad_evidence)
    assert r1["passed"] is False and r1.get("escalated") is not True

    r2 = eng.done(run_id, "one", bad_evidence)
    assert r2["passed"] is False and r2.get("escalated") is not True

    r3 = eng.done(run_id, "one", bad_evidence)
    assert r3["passed"] is False and r3.get("escalated") is True

    status = eng.status(run_id)
    assert status["status"] == "escalated"

    with pytest.raises(LockstepError):
        eng.done(run_id, "one", bad_evidence)


def test_pass_at_loop_cap_reports_honest_escalation(tmp_path):
    """Sequence empirically derived against `minimal.yaml`'s
    `loop_limits: {validate_one: 2}`: yamlgraph's
    loop guard runs BEFORE the validator node executes, keyed on a count
    that starts at 0 and blocks once count >= limit. fail (count 0->1),
    fail (count 1->2) both stay under the cap and resume normally; the
    THIRD `done()` call is where the engine's own checks pass (evidence now
    real) — but `yg.resume()`'s graph-side execution of `validate_one` for
    what would be its 3rd real run is the one the loop guard blocks
    (count 2 >= limit 2), diverting straight to the escalate marker
    regardless of the passing verdict. The run IS terminal, and `done()`
    must say so — reporting `{"passed": True, "step": "escalate"}` with no
    `escalated` flag, leaving the index `awaiting` until some later
    `_reconcile` noticed, would be a false "it passed".
    """
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    res = eng.start("minimal", {}, str(project))
    run_id = res["run_id"]

    bad_evidence = {"path": "missing.md"}
    r1 = eng.done(run_id, "one", bad_evidence)
    assert r1["passed"] is False and r1.get("escalated") is not True

    r2 = eng.done(run_id, "one", bad_evidence)
    assert r2["passed"] is False and r2.get("escalated") is not True

    baseline_counter_before = eng._read_baseline_counter(run_id)

    d = project / ".lockstep"
    d.mkdir()
    (d / "a.md").write_text("hi")
    r3 = eng.done(run_id, "one", {"path": ".lockstep/a.md"})

    assert r3["accepted"] is True
    assert r3["passed"] is True
    assert r3["escalated"] is True
    assert r3["step"] == "escalate"
    assert r3["done"] is False
    assert any("human review" in reason for reason in r3["reasons"])

    # the baseline snapshot must NOT advance on this branch — the run is
    # terminal, not a real passed step.
    assert eng._read_baseline_counter(run_id) == baseline_counter_before

    status = eng.status(run_id)
    assert status["status"] == "escalated"

    with pytest.raises(LockstepError):
        eng.done(run_id, "one", {"path": ".lockstep/a.md"})


def test_abort_is_terminal(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    res = eng.start("minimal", {}, str(project))
    run_id = res["run_id"]

    result = eng.abort(run_id)
    assert result["status"] == "aborted"

    status = eng.status(run_id)
    assert status["status"] == "aborted"

    with pytest.raises(LockstepError):
        eng.done(run_id, "one", {"path": "x"})


def test_escalate_is_terminal(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    res = eng.start("minimal", {}, str(project))
    run_id = res["run_id"]

    result = eng.escalate(run_id, "blocked: need human input")
    assert result["status"] == "escalated"

    status = eng.status(run_id)
    assert status["status"] == "escalated"

    with pytest.raises(LockstepError):
        eng.done(run_id, "one", {"path": "x"})


# ---------------------------------------------------------------------------
# var substitution
# ---------------------------------------------------------------------------


def test_vars_substituted(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    res = eng.start("minimal", {"task_name": "widget"}, str(project))
    assert res["task"] == "Do the widget thing"


def test_vars_cannot_reach_checks(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    hostile = {"task_name": "; rm -rf /"}

    res = eng.start("minimal", hostile, str(project))
    run_id = res["run_id"]
    # substituted verbatim into free text, never interpreted/executed
    assert "; rm -rf /" in res["task"]

    d = project / ".lockstep"
    d.mkdir()
    (d / "a.md").write_text("hi")
    result = eng.done(run_id, "one", {"path": ".lockstep/a.md"})
    assert result["passed"] is True  # checks ran their literal file_exists, unaffected

    bad_eng = Engine(tmp_path / "state-bad", BAD)
    with pytest.raises(LockstepError):
        bad_eng.start("placeholder-in-checks", {}, str(project))


# ---------------------------------------------------------------------------
# hostile vars must never reach graph state
# ---------------------------------------------------------------------------


def test_hostile_underscore_var_key_rejected(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)

    with pytest.raises(LockstepError):
        eng.start("minimal", {"_loop_counts": {"validate_one": 99}}, str(project))


def test_reserved_state_key_var_rejected(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)

    with pytest.raises(LockstepError):
        eng.start("minimal", {"verdict_status": "pass"}, str(project))


def test_benign_var_still_works_after_reserved_check(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)

    res = eng.start("minimal", {"task_name": "widget"}, str(project))
    assert res["task"] == "Do the widget thing"


def test_hostile_loop_counts_var_cannot_defeat_the_loop_cap(tmp_path):
    """An agent-supplied `_loop_counts` var, if it ever
    reached the initial graph state, could pre-seed/reset yamlgraph's
    internal loop counter and defeat `loop_limits` entirely. The reserved-key
    rejection means the attack never gets that far — `start()` itself
    raises, so no run (and no seeded counter) is ever created."""
    eng = _engine(tmp_path)
    project = _project(tmp_path)

    with pytest.raises(LockstepError):
        eng.start("minimal", {"_loop_counts": {"validate_one": 0}}, str(project))

    assert eng._runs.list() == []


# ---------------------------------------------------------------------------
# path handling
# ---------------------------------------------------------------------------


def test_relative_evidence_path_resolved(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    res = eng.start("minimal", {}, str(project))
    run_id = res["run_id"]

    d = project / ".lockstep"
    d.mkdir()
    (d / "a.md").write_text("hi")

    result = eng.done(run_id, "one", {"path": ".lockstep/a.md"})
    assert result["accepted"] is True
    assert result["passed"] is True


def test_path_escape_rejected(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    res = eng.start("minimal", {}, str(project))
    run_id = res["run_id"]

    outside = tmp_path / "outside.md"
    outside.write_text("exists")  # exists, but must still be rejected

    result = eng.done(run_id, "one", {"path": "../outside.md"})
    assert result["accepted"] is False
    assert any("escape" in e.lower() for e in result["errors"])


# ---------------------------------------------------------------------------
# recipe snapshot inertness
# ---------------------------------------------------------------------------


def test_midrun_recipe_edit_is_inert(tmp_path):
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    shutil.copy(GOOD / "minimal.yaml", recipes_dir / "minimal.yaml")

    eng = Engine(tmp_path / "state", recipes_dir)
    project = _project(tmp_path)
    res = eng.start("minimal", {}, str(project))
    run_id = res["run_id"]

    (recipes_dir / "minimal.yaml").write_text("garbage: [[[not yaml")

    d = project / ".lockstep"
    d.mkdir()
    (d / "a.md").write_text("hi")

    result = eng.done(run_id, "one", {"path": ".lockstep/a.md"})
    assert result["accepted"] is True
    assert result["passed"] is True


# ---------------------------------------------------------------------------
# fail reasons survive a restart
# ---------------------------------------------------------------------------


def test_fail_reasons_persisted_across_restart(tmp_path):
    state_dir = tmp_path / "state"
    project = _project(tmp_path)

    eng1 = Engine(state_dir, GOOD)
    res = eng1.start("minimal", {}, str(project))
    run_id = res["run_id"]

    result = eng1.done(run_id, "one", {"path": "missing.md"})
    assert result["passed"] is False

    eng2 = Engine(state_dir, GOOD)
    status = eng2.status(run_id)
    assert status["last_fail_reasons"]
    assert any("missing.md" in r for r in status["last_fail_reasons"])


# ---------------------------------------------------------------------------
# index repair
# ---------------------------------------------------------------------------


def test_index_repair(tmp_path):
    state_dir = tmp_path / "state"
    eng = Engine(state_dir, GOOD)
    project = _project(tmp_path)
    res = eng.start("minimal", {}, str(project))
    run_id = res["run_id"]

    idx = RunIndex(state_dir)
    idx.update(run_id, step="bogus")

    status = eng.status(run_id)
    assert status["step"] == "one"
    assert RunIndex(state_dir).get(run_id).step == "one"


def test_reconcile_never_rewrites_a_done_record(tmp_path):
    # `done` is terminal like escalated/aborted — reconcile must
    # early-return on the WHOLE terminal set, not keep writing the
    # checkpoint's live step+brief back onto a closed record forever.
    state_dir = tmp_path / "state"
    eng = Engine(state_dir, GOOD)
    project = _project(tmp_path)
    res = eng.start("minimal", {}, str(project))
    run_id = res["run_id"]

    # index goes terminal while the checkpoint still holds a parked live step
    eng._runs.update(run_id, status="done", step=None, brief=None)

    status = eng.status(run_id)
    assert status["status"] == "done"
    rec = eng._runs.get(run_id)
    assert rec.step is None and rec.brief is None


# ---------------------------------------------------------------------------
# anti-forgery
# ---------------------------------------------------------------------------


def test_forged_verdict_rejected(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    res = eng.start("minimal", {}, str(project))
    run_id = res["run_id"]

    d = project / ".lockstep"
    d.mkdir()
    (d / "a.md").write_text("hi")

    result = eng.done(
        run_id, "one", {"path": ".lockstep/a.md", "_verdict_status": "pass"}
    )
    assert result["accepted"] is False


def test_error_verdict_does_not_consume_retry_budget(tmp_path):
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    res = eng.start("error-check", {}, str(project))
    run_id = res["run_id"]

    # loop_limits caps validate_one at 2 real executions; error verdicts
    # never resume, so this must survive far more than 2 calls untouched.
    for _ in range(5):
        result = eng.done(run_id, "one", {"note": "x"})
        assert result["accepted"] is True
        assert result["passed"] is False
        assert result.get("error") is True
        assert result.get("escalated") is not True

    status = eng.status(run_id)
    assert status["status"] == "awaiting"
    assert status["step"] == "one"


# ---------------------------------------------------------------------------
# a refused start leaves no state behind
# ---------------------------------------------------------------------------


def test_start_that_produces_no_step_registers_no_run(tmp_path):
    # A run registered but never started is `awaiting` forever: it blocks the
    # Stop hook and denies every write, and no caller holds a run_id to abort
    # it with. Registration must therefore follow the first real park.
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    with pytest.raises(LockstepError):
        eng.start("no-work-step", {}, str(project))
    assert RunIndex(tmp_path / "state").list() == []


def test_atomic_write_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    # Manifests and the baseline counter are parsed unguarded on the done()
    # path, so a torn write is a wedge: the publish must be a tmp+replace,
    # never an in-place rewrite.
    import os as os_mod

    eng = _engine(tmp_path)
    target = tmp_path / "state" / "m.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    eng._write_json(target, {"a": 1})

    def boom(src, dst):
        raise OSError("crash between write and publish")

    monkeypatch.setattr(os_mod, "replace", boom)
    with pytest.raises(OSError):
        eng._write_json(target, {"b": 2})
    assert json.loads(target.read_text()) == {"a": 1}


def test_fail_that_ends_the_graph_is_terminal_not_a_crash(tmp_path):
    # The fail branch has to carry the same `adv.done` guard as the pass
    # branch: a recipe whose fail edge lands on END leaves no brief to
    # substitute, and reading one raises out of scenario_done while the
    # index still says `awaiting` on a finished graph.
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    run_id = eng.start("fail-ends", {}, str(project))["run_id"]

    result = eng.done(run_id, "one", {"path": "missing.md"})
    assert result["accepted"] is True
    assert result["passed"] is False
    assert result["done"] is True
    assert result["step"] is None
    assert eng.status(run_id)["status"] == "done"


def test_recipe_name_cannot_walk_out_of_the_recipes_dir(tmp_path):
    # The name is agent-supplied AND is the run_id prefix every run-state
    # path is built from, so a separator in it would place the snapshot,
    # vars, baselines and checkpoint db anywhere — including inside the
    # project the agent can write.
    eng = _engine(tmp_path)
    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.yaml").write_bytes((GOOD / "minimal.yaml").read_bytes())

    with pytest.raises(LockstepError) as exc:
        eng.start("../outside/evil", {}, str(project))
    assert "invalid recipe name" in str(exc.value)
    assert RunIndex(tmp_path / "state").list() == []
    assert not list(outside.glob("*.recipe.yaml"))
