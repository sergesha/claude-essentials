import json
from pathlib import Path

import pytest

from lockstep import authoring
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.runtime import sessions
from lockstep.runtime.hooks import (
    doctor,
    hook_pretool,
    hook_session_start,
    hook_stop,
    policy_require,
)
from lockstep.runtime.service import LockstepCommandService
from lockstep.runtime.start_service import AuthorizedStartService
from tests._authoring_gate import replace_marker, tree_image, write_workflow
from tests.test_authoring_legacy_v4_refusal import (
    _locate_test_namespace,
    _retain,
    live_v4_bytes,
)

FIXTURES = Path(__file__).parent / "fixtures" / "native"


def _parked(tmp_path):
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "native-parent-direct.recipe.yaml").write_bytes(
        (FIXTURES / "parent_direct.recipe.yaml").read_bytes()
    )
    (recipes / "child_interrupt.recipe.yaml").write_bytes(
        (FIXTURES / "worker_child_interrupt.recipe.yaml").read_bytes()
    )
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    service = LockstepCommandService(state, recipes)
    run_id = service.start("native-parent-direct", {}, str(project))["run_id"]
    service.close()
    return state, recipes, project, run_id


def test_stop_and_session_start_are_read_only_native_projections(tmp_path):
    state, _recipes, project, run_id = _parked(tmp_path)
    before = {
        path: path.stat().st_mtime_ns
        for path in state.rglob("*")
        if path.is_file()
        and not path.name.endswith(("-wal", "-shm", "-journal"))
    }
    _code, raw = hook_stop({}, state, str(project))
    assert run_id in json.loads(raw)["reason"]
    assert run_id in hook_session_start(state, str(project))
    after = {
        path: path.stat().st_mtime_ns
        for path in state.rglob("*")
        if path.is_file()
        and not path.name.endswith(("-wal", "-shm", "-journal"))
    }
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
    assert "start a fresh run" in report


def test_legacy_evidence_blocks_only_its_exact_project_namespace(
    tmp_path, monkeypatch
) -> None:
    state = (tmp_path / "state").resolve()
    projects = {name: tmp_path / name for name in ("project-a", "project-b")}
    for project in projects.values():
        project.mkdir()
        write_workflow(project, "release")
        authoring.publish_project_compilation(project, "release", state_dir=state)
    namespace, _identity = _locate_test_namespace(state, projects["project-a"])
    evidence = _retain(namespace, live_v4_bytes(projects["project-a"]))
    evidence_before = (evidence.read_bytes(), evidence.stat().st_ino)
    source_b = projects["project-b"] / ".lockstep/workflows/release.workflow.yaml"
    replace_marker(source_b, "initial", "updated")

    assert AuthoringPublisher(state).observe(
        projects["project-b"], lambda: "observed"
    ) == "observed"
    authoring.publish_project_compilation(
        projects["project-b"], "release", state_dir=state
    )
    started = []
    monkeypatch.setattr(
        AuthorizedStartService,
        "start",
        lambda _self, *_args, **_kwargs: started.append(True)
        or {"status": "captured", "run_id": "probe"},
    )
    service = LockstepCommandService(
        state, projects["project-b"] / ".lockstep/recipes"
    )
    try:
        assert service.start("release", {}, str(projects["project-b"]))["run_id"] == "probe"
    finally:
        service.close()
    assert started == [True]
    with pytest.raises(Exception, match=str(projects["project-a"].resolve())):
        AuthoringPublisher(state).observe(projects["project-a"], lambda: None)
    assert (evidence.read_bytes(), evidence.stat().st_ino) == evidence_before


def test_replaced_project_uses_distinct_namespace_and_doctor_reports_orphan_read_only(
    tmp_path
) -> None:
    state = (tmp_path / "state").resolve()
    project = tmp_path / "project"
    project.mkdir()
    write_workflow(project, "release")
    authoring.publish_project_compilation(project, "release", state_dir=state)
    old_namespace, _identity = _locate_test_namespace(state, project)
    evidence = _retain(old_namespace, live_v4_bytes(project))
    retired = tmp_path / "retired"
    project.rename(retired)
    project.mkdir()
    write_workflow(project, "release")
    retired_before = tree_image(retired)
    evidence_before = (evidence.read_bytes(), evidence.stat().st_ino)

    authoring.publish_project_compilation(project, "release", state_dir=state)

    new_namespace, _identity = _locate_test_namespace(state, project)
    assert new_namespace != old_namespace
    assert tree_image(retired) == retired_before
    assert (evidence.read_bytes(), evidence.stat().st_ino) == evidence_before
    before = tree_image(state)
    ok, report = doctor(state, project / ".lockstep/recipes")
    legacy_line = next(line for line in report.splitlines() if "legacy authoring" in line)
    assert ok is False
    assert "original exact project directory identity" in legacy_line
    assert "pre-simplification" in legacy_line
    assert "Do not delete transaction.json manually" in legacy_line
    assert str(project.resolve()) not in legacy_line
    assert tree_image(state) == before


def test_doctor_bounds_namespace_discovery_without_blocking_normal_project_observation(
    tmp_path
) -> None:
    state = tmp_path / "state"
    authoring_root = state / "authoring"
    authoring_root.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    authoring_root.chmod(0o700)
    for index in range(257):
        namespace = authoring_root / f"{index:064x}"
        namespace.mkdir(mode=0o700)
    project = tmp_path / "project"
    project.mkdir()
    recipes = tmp_path / "recipes"
    recipes.mkdir()

    assert AuthoringPublisher(state.resolve()).observe(project, lambda: "ok") == "ok"
    before = tree_image(state)
    ok, report = doctor(state, recipes)

    assert ok is False
    assert "256" in report and "may remain undiscovered" in report
    assert "pre-simplification" in report
    assert "original exact project directory identity" in report
    assert "Do not delete transaction.json manually" in report
    assert tree_image(state) == before
