from pathlib import Path

from lockstep.runtime import sessions
from lockstep.runtime.service import LockstepService

FIXTURES = Path(__file__).parent / "fixtures" / "native"


def test_native_public_roundtrip_survives_service_restart_and_source_deletion(tmp_path):
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "native-parent-direct.recipe.yaml").write_bytes(
        (FIXTURES / "parent_direct.recipe.yaml").read_bytes()
    )
    (recipes / "child_interrupt.recipe.yaml").write_bytes(
        (FIXTURES / "child_interrupt.recipe.yaml").read_bytes()
    )
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"

    first = LockstepService(state, recipes)
    started = first.start("native-parent-direct", {"seed": "kept"}, str(project))
    run_id = started["run_id"]
    sessions.touch(state, run_id, "session", 30)
    first.close()
    for path in recipes.iterdir():
        path.unlink()

    restarted = LockstepService(state, recipes)
    completed = restarted.scenario_done(
        run_id,
        "answer",
        {"answer": "yes"},
        session_id="session",
        project=str(project),
    )
    assert completed["status"] == "completed"
    assert restarted.history(run_id, str(project))
    restarted.close()
