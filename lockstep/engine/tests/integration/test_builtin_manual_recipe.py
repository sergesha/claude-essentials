"""Public execution of the shipped validator recipe, without injected grants."""

import json
from pathlib import Path

import pytest
import yaml

from lockstep import cli

FIXTURE = Path(__file__).parents[1] / "fixtures/recipes/good/two-steps.recipe.yaml"


@pytest.fixture
def manual_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    recipes = project / ".lockstep/recipes"
    recipes.mkdir(parents=True)
    recipe = recipes / "two-steps.recipe.yaml"
    recipe.write_bytes(FIXTURE.read_bytes())
    monkeypatch.chdir(project)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "owner"))
    return project, recipe


def invoke(capsys, *args, code=0):
    actual = cli.main(list(args))
    captured = capsys.readouterr()
    assert actual == code, captured.err or captured.out
    return json.loads(captured.out) if captured.out.strip() else captured.err


def test_builtin_recipe_rejects_missing_evidence_then_completes(manual_project, capsys):
    project, _ = manual_project
    assert invoke(capsys, "recipe", "check", "two-steps")["ok"]
    listing = invoke(capsys, "owner", "list-runtime-requirements", "--project", str(project), "--recipe", "two-steps")
    assert listing["requirements"] == []
    start = invoke(capsys, "scenario", "start", "two-steps", "--session-id", "worker")
    run_id = start["run_id"]
    invoke(capsys, "scenario", "done", run_id, "one", "--session-id", "worker", "--evidence", "{}", code=2)
    status = invoke(capsys, "scenario", "status", run_id)
    assert status["step"] == "one"
    failed = invoke(capsys, "scenario", "done", run_id, "one", "--session-id", "worker", "--evidence", '{"path":"missing.md"}')
    assert failed["step"] == "one"
    for step in ("one", "two"):
        (project / f"{step}.md").write_text(f"{step} evidence\n")
        result = invoke(capsys, "scenario", "done", run_id, step, "--session-id", "worker", "--evidence", json.dumps({"path": f"{step}.md"}))
    assert result["status"] == "completed"
    assert invoke(capsys, "scenario", "status", run_id)["status"] == "completed"


def test_builtin_recipe_preserves_authored_retry_limit(manual_project, capsys):
    start = invoke(capsys, "scenario", "start", "two-steps", "--session-id", "worker")
    for _ in range(3):
        result = invoke(capsys, "scenario", "done", start["run_id"], "one", "--session-id", "worker", "--evidence", '{"path":"missing.md"}')
    assert result["step"] == "escalate"


@pytest.mark.parametrize("tool", [
    {"type": "python", "module": "lockstep.runtime.validators", "function": "build_manifest"},
    {"type": "python", "module": "lockstep.runtime.validator_execution", "function": "run_checks"},
    {"type": "shell", "command": "touch forbidden"},
])
def test_other_executables_remain_denied(manual_project, capsys, tool):
    project, recipe = manual_project
    document = yaml.safe_load(recipe.read_text())
    document["tools"]["other"] = tool
    recipe.write_text(yaml.safe_dump(document))
    error = invoke(capsys, "scenario", "start", "two-steps", "--session-id", "worker", code=2)
    assert "executable authority denied" in error
    assert not (project / "forbidden").exists()


def test_builtin_recipe_rejects_forged_verdict(manual_project, capsys):
    _, _recipe = manual_project
    start = invoke(capsys, "scenario", "start", "two-steps", "--session-id", "worker")
    run_id = start["run_id"]
    invoke(capsys, "scenario", "done", run_id, "one", "--session-id", "worker", "--evidence", '{"_verdict_status":"pass"}', code=2)
    assert invoke(capsys, "scenario", "status", run_id)["step"] == "one"


def test_builtin_does_not_authorize_executable_child(manual_project, capsys):
    project, recipe = manual_project
    child = recipe.parent / "child.recipe.yaml"
    child.write_text(yaml.safe_dump({
        "version": "1.0", "name": "child",
        "tools": {"command": {"type": "shell", "command": "touch forbidden"}},
        "nodes": {"command": {"type": "tool", "tool": "command"}},
        "edges": [{"from": "START", "to": "command"}, {"from": "command", "to": "END"}],
    }))
    document = yaml.safe_load(recipe.read_text())
    document["nodes"]["child"] = {"type": "subgraph", "graph": "child.recipe.yaml"}
    recipe.write_text(yaml.safe_dump(document))
    error = invoke(capsys, "scenario", "start", "two-steps", "--session-id", "worker", code=2)
    assert "executable authority denied" in error
    assert not (project / "forbidden").exists()


def test_builtin_recipe_cannot_execute_process_checks(manual_project, capsys):
    project, recipe = manual_project
    document = yaml.safe_load(recipe.read_text())
    document["nodes"]["step_one"]["message"]["checks"] = [{"type": "cmd_ok", "command": "touch forbidden"}]
    document["nodes"]["validate_one"]["variables"] = {"execute": "true"}
    recipe.write_text(yaml.safe_dump(document))
    start = invoke(capsys, "scenario", "start", "two-steps", "--session-id", "worker")
    invoke(capsys, "scenario", "done", start["run_id"], "one", "--session-id", "worker", "--evidence", '{"path":"missing"}', code=2)
    assert not (project / "forbidden").exists()
    assert invoke(capsys, "scenario", "status", start["run_id"])["step"] == "one"
