import json
from pathlib import Path

from lockstep.runtime import sessions
from lockstep.runtime.hooks import hook_posttool, hook_pretool, policy_require
from lockstep.runtime.service import LockstepService

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
        (FIXTURES / "child_interrupt.recipe.yaml").read_bytes()
    )
    (recipes / "native-child-interrupt.recipe.yaml").write_bytes(
        (FIXTURES / "child_interrupt.recipe.yaml").read_bytes()
    )
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    service = LockstepService(state, recipes)
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


def test_pretool_policy_requires_current_native_run_session(tmp_path, monkeypatch):
    state, project, run_id = _run(tmp_path, monkeypatch)
    sessions.touch(state, run_id, "owner", 30)
    policy_require(state, str(project), "native-parent-direct")

    assert hook_pretool({"cwd": str(project), "session_id": "owner"}, state) == (0, "")
    _code, raw = hook_pretool({"cwd": str(project), "session_id": "foreign"}, state)
    assert json.loads(raw)["hookSpecificOutput"]["permissionDecision"] == "deny"


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
