import json
from pathlib import Path

import pytest

from lockstep.runtime import sessions
from lockstep.runtime.engine import Engine, LockstepError
from lockstep.runtime.hooks import hook_posttool, hook_pretool, policy_require
from lockstep.runtime.service import LockstepCommandService

FIXTURES = Path(__file__).parent / "fixtures" / "native"


def _run(tmp_path, monkeypatch=None):
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    if monkeypatch is not None:
        monkeypatch.setenv("LOCKSTEP_RECIPES", str(recipes))
    (recipes / "native-parent-direct.recipe.yaml").write_bytes(
        (FIXTURES / "parent_direct.recipe.yaml").read_bytes()
    )
    (recipes / "child_interrupt.recipe.yaml").write_bytes(
        (FIXTURES / "worker_child_interrupt.recipe.yaml").read_bytes()
    )
    (recipes / "native-child-interrupt.recipe.yaml").write_bytes(
        (FIXTURES / "worker_child_interrupt.recipe.yaml").read_bytes()
    )
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    service = LockstepCommandService(state, recipes)
    run_id = service.start("native-parent-direct", {}, str(project))["run_id"]
    service.close()
    return state, project, run_id


def test_posttool_binds_only_real_native_awaiting_run(tmp_path):
    state, _project, run_id = _run(tmp_path)
    hook_posttool(
        {
            "tool_name": "mcp__lockstep__scenario_start",
            "session_id": "session-1",
            "tool_response": {
                "run_id": run_id,
                sessions.BINDING_MARKER_KEY: sessions.BINDING_MARKER_VALUE,
            },
        },
        state,
    )
    assert sessions.read_binding(state, run_id)["session_id"] == "session-1"


@pytest.mark.parametrize(
    "tool_response",
    (
        {"isError": True, "message": "start failed"},
        {
            "isError": True,
            "run_id": "VICTIM",
            sessions.BINDING_MARKER_KEY: sessions.BINDING_MARKER_VALUE,
        },
        {"run_id": "VICTIM"},
    ),
)
def test_posttool_never_adopts_input_run_id_without_matching_marked_response(
    tmp_path, tool_response
):
    state, _project, victim_run_id = _run(tmp_path)
    response = {
        key: victim_run_id if value == "VICTIM" else value
        for key, value in tool_response.items()
    }

    hook_posttool(
        {
            "tool_name": "mcp__lockstep__scenario_start",
            "session_id": "attacker",
            "tool_input": {"run_id": victim_run_id},
            "tool_response": response,
        },
        state,
    )

    assert sessions.read_binding(state, victim_run_id) is None


def test_posttool_rejects_input_response_mismatch_between_two_real_runs(tmp_path):
    state, project, input_run_id = _run(tmp_path)
    service = LockstepCommandService(state, tmp_path / "recipes")
    try:
        response_run_id = service.start(
            "native-parent-direct", {}, str(project)
        )["run_id"]
    finally:
        service.close()
    assert response_run_id != input_run_id

    hook_posttool(
        {
            "tool_name": "mcp__lockstep__scenario_start",
            "session_id": "attacker",
            "tool_input": {"run_id": input_run_id},
            "tool_response": {
                "run_id": response_run_id,
                sessions.BINDING_MARKER_KEY: sessions.BINDING_MARKER_VALUE,
            },
        },
        state,
    )

    assert sessions.read_binding(state, input_run_id) is None
    assert sessions.read_binding(state, response_run_id) is None


def test_posttool_status_never_refreshes_or_adopts_binding(tmp_path):
    state, _project, run_id = _run(tmp_path)
    sessions.touch(state, run_id, "original", 30)
    path = sessions.binding_path(state, run_id)
    binding = json.loads(path.read_text())
    binding["last_seen"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(binding, sort_keys=True))
    before = path.read_bytes()

    hook_posttool(
        {
            "tool_name": "mcp__lockstep__scenario_status",
            "session_id": "replacement",
            "tool_input": {"run_id": run_id},
            "tool_response": {
                "run_id": run_id,
                sessions.BINDING_MARKER_KEY: sessions.BINDING_MARKER_VALUE,
            },
        },
        state,
    )
    assert path.read_bytes() == before


