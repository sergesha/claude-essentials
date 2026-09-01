from pathlib import Path

import pytest

from lockstep.runtime import sessions
from lockstep.runtime.engine import Engine, LockstepError

FIXTURES = Path(__file__).parent / "fixtures" / "native"


def _recipes(tmp_path: Path) -> Path:
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "native-parent-direct.recipe.yaml").write_bytes(
        (FIXTURES / "parent_direct.recipe.yaml").read_bytes()
    )
    (recipes / "child_interrupt.recipe.yaml").write_bytes(
        (FIXTURES / "worker_child_interrupt.recipe.yaml").read_bytes()
    )
    return recipes


def test_engine_is_state_free_service_delegate_and_restarts_from_native_checkpoint(tmp_path):
    state = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    recipes = _recipes(tmp_path)
    first = Engine.command(state, recipes)
    started = first.start("native-parent-direct", {}, str(project))
    run_id = started["run_id"]
    sessions.touch(state, run_id, "session-1", 30)
    first.close()

    restarted = Engine.command(state, recipes)
    completed = restarted.done(
        run_id,
        "answer",
        {"answer": "yes"},
        session_id="session-1",
        project=str(project),
    )
    assert completed["status"] == "completed"
    assert Engine.observe(state, recipes).status(run_id, str(project))["status"] == (
        "completed"
    )
    restarted.close()


def test_worker_resume_requires_current_session_binding(tmp_path):
    state = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    engine = Engine.command(state, _recipes(tmp_path))
    run_id = engine.start("native-parent-direct", {}, str(project))["run_id"]
    sessions.touch(state, run_id, "owner", 30)

    with pytest.raises(LockstepError, match="session binding"):
        engine.done(
            run_id,
            "answer",
            {},
            session_id="foreign",
            project=str(project),
        )
    assert Engine.observe(state, engine.recipes_dir).status(
        run_id, str(project)
    )["status"] == "awaiting"
    engine.close()


def test_manual_protected_recipe_resumes_after_service_restart(tmp_path):
    state = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "protected-manual.recipe.yaml").write_text(
        "version: '1.0'\n"
        "name: protected-manual\n"
        "state: {edit_result: dict, lockstep_outcome: str}\n"
        "nodes:\n"
        "  edit:\n"
        "    type: interrupt\n"
        "    state_key: edit_request\n"
        "    resume_key: edit_result\n"
        "    idempotent: false\n"
        "    message:\n"
        "      step: edit\n"
        "      task: Edit the project\n"
        "      exit_criterion: The edit is complete\n"
        "      evidence_schema: {type: object}\n"
        "      artifact_contract: []\n"
        "      lockstep_effect:\n"
        "        schema: lockstep.effect/v1\n"
        "        kind: manual\n"
        "        logical_id: edit\n"
        "        runner: null\n"
        "        inputs: {}\n"
        "        writes: [src/]\n"
        "        artifacts: []\n"
        "        deadline_seconds: null\n"
        "        scope_state_keys: []\n"
        "        result_schema: lockstep.effect-result/v1\n"
        "  pass: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        "  fail: {type: passthrough, output: {lockstep_outcome: FAIL}}\n"
        "  error: {type: passthrough, output: {lockstep_outcome: ERROR}}\n"
        "edges:\n"
        "  - {from: START, to: edit}\n"
        "  - {from: edit, to: pass, condition: \"edit_result.outcome == 'PASS'\"}\n"
        "  - {from: edit, to: fail, condition: \"edit_result.outcome == 'FAIL'\"}\n"
        "  - {from: edit, to: error, condition: \"edit_result.outcome == 'ERROR'\"}\n"
        "  - {from: pass, to: END}\n"
        "  - {from: fail, to: END}\n"
        "  - {from: error, to: END}\n"
    )
    first = Engine.command(state, recipes)
    started = first.start("protected-manual", {}, str(project))
    assert started["status"] == "awaiting"
    assert started["step"] == "edit"
    run_id = started["run_id"]
    sessions.touch(state, run_id, "session-1", 30)
    first.close()

    restarted = Engine.command(state, recipes)
    completed = restarted.scenario_done(
        run_id,
        "edit",
        {"reviewed": True},
        session_id="session-1",
        project=str(project),
    )

    assert completed["status"] == "completed"
    assert Engine.observe(state, recipes).status(run_id, str(project))["status"] == (
        "completed"
    )
    restarted.close()
