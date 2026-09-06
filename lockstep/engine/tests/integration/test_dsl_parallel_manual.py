"""Authored parallel manual workflows execute through fresh public services."""

from pathlib import Path

import pytest

from lockstep.authoring import publish_project_compilation
from lockstep.runtime import sessions
from lockstep.runtime.engine import Engine


def _start(tmp_path: Path):
    project = tmp_path / "project"
    source = project / ".lockstep/workflows/parallel.workflow.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(
        "workflow_version: '1'\nname: parallel\ndescription: Parallel editing\n"
        "protect: ['**']\nflow:\n"
        "  - parallel:\n      id: edit\n      join: all\n      branches:\n"
        "        alpha:\n"
        "          - {step: alpha-first, task: Write alpha, exit: Written, writes: [alpha.txt]}\n"
        "          - {step: alpha-second, task: Refine alpha, exit: Refined, writes: [alpha.txt]}\n"
        "        beta:\n"
        "          - {step: beta, task: Write beta, exit: Written, writes: [beta.txt]}\n"
        + "".join(
            f"  - decide:\n      id: has-{name}\n      using:\n"
            "        type: changed-paths\n        since: start\n"
            f"        cases: {{present: [{name}.txt]}}\n        default: missing\n"
            f"  - choose:\n      value: has-{name}\n"
            "      cases: {present: []}\n      default: [{escalate: {}}]\n"
            for name in ("alpha", "beta")
        )
        + "  - {step: joined, task: Confirm joined work, exit: Confirmed}\n"
    )
    recipes = project / ".lockstep/recipes"
    state = tmp_path / "state"
    publish_project_compilation(project, "parallel", state_dir=state)
    command = Engine.command(state, recipes)
    try:
        started = command.start("parallel", {}, str(project))
    finally:
        command.close()
    sessions.touch(state, started["run_id"], "worker", 300)
    return project, recipes, state, started


def _done(paths, step):
    project, recipes, state, started = paths
    command = Engine.command(state, recipes)
    try:
        return command.done(
            started["run_id"], step, {}, session_id="worker", project=str(project)
        )
    finally:
        command.close()


@pytest.mark.parametrize("order", [
    ("beta", "alpha-first", "alpha-second"),
    ("alpha-first", "beta", "alpha-second"),
])
def test_parallel_manual_restart_preserves_both_results_at_join(tmp_path: Path, order):
    paths = _start(tmp_path)
    project, recipes, state, started = paths
    assert {item["step"] for item in started["parallel_progress"]["steps"]} == {"alpha-first", "beta"}
    (project / "alpha.txt").write_text("first alpha")
    (project / "beta.txt").write_text("finished beta")
    for index, step in enumerate(order):
        if step == "alpha-second":
            (project / "alpha.txt").write_text("refined alpha")
        result = _done(paths, step)
        if index < len(order) - 1:
            assert result["status"] == "awaiting"
            assert result["step"] == order[index + 1]
        else:
            assert result["step"] == "joined"
    assert _done(paths, "joined")["status"] == "completed"
    assert Engine.observe(state, recipes).status(started["run_id"], str(project))["status"] == "completed"


@pytest.mark.parametrize("mutation", ["outside", "symlink", "declared-symlink"])
def test_parallel_manual_integrity_failure_settles_without_snapshot_crash(tmp_path: Path, mutation: str):
    paths = _start(tmp_path)
    project = paths[0]
    if mutation == "outside":
        (project / "undeclared.txt").write_text("invalid")
    else:
        (project / ("alpha.txt" if mutation == "declared-symlink" else "link")).symlink_to("beta.txt")
    assert _done(paths, "alpha-first")["step"] == "beta"
    assert _done(paths, "beta")["status"] == "escalated"
    project, recipes, state, started = paths
    effects = [event for event in Engine.observe(state, recipes).events(
        started["run_id"], str(project)
    ) if event["source"] == "effect"]
    assert len(effects) == 2
    assert all(event.get("fixed_error_code") == "manifest_invalid" for event in effects)
    assert all(event["phase"] == "delivered" for event in effects)
