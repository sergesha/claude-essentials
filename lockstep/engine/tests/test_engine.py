"""Task 5: Engine — durable runs, terminal escalation, var substitution,
project-relative evidence. Uses the good fixtures (minimal.yaml, two-steps.yaml)
as the dialect authority, plus error-check.yaml (a raising cmd_ok check, for
the decision-16 error-verdict mechanic) added under fixtures/recipes/good/.
"""

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
# path handling (decision 12)
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
# index repair (decision 13)
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


# ---------------------------------------------------------------------------
# anti-forgery (decision 16 + reserved `_` prefix)
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