def test_oversize_session_identity_cannot_poison_existing_binding(tmp_path) -> None:
    state = tmp_path / "state"
    sessions.touch(state, "run-1", "original", 30)
    path = sessions.binding_path(state, "run-1")
    binding = json.loads(path.read_text())
    binding["last_seen"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(binding, sort_keys=True))
    before = path.read_bytes()

    with pytest.raises(ValueError, match="session identity exceeds"):
        sessions.touch(
            state,
            "run-1",
            "x" * (64 * 1024 + 1),
            30,
        )

    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".tmp").exists()


def test_pretool_policy_requires_current_native_run_session(tmp_path, monkeypatch):
    state, project, run_id = _run(tmp_path, monkeypatch)
    sessions.touch(state, run_id, "owner", 30)
    policy_require(state, str(project), "native-parent-direct")

    assert hook_pretool({"cwd": str(project), "session_id": "owner"}, state) == (0, "")
    _code, raw = hook_pretool({"cwd": str(project), "session_id": "foreign"}, state)
    assert json.loads(raw)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_cannot_revive_expired_exact_owner_or_enable_resume(
    tmp_path, monkeypatch
):
    state, project, run_id = _run(tmp_path, monkeypatch)
    sessions.touch(state, run_id, "expired-owner", 30)
    policy_require(state, str(project), "native-parent-direct")
    path = sessions.binding_path(state, run_id)
    binding = json.loads(path.read_text())
    binding["last_seen"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(binding, sort_keys=True))
    before = path.read_bytes()

    _code, raw = hook_pretool(
        {"cwd": str(project), "session_id": "expired-owner"}, state
    )
    assert json.loads(raw)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert path.read_bytes() == before

    service = LockstepCommandService(state, tmp_path / "recipes")
    with pytest.raises(LockstepError, match="stale"):
        service.scenario_done(
            run_id,
            "answer",
            {"answer": "yes"},
            session_id="expired-owner",
            project=str(project),
        )
    assert Engine.observe(state, tmp_path / "recipes").status(
        run_id, str(project)
    )["status"] == "awaiting"
    service.close()


def test_pretool_policy_binds_exact_transitive_recipe_digest(tmp_path, monkeypatch):
    state, project, run_id = _run(tmp_path, monkeypatch)
    sessions.touch(state, run_id, "owner", 30)
    policy_require(state, str(project), "native-parent-direct")
    assert hook_pretool({"cwd": str(project), "session_id": "owner"}, state) == (0, "")

    child = tmp_path / "recipes" / "child_interrupt.recipe.yaml"
    child.write_text(child.read_text() + "\ndescription: changed definition\n")
    policy_require(state, str(project), "native-parent-direct")

    _code, raw = hook_pretool({"cwd": str(project), "session_id": "owner"}, state)
    decision = json.loads(raw)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "start recipe native-parent-direct" in decision["permissionDecisionReason"]


def test_pretool_uses_most_specific_policy_and_exact_policy_project(tmp_path, monkeypatch):
    state, parent, parent_run = _run(tmp_path, monkeypatch)
    child = parent / "child"
    child.mkdir()
    sessions.touch(state, parent_run, "owner", 30)
    policy_require(state, str(parent), "native-parent-direct")
    policy_require(state, str(child), "native-child-interrupt")

    _code, raw = hook_pretool({"cwd": str(child), "session_id": "owner"}, state)
    decision = json.loads(raw)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "start recipe native-child-interrupt" in decision["permissionDecisionReason"]

def test_pretool_does_not_reuse_parent_run_for_child_policy(tmp_path, monkeypatch):
    state, parent, parent_run = _run(tmp_path, monkeypatch)
    child = parent / "child"
    child.mkdir()
    sessions.touch(state, parent_run, "owner", 30)
    policy_require(state, str(child), "native-parent-direct")

    _code, raw = hook_pretool(
        {"cwd": str(child / "nested"), "session_id": "owner"}, state
    )
    decision = json.loads(raw)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "start recipe native-parent-direct" in decision["permissionDecisionReason"]


def test_native_child_uses_no_public_child_credential_environment(tmp_path, monkeypatch):
    _state, _project, _run_id = _run(tmp_path)
    monkeypatch.delenv("LOCKSTEP_CHILD_RUN", raising=False)
    monkeypatch.delenv("LOCKSTEP_CHILD_NONCE", raising=False)
    assert "LOCKSTEP_CHILD_RUN" not in dict(__import__("os").environ)
    assert "LOCKSTEP_CHILD_NONCE" not in dict(__import__("os").environ)
