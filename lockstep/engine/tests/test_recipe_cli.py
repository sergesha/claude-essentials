from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from lockstep import cli
from lockstep.mcp import server
from lockstep.runtime.service import LockstepService, preflight_recipe


@dataclass(frozen=True)
class _Result:
    returncode: int
    stdout: str
    stderr: str


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    project: Path,
    *args: str,
) -> _Result:
    monkeypatch.chdir(project)
    try:
        returncode = cli.main(list(args))
    except SystemExit as exc:
        returncode = int(exc.code)
    captured = capsys.readouterr()
    return _Result(returncode, captured.out, captured.err)


def _write_minimal_workflow(project: Path, name: str = "release") -> Path:
    workflows = project / ".lockstep/workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    source = workflows / f"{name}.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\n"
        f"name: {name}\n"
        "description: terminal recipe\n"
        "protect: ['**']\n"
        "flow:\n"
        "  - escalate: {}\n"
    )
    return source


def _write_minimal_manual(project: Path, name: str = "manual") -> Path:
    recipes = project / ".lockstep/recipes"
    recipes.mkdir(parents=True, exist_ok=True)
    recipe = recipes / f"{name}.recipe.yaml"
    recipe.write_text(
        f"name: {name}\n"
        "nodes:\n"
        "  done: {type: passthrough}\n"
        "edges:\n"
        "  - {from: START, to: done}\n"
        "  - {from: done, to: END}\n"
    )
    return recipe


def test_recipe_init_compile_check_diff_render_and_estimate_are_public_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    initialized = _run_cli(monkeypatch, capsys, project, "recipe", "init", "release")
    assert initialized == _Result(0, "initialized release\n", "")
    source = project / ".lockstep/workflows/release.workflow.yaml"
    recipe = project / ".lockstep/recipes/release.recipe.yaml"
    assert source.is_file()
    assert recipe.is_file()

    before_check = {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    assert _run_cli(monkeypatch, capsys, project, "recipe", "compile", "release").returncode == 0
    assert _run_cli(monkeypatch, capsys, project, "recipe", "check", "release").returncode == 0
    assert _run_cli(monkeypatch, capsys, project, "recipe", "check", "--all").returncode == 0
    assert _run_cli(monkeypatch, capsys, project, "recipe", "diff", "release").returncode == 0
    assert _run_cli(
        monkeypatch, capsys, project, "recipe", "render", "release", "--view", "workflow"
    ).returncode == 0
    assert _run_cli(
        monkeypatch, capsys, project, "recipe", "render", "release", "--view", "generated"
    ).returncode == 0
    assert _run_cli(monkeypatch, capsys, project, "recipe", "estimate", "release").returncode == 0

    after_read_only_commands = {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    assert after_read_only_commands == before_check


def test_manual_recipe_detection_rejects_compile_and_workflow_render_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manual = _write_minimal_manual(project)
    original = manual.read_bytes()

    compiled = _run_cli(monkeypatch, capsys, project, "recipe", "compile", "manual")
    rendered = _run_cli(
        monkeypatch, capsys, project, "recipe", "render", "manual", "--view", "workflow"
    )

    assert compiled == _Result(
        2,
        "",
        "manual yamlgraph recipe 'manual' has no generated output to compile\n",
    )
    assert rendered == _Result(
        2,
        "",
        "manual yamlgraph recipe 'manual' has no Workflow DSL view\n",
    )
    assert manual.read_bytes() == original


def test_recipe_estimate_json_is_the_exact_normative_schema_for_manual_yamlgraph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_minimal_manual(project)

    result = _run_cli(
        monkeypatch, capsys, project, "recipe", "estimate", "manual", "--json"
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "schema": "lockstep.structural-estimate/v1",
        "user_work_steps": 0,
        "maximum_validator_submissions": 0,
        "pinned_commands": 0,
        "child_calls": 0,
        "maximum_child_calls": 0,
        "peak_parallel_branches": 0,
        "peak_parallel_subcalls": 0,
        "maximum_runner_timeout_seconds": None,
        "generated_node_count": 1,
        "expanded_fragment_count": 0,
        "controlled_time": {
            "available": True,
            "upper_bound_seconds": 0,
            "formula": "0s",
            "assumptions": ["configured runner timeouts are enforced"],
            "unavailable_reasons": [],
        },
        "end_to_end_wall_time": {
            "available": False,
            "reason": "human and external-agent completion time is unbounded",
        },
        "tokens": {
            "available": False,
            "reason": "owner-controlled runner metadata is unavailable",
            "assumptions": [],
        },
        "money": {
            "available": False,
            "reason": "owner-controlled runner metadata is unavailable",
            "assumptions": [],
        },
    }


def test_template_list_has_exact_stable_text_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _run_cli(monkeypatch, capsys, tmp_path, "template", "list")

    assert result == _Result(0, "parallel-review\nreviewed-change\n", "")


def test_generated_start_preflight_mints_canonical_match_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_minimal_workflow(project)
    compiled = _run_cli(monkeypatch, capsys, project, "recipe", "compile", "release")
    assert compiled.returncode == 0

    authorized = preflight_recipe(project / ".lockstep/recipes", "release")

    assert authorized.canonical_match_proof.context == "canonical-match"
    assert authorized.canonical_match_proof.source_bundle_sha256 == authorized.source_bundle_sha256


def test_complete_manual_yamlgraph_starts_without_a_template(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_minimal_manual(project)
    service = LockstepService(tmp_path / "state", project / ".lockstep/recipes")
    try:
        result = service.start("manual", {}, str(project))
    finally:
        service.close()

    assert result == {
        "status": "completed",
        "run_id": result["run_id"],
        "owner": "engine",
        "next_action": None,
    }


def test_mcp_exposes_authoring_wait_event_and_acceptance_surfaces() -> None:
    names = {tool.name for tool in server.app._tool_manager.list_tools()}

    assert {
        "recipe_init",
        "recipe_compile",
        "recipe_check",
        "recipe_diff",
        "recipe_render",
        "recipe_estimate",
        "template_list",
        "template_show",
        "scenario_wait",
        "scenario_events",
        "scenario_accept_artifact",
    } <= names
