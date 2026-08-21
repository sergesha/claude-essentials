from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from lockstep.mcp import server
from lockstep.runtime import advisory_lock, sessions
from lockstep.runtime.engine import LockstepError

FIXTURES = Path(__file__).parent / "fixtures" / "native"
EXPECTED_TOOLS = {
    "scenario_start", "scenario_status", "scenario_done", "scenario_escalate",
    "scenario_abort", "scenario_dryrun", "list_recipes", "validate_recipe",
    "render_flow", "list_runs", "run_trace",
}


def _configure(monkeypatch, tmp_path):
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "native-parent-direct.recipe.yaml").write_bytes(
        (FIXTURES / "parent_direct.recipe.yaml").read_bytes()
    )
    child = (FIXTURES / "worker_child_interrupt.recipe.yaml").read_text()
    (recipes / "child_interrupt.recipe.yaml").write_text(
        child.replace("name: native-child-interrupt", "name: child_interrupt")
    )
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(recipes))
    monkeypatch.chdir(project)
    server._reset_engine()
    return project, recipes


def _ctx(project: Path, session_id: str | None = None):
    meta = {
        "x-codex-turn-metadata": {"workspaces": {str(project): {}}},
    }
    if session_id is not None:
        meta["session_id"] = session_id
    return SimpleNamespace(request_context=SimpleNamespace(meta=meta))


def test_tools_registered():
    assert {tool.name for tool in server.app._tool_manager.list_tools()} == EXPECTED_TOOLS


def test_native_start_status_list_and_history_use_immutable_catalog(tmp_path, monkeypatch):
    project, _recipes = _configure(monkeypatch, tmp_path)
    started = server.scenario_start("native-parent-direct", {}, ctx=_ctx(project))
    run_id = started["run_id"]
    assert started["status"] == "awaiting"
    assert started[sessions.BINDING_MARKER_KEY] == sessions.BINDING_MARKER_VALUE
    assert server.scenario_status(run_id, ctx=_ctx(project))["status"] == "awaiting"
    assert [item["run_id"] for item in server.list_runs(ctx=_ctx(project))] == [run_id]
    assert "checkpoint_id" in server.run_trace(run_id, ctx=_ctx(project))


def test_scenario_done_uses_current_native_session_binding(tmp_path, monkeypatch):
    project, _recipes = _configure(monkeypatch, tmp_path)
    run_id = server.scenario_start("native-parent-direct", {}, ctx=_ctx(project))["run_id"]
    sessions.touch(tmp_path / "state", run_id, "session-1", 30)

    with pytest.raises(LockstepError, match="session binding"):
        server.scenario_done(run_id, "answer", {}, ctx=_ctx(project, "foreign"))
    completed = server.scenario_done(
        run_id, "answer", {"answer": "yes"}, ctx=_ctx(project, "session-1")
    )
    assert completed["status"] == "completed"


def test_service_rechecks_session_after_mcp_edge_guard(tmp_path, monkeypatch):
    project, _recipes = _configure(monkeypatch, tmp_path)
    run_id = server.scenario_start("native-parent-direct", {}, ctx=_ctx(project))["run_id"]
    state = tmp_path / "state"
    sessions.touch(state, run_id, "session-1", 30)
    service = server._eng(project)._service
    original = service.require_session

    def swap_owner_after_edge_check(checked_run_id, session_id, checked_project):
        original(checked_run_id, session_id, checked_project)
        binding = sessions.read_binding(state, checked_run_id)
        assert binding is not None
        binding["session_id"] = "foreign"
        sessions.binding_path(state, checked_run_id).write_text(json.dumps(binding))

    monkeypatch.setattr(service, "require_session", swap_owner_after_edge_check)
    with pytest.raises(LockstepError, match="session binding"):
        server.scenario_done(
            run_id, "answer", {"answer": "yes"}, ctx=_ctx(project, "session-1")
        )
    assert server.scenario_status(run_id, ctx=_ctx(project))["status"] == "awaiting"


