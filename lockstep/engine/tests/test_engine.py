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
        (FIXTURES / "child_interrupt.recipe.yaml").read_bytes()
    )
    return recipes


def test_engine_is_state_free_service_delegate_and_restarts_from_native_checkpoint(tmp_path):
    state = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    recipes = _recipes(tmp_path)
    first = Engine(state, recipes)
    started = first.start("native-parent-direct", {}, str(project))
    run_id = started["run_id"]
    sessions.touch(state, run_id, "session-1", 30)
    first.close()

    restarted = Engine(state, recipes)
    completed = restarted.done(
        run_id,
        "answer",
        {"answer": "yes"},
        session_id="session-1",
        project=str(project),
    )
    assert completed["status"] == "completed"
    assert restarted.status(run_id, str(project))["status"] == "completed"
    restarted.close()


def test_worker_resume_requires_current_session_binding(tmp_path):
    state = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    engine = Engine(state, _recipes(tmp_path))
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
    assert engine.status(run_id, str(project))["status"] == "awaiting"
    engine.close()


def test_engine_has_no_workflow_state_fields(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    engine = Engine(tmp_path / "state", _recipes(tmp_path))
    assert set(engine.__dict__) == {"_service"}
    engine.close()
