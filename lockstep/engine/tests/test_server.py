from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from lockstep.mcp import server
from lockstep.recipe.authority import (
    OwnerReviewedGrant,
    OwnerReviewedPythonTarget,
    RecipeAuthorityPolicy,
    StrictRecipeIngress,
)
from lockstep.runtime import advisory_lock, sessions
from lockstep.runtime.engine import LockstepError

FIXTURES = Path(__file__).parent / "fixtures" / "native"
MIXED_CHECKS = Path(__file__).parent / "fixtures" / "recipes" / "good" / "mixed-checks.recipe.yaml"
EXPECTED_TOOLS = {
    "scenario_start",
    "scenario_status",
    "scenario_done",
    "scenario_escalate",
    "scenario_abort",
    "scenario_accept_artifact",
    "scenario_wait",
    "scenario_history",
    "scenario_events",
    "scenario_recover",
    "scenario_dryrun",
    "recipe_init",
    "recipe_compile",
    "recipe_check",
    "recipe_diff",
    "recipe_render",
    "recipe_estimate",
    "template_list",
    "template_show",
    "list_recipes",
    "validate_recipe",
    "render_flow",
    "list_runs",
    "run_trace",
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


def _authorize_dryrun_recipe(recipes: Path, monkeypatch) -> None:
    destination = recipes / "mixed-checks.recipe.yaml"
    destination.write_bytes(MIXED_CHECKS.read_bytes())
    candidate = StrictRecipeIngress(recipes).inspect(destination.name)
    requirement = candidate.authority_requirements[0]
    authorized = candidate.authorize(
        RecipeAuthorityPolicy(
            (
                OwnerReviewedGrant(
                    recipe_sha256=candidate.definition_sha256,
                    requirement_sha256=requirement.sha256,
                    authority="os_user_execution",
                ),
            ),
            python_targets=(
                OwnerReviewedPythonTarget(
                    module="lockstep.runtime.validators", function="run_checks"
                ),
            ),
        )
    )
    monkeypatch.setattr(server, "preflight_recipe", lambda *_args: authorized)


def test_tools_registered():
    assert {tool.name for tool in server.app._tool_manager.list_tools()} == EXPECTED_TOOLS


def test_accept_artifact_is_the_only_token_only_mcp_consent_surface() -> None:
    tools = {tool.name: tool for tool in server.app._tool_manager.list_tools()}
    schema = tools["scenario_accept_artifact"].parameters
    assert set(schema["properties"]) == {"token"}
    assert schema["required"] == ["token"]
    assert not any(
        forbidden in name
        for name in tools
        for forbidden in ("issue_consent", "preview_consent", "revoke_consent")
    )


def test_accept_artifact_forwards_only_token_and_ambient_project_without_session(
    tmp_path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    calls = []

    class FakeEngine:
        def scenario_accept_artifact(self, token, *, project):
            calls.append((token, project))
            return {"run_id": "run-1", "status": "completed"}

    monkeypatch.setattr(server, "_command_for", lambda actual: FakeEngine())
    monkeypatch.setattr(
        server,
        "_assert_origin",
        lambda *_args, **_kwargs: pytest.fail("token acceptance used session origin"),
    )
    monkeypatch.setattr(
        server,
        "_session_for_context",
        lambda *_args, **_kwargs: pytest.fail("token acceptance read a session"),
    )
    token = "secret-publication-token"
    result = server.scenario_accept_artifact(token, ctx=_ctx(project, "foreign"))

    assert calls == [(token, str(project.resolve()))]
    assert token not in json.dumps(result)


def test_accept_artifact_error_does_not_echo_token(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()

    class FakeEngine:
        def scenario_accept_artifact(self, token, *, project):
            del token, project
            raise LockstepError("invalid or stale publication consent")

    monkeypatch.setattr(server, "_command_for", lambda actual: FakeEngine())
    token = "secret-publication-token"
    with pytest.raises(LockstepError) as caught:
        server.scenario_accept_artifact(token, ctx=_ctx(project))
    assert token not in str(caught.value)


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
    service = server._command_for(project)
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
    service = server._command_for(project)
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
    server._reset_engine()
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
        assert {
            path.relative_to(state): path.read_bytes()
            for path in state.rglob("*")
            if path.is_file()
        } == before
    after = {
        path.relative_to(state): path.read_bytes()
        for path in state.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_cold_unknown_run_never_traverses_session_sidecar_namespace(
    tmp_path, monkeypatch
):
    project, _recipes = _configure(monkeypatch, tmp_path)
    run_id = server.scenario_start("native-parent-direct", {}, ctx=_ctx(project))["run_id"]
    state = tmp_path / "state"
    sessions.touch(state, run_id, "owner", 30)
    sessions.binding_path(state, run_id).write_text("{malformed")
    foreign = tmp_path / "foreign-project"
    foreign.mkdir()
    server._reset_engine()
    before = {
        path.relative_to(state): path.read_bytes()
        for path in state.rglob("*")
        if path.is_file()
    }

    for requested_run_id, requested_project in (
        (run_id, foreign),
        ("../runtime.sqlite", project),
    ):
        with pytest.raises(
            LockstepError, match=f"unknown run {requested_run_id!r}"
        ):
            server.scenario_done(
                requested_run_id,
                "answer",
                {"answer": "yes"},
                ctx=_ctx(requested_project, "owner"),
            )

    after = {
        path.relative_to(state): path.read_bytes()
        for path in state.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_cold_mcp_resume_preflights_read_only_then_activates(tmp_path, monkeypatch):
    project, _recipes = _configure(monkeypatch, tmp_path)
    run_id = server.scenario_start("native-parent-direct", {}, ctx=_ctx(project))["run_id"]
    sessions.touch(tmp_path / "state", run_id, "owner", 30)
    server._reset_engine()

    result = server.scenario_done(
        run_id,
        "answer",
        {"answer": "yes"},
        ctx=_ctx(project, "owner"),
    )

    assert result["run_id"] == run_id


@pytest.mark.parametrize("bound_session", [None, "owner"])
def test_cold_mcp_session_rejection_is_read_only(
    tmp_path, monkeypatch, bound_session
):
    project, _recipes = _configure(monkeypatch, tmp_path)
    run_id = server.scenario_start("native-parent-direct", {}, ctx=_ctx(project))["run_id"]
    state = tmp_path / "state"
    if bound_session is not None:
        sessions.touch(state, run_id, bound_session, 30)
    server._reset_engine()
    before = {
        path.relative_to(state): path.read_bytes()
        for path in state.rglob("*")
        if path.is_file()
    }

    with pytest.raises(LockstepError, match="missing, stale, or mismatched"):
        server.scenario_done(
            run_id,
            "answer",
            {"answer": "yes"},
            ctx=_ctx(project, "intruder"),
        )

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


def test_dryrun_reports_shape_checks_without_executing_commands(
    tmp_path, monkeypatch
) -> None:
    project, recipes = _configure(monkeypatch, tmp_path)
    _authorize_dryrun_recipe(recipes, monkeypatch)

    result = server.scenario_dryrun(
        "mixed-checks", "one", {"path": "missing.txt"}, ctx=_ctx(project)
    )

    assert result == {
        "accepted": True,
        "results": [
            {
                "type": "file_exists",
                "verdict": "fail",
                "reasons": ["file_exists: missing.txt is not a file"],
            },
            {"type": "cmd_ok", "verdict": "skipped (dryrun)"},
        ],
    }
    assert not (project / "DRYRUN-SENTINEL-SHOULD-NOT-EXIST").exists()


def test_dryrun_rejects_project_path_escape(tmp_path, monkeypatch) -> None:
    project, recipes = _configure(monkeypatch, tmp_path)
    _authorize_dryrun_recipe(recipes, monkeypatch)

    result = server.scenario_dryrun(
        "mixed-checks", "one", {"path": "../../outside"}, ctx=_ctx(project)
    )

    assert result["accepted"] is False
    assert any("project" in error and "path" in error for error in result["errors"])
    assert not (project / "DRYRUN-SENTINEL-SHOULD-NOT-EXIST").exists()


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
    first = server._command_for(project)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "other-state"))
    second = server._command_for(project)
    assert second is not first
    assert first._closed is True


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


@pytest.mark.parametrize(
    "operation",
    (
        lambda ctx: server.recipe_compile("release", ctx=ctx),
        lambda ctx: server.recipe_check("release", ctx=ctx),
        lambda ctx: server.recipe_diff("release", ctx=ctx),
        lambda ctx: server.recipe_render("release", ctx=ctx),
        lambda ctx: server.recipe_estimate("release", ctx=ctx),
    ),
    ids=("compile", "check", "diff", "render", "estimate"),
)
def test_installed_mcp_refuses_legacy_authoring_evidence(
    tmp_path, monkeypatch, operation
) -> None:
    from lockstep import authoring
    from tests._authoring_gate import tree_image, write_workflow
    from tests.test_authoring_legacy_v4_refusal import (
        _locate_test_namespace,
        _retain,
        live_v4_bytes,
    )

    project = tmp_path / "project"
    project.mkdir()
    state = (tmp_path / "state").resolve()
    write_workflow(project, "release")
    authoring.publish_project_compilation(project, "release", state_dir=state)
    namespace, _identity = _locate_test_namespace(state, project)
    transaction = _retain(namespace, live_v4_bytes(project))
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state))
    before = transaction.read_bytes(); project_before, owner_before = tree_image(project), tree_image(state)

    with pytest.raises(Exception) as raised:
        operation(_ctx(project))
    error = str(raised.value)
    assert "pre-simplification" in error
    assert str(project.resolve()) in error and str(state) in error
    assert "Do not delete transaction.json manually" in error
    assert transaction.read_bytes() == before
    assert tree_image(project) == project_before and tree_image(state) == owner_before
