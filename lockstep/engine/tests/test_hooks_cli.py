import json
from pathlib import Path

import pytest

from lockstep.runtime import sessions
from lockstep.runtime.hooks import (
    doctor,
    hook_pretool,
    hook_session_start,
    hook_stop,
    policy_require,
)
from lockstep.runtime.service import LockstepService

FIXTURES = Path(__file__).parent / "fixtures" / "native"


def _parked(tmp_path):
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
    service = LockstepService(state, recipes)
    run_id = service.start("native-parent-direct", {}, str(project))["run_id"]
    service.close()
    return state, recipes, project, run_id


def test_stop_and_session_start_are_read_only_native_projections(tmp_path):
    state, _recipes, project, run_id = _parked(tmp_path)
    before = {path: path.stat().st_mtime_ns for path in state.rglob("*") if path.is_file()}
    _code, raw = hook_stop({}, state, str(project))
    assert run_id in json.loads(raw)["reason"]
    assert run_id in hook_session_start(state, str(project))
    after = {path: path.stat().st_mtime_ns for path in state.rglob("*") if path.is_file()}
    assert after == before


def test_doctor_reports_missing_native_session_binding_without_mutation(tmp_path):
    state, recipes, _project, run_id = _parked(tmp_path)
    ok, report = doctor(state, recipes)
    assert ok is False
    assert run_id in report and "session binding" in report


def _assert_hook_integrity_failure(state, recipes, project):
    assert hook_stop({}, state, str(project)) == (0, "")
    assert hook_session_start(state, str(project)) == ""
    _code, raw = hook_pretool(
        {"cwd": str(project), "session_id": "owner-session"}, state
    )
    assert json.loads(raw)["hookSpecificOutput"]["permissionDecision"] == "deny"
    ok, report = doctor(state, recipes)
    assert ok is False
    assert "native run projection readable" in report
    assert "trusted native state failed read-only verification" in report
    return report


@pytest.mark.parametrize(
    "relative",
    [
        "runtime.sqlite",
        "runtime.sqlite-wal",
        "runtime.sqlite-shm",
        "checkpoints/native.sqlite",
        "checkpoints/native.sqlite-wal",
        "checkpoints/native.sqlite-shm",
    ],
)
def test_hooks_reject_insecure_native_storage_files_with_documented_failure_modes(
    tmp_path, monkeypatch, relative
):
    state, recipes, project, run_id = _parked(tmp_path)
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(recipes))
    sessions.touch(state, run_id, "owner-session", 30)
    policy_require(state, str(project), "native-parent-direct")

    target = state / relative
    if not target.exists():
        target.touch(mode=0o600)
    target.chmod(0o644)

    report = _assert_hook_integrity_failure(state, recipes, project)
    assert target.name not in report


@pytest.mark.parametrize(
    "relative",
    [".", "checkpoints", "recipe-bundles", "recipe-materializations"],
)
def test_hooks_reject_insecure_native_state_directories(
    tmp_path, monkeypatch, relative
):
    state, recipes, project, run_id = _parked(tmp_path)
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(recipes))
    sessions.touch(state, run_id, "owner-session", 30)
    policy_require(state, str(project), "native-parent-direct")

    (state / relative).chmod(0o755)

    _assert_hook_integrity_failure(state, recipes, project)


def test_hooks_verify_complete_immutable_recipe_materialization(tmp_path, monkeypatch):
    state, recipes, project, run_id = _parked(tmp_path)
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(recipes))
    sessions.touch(state, run_id, "owner-session", 30)
    policy_require(state, str(project), "native-parent-direct")

    materialized_source = next(
        (state / "recipe-materializations").glob("*/native-parent-direct.recipe.yaml")
    )
    materialized_source.chmod(0o600)
    materialized_source.write_text(materialized_source.read_text() + "\n# tampered\n")

    report = _assert_hook_integrity_failure(state, recipes, project)
    assert materialized_source.name not in report


def test_doctor_redacts_live_session_identity(tmp_path):
    state, recipes, _project, run_id = _parked(tmp_path)
    secret_session = "secret-session-identity"
    sessions.touch(state, run_id, secret_session, 30)

    ok, report = doctor(state, recipes)

    assert ok is True
    assert secret_session not in report
    assert "present and live" in report


def test_doctor_reports_stale_session_binding_as_failure(tmp_path, monkeypatch):
    state, recipes, _project, run_id = _parked(tmp_path)
    secret_session = "stale-secret-session-identity"
    sessions.touch(state, run_id, secret_session, 30)
    monkeypatch.setattr("lockstep.runtime.hooks._session_stale_minutes", lambda: -1)

    ok, report = doctor(state, recipes)

    assert ok is False
    assert secret_session not in report
    assert "stale" in report
    assert "adoptable" in report