def test_session_rebinding_waits_for_verified_native_resume_commit(tmp_path, monkeypatch):
    project, _recipes = _configure(monkeypatch, tmp_path)
    run_id = server.scenario_start("native-parent-direct", {}, ctx=_ctx(project))["run_id"]
    state = tmp_path / "state"
    sessions.touch(state, run_id, "owner", 30)
    service = server._eng(project)._service
    original_resume = service.runtime.resume
    entered = threading.Event()
    release = threading.Event()
    adopted = threading.Event()
    errors = []

    def blocked_resume(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_resume(*args, **kwargs)

    def complete():
        try:
            service.scenario_done(
                run_id,
                "answer",
                {"answer": "yes"},
                session_id="owner",
                project=str(project),
            )
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - asserted below
            errors.append(exc)

    def replace_owner():
        sessions.touch(state, run_id, "replacement", -1)
        adopted.set()

    monkeypatch.setattr(service.runtime, "resume", blocked_resume)
    completing = threading.Thread(target=complete)
    completing.start()
    assert entered.wait(5)
    real_monotonic = advisory_lock.time.monotonic
    monkeypatch.setattr(
        advisory_lock.time, "monotonic", lambda: real_monotonic() + 61
    )
    replacing = threading.Thread(target=replace_owner)
    replacing.start()
    assert not adopted.wait(0.1)
    release.set()
    completing.join(5)
    replacing.join(5)
    assert errors == []
    assert adopted.is_set()
    assert sessions.read_binding(state, run_id)["session_id"] == "replacement"


def test_cross_project_status_and_resume_are_indistinguishable_and_read_only(
    tmp_path, monkeypatch
):
    project, _recipes = _configure(monkeypatch, tmp_path)
    run_id = server.scenario_start("native-parent-direct", {}, ctx=_ctx(project))["run_id"]
    state = tmp_path / "state"
    sessions.touch(state, run_id, "owner", 30)
    foreign = tmp_path / "foreign-project"
    foreign.mkdir()
    before = {
        path.relative_to(state): path.read_bytes()
        for path in state.rglob("*")
        if path.is_file()
    }

    for operation in (
        lambda: server.scenario_status(run_id, ctx=_ctx(foreign)),
        lambda: server.scenario_done(
            run_id,
            "answer",
            {"answer": "yes"},
            ctx=_ctx(foreign, "owner"),
        ),
    ):
        with pytest.raises(LockstepError, match=f"unknown run {run_id!r}"):
            operation()
    after = {
        path.relative_to(state): path.read_bytes()
        for path in state.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_scenario_start_rejects_python_before_import_run_or_checkpoint_mutation(
    tmp_path, monkeypatch
):
    project, recipes = _configure(monkeypatch, tmp_path)
    sentinel = project / "START-IMPORTED"
    (project / "attacker_module.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n"
        "def run(state): return state\n"
    )
    (recipes / "attacker.recipe.yaml").write_text(
        "name: attacker\n"
        "tools:\n"
        "  code: {type: python, module: attacker_module, function: run}\n"
        "nodes: {code: {type: python, tool: code}}\n"
        "edges: [{from: START, to: code}, {from: code, to: END}]\n"
    )
    with pytest.raises(LockstepError, match="executable authority denied"):
        server.scenario_start("attacker", {}, ctx=_ctx(project))
    assert not sentinel.exists()
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("reserved", ["lockstep_outcome", "namespace", "_checkpoint"])
def test_scenario_start_rejects_engine_owned_input_before_state_mutation(
    tmp_path, monkeypatch, reserved
):
    project, _recipes = _configure(monkeypatch, tmp_path)
    with pytest.raises(LockstepError, match="reserved scenario input"):
        server.scenario_start(
            "native-parent-direct", {reserved: "PASS"}, ctx=_ctx(project)
        )
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("invalid", [[], "", 0, False])
def test_scenario_start_rejects_non_object_and_oversized_input_before_state(
    tmp_path, monkeypatch, invalid
):
    project, _recipes = _configure(monkeypatch, tmp_path)
    with pytest.raises(LockstepError, match="JSON object"):
        server.scenario_start("native-parent-direct", invalid, ctx=_ctx(project))
    assert not (tmp_path / "state").exists()


def test_scenario_start_rejects_oversized_input_before_state(tmp_path, monkeypatch):
    project, _recipes = _configure(monkeypatch, tmp_path)
    with pytest.raises(LockstepError, match="byte limit"):
        server.scenario_start(
            "native-parent-direct", {"huge": "x" * 70_000}, ctx=_ctx(project)
        )
    assert not (tmp_path / "state").exists()


def test_oversized_result_controls_leave_native_state_byte_identical(tmp_path, monkeypatch):
    project, _recipes = _configure(monkeypatch, tmp_path)
    run_id = server.scenario_start("native-parent-direct", {}, ctx=_ctx(project))["run_id"]
    state = tmp_path / "state"
    sessions.touch(state, run_id, "owner", 30)
    before = {
        path.relative_to(state): path.read_bytes()
        for path in state.rglob("*")
        if path.is_file()
    }
    operations = (
        lambda: server.scenario_done(
            run_id, "answer", {"huge": "x" * 70_000}, ctx=_ctx(project, "owner")
        ),
        lambda: server.scenario_escalate(
            run_id, "x" * 70_000, ctx=_ctx(project, "owner")
        ),
    )
    for operation in operations:
        with pytest.raises(LockstepError, match="byte limit"):
            operation()
    after = {
        path.relative_to(state): path.read_bytes()
        for path in state.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_stale_binding_is_visible_and_cannot_resume_or_adopt_on_status(
    tmp_path, monkeypatch
):
    project, _recipes = _configure(monkeypatch, tmp_path)
    run_id = server.scenario_start("native-parent-direct", {}, ctx=_ctx(project))["run_id"]
    state = tmp_path / "state"
    sessions.touch(state, run_id, "expired-owner", 30)
    sidecar = sessions.binding_path(state, run_id)
    binding = json.loads(sidecar.read_text())
    binding["last_seen"] = "2000-01-01T00:00:00+00:00"
    sidecar.write_text(json.dumps(binding, sort_keys=True))
    before_binding = sidecar.read_bytes()

    status = server.scenario_status(run_id, ctx=_ctx(project, "expired-owner"))
    assert status["status"] == "awaiting"
    assert status["binding_integrity"] == "missing_or_stale"
    assert "expired-owner" not in json.dumps(status)
    assert sidecar.read_bytes() == before_binding

    before = {
        path.relative_to(state): path.read_bytes()
        for path in state.rglob("*")
        if path.is_file()
    }
    operations = (
        lambda: server.scenario_done(
            run_id, "answer", {"answer": "yes"}, ctx=_ctx(project, "expired-owner")
        ),
        lambda: server.scenario_escalate(
            run_id, "expired", ctx=_ctx(project, "expired-owner")
        ),
        lambda: server.scenario_abort(run_id, ctx=_ctx(project, "expired-owner")),
    )
    for operation in operations:
        with pytest.raises(LockstepError, match="stale"):
            operation()
    after = {
        path.relative_to(state): path.read_bytes()
        for path in state.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_validate_recipe_rejects_python_before_import_or_owner_state_mutation(
    tmp_path, monkeypatch
):
    project, _recipes = _configure(monkeypatch, tmp_path)
    sentinel = project / "IMPORTED"
    (project / "attacker_module.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n"
        "def run(state): return state\n"
    )
    recipe = project / "attacker.recipe.yaml"
    recipe.write_text(
        "name: attacker\n"
        "tools:\n  code: {type: python, module: attacker_module, function: run}\n"
        "nodes: {code: {type: python, tool: code}}\n"
        "edges: [{from: START, to: code}, {from: code, to: END}]\n"
    )
    result = server.validate_recipe(str(recipe), ctx=_ctx(project))
    assert result["ok"] is False
    assert any("executable authority denied" in error for error in result["errors"])
    assert not sentinel.exists()


def test_list_and_rejected_render_do_not_initialize_runtime_state(tmp_path, monkeypatch):
    project, recipes = _configure(monkeypatch, tmp_path)
    assert "native-parent-direct" in server.list_recipes(ctx=_ctx(project))
    assert not (tmp_path / "state").exists()

    (recipes / "unsafe-render.recipe.yaml").write_text(
        "name: unsafe-render\n"
        "tools:\n  code: {type: python, module: attacker, function: run}\n"
        "nodes: {code: {type: python, tool: code}}\n"
        "edges: [{from: START, to: code}, {from: code, to: END}]\n"
    )
    with pytest.raises(LockstepError, match="executable authority denied"):
        server.render_flow("unsafe-render", ctx=_ctx(project))
    assert not (tmp_path / "state").exists()


def test_dryrun_reads_only_an_authorized_immutable_recipe(tmp_path, monkeypatch):
    project, recipes = _configure(monkeypatch, tmp_path)
    (recipes / "unsafe.recipe.yaml").write_text(
        "name: unsafe\n"
        "tools:\n  validate: {type: python, module: attacker, function: run}\n"
        "nodes:\n"
        "  work:\n"
        "    type: interrupt\n"
        "    idempotent: false\n"
        "    message: {step: work, task: x, exit_criterion: y, checks: [{type: equals, key: answer, value: 'yes'}]}\n"
        "  validate: {type: python, tool: validate}\n"
        "edges: [{from: START, to: work}, {from: work, to: validate}, {from: validate, to: END}]\n"
    )
    with pytest.raises(LockstepError, match="executable authority denied"):
        server.scenario_dryrun("unsafe", "work", {"answer": "yes"}, ctx=_ctx(project))
    assert not (tmp_path / "state").exists()


def test_dryrun_bounds_evidence_before_recipe_preflight_or_state(tmp_path, monkeypatch):
    project, _recipes = _configure(monkeypatch, tmp_path)
    too_deep = {}
    cursor = too_deep
    for _ in range(18):
        child = {}
        cursor["next"] = child
        cursor = child

    for evidence in ({"huge": "x" * 70_000}, too_deep):
        with pytest.raises(LockstepError):
            server.scenario_dryrun("missing", "work", evidence, ctx=_ctx(project))
        assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("invalid", [[], "", 0, False])
def test_dryrun_rejects_falsey_non_object_before_recipe_preflight(
    tmp_path, monkeypatch, invalid
):
    project, _recipes = _configure(monkeypatch, tmp_path)
    with pytest.raises(LockstepError, match="JSON object"):
        server.scenario_dryrun("missing", "work", invalid, ctx=_ctx(project))
    assert not (tmp_path / "state").exists()


def test_dryrun_preserves_reserved_evidence_response_contract(tmp_path, monkeypatch):
    project, _recipes = _configure(monkeypatch, tmp_path)
    result = server.scenario_dryrun(
        "missing", "one", {"_forged": True}, ctx=_ctx(project)
    )
    assert result == {
        "accepted": False,
        "errors": ["reserved evidence key(s) rejected: ['_forged']"],
    }


def test_engine_singleton_closes_old_instance_before_reconfiguration(tmp_path, monkeypatch):
    project, _recipes = _configure(monkeypatch, tmp_path)
    first = server._eng(project)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "other-state"))
    second = server._eng(project)
    assert second is not first
    assert first._service._closed is True


def test_dryrun_runs_profile_before_any_persistent_service_init(tmp_path, monkeypatch):
    project, _recipes = _configure(monkeypatch, tmp_path)
    called = []

    def reject(_path):
        called.append(True)
        return ["profile rejected"], []

    monkeypatch.setattr(server.profile, "check_recipe_full", reject)
    with pytest.raises(LockstepError, match="profile rejected"):
        server.scenario_dryrun(
            "native-parent-direct", "answer", {"answer": "yes"}, ctx=_ctx(project)
        )
    assert called == [True]
    assert not (tmp_path / "state").exists()
