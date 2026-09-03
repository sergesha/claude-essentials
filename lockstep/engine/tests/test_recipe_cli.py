from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from lockstep import authoring, cli
from lockstep.mcp import server
from lockstep.runtime.service import LockstepCommandService, preflight_recipe


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
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(project.parent / "owner-state"))
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


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def _invalid_logical_name(kind: str, probe: Path) -> str:
    return {
        "traversal": "../../../escape",
        "absolute": str(probe / "absolute-escape"),
        "slash": "nested/escape",
        "backslash": r"nested\escape",
    }[kind]


def _mcp_context(project: Path) -> SimpleNamespace:
    return SimpleNamespace(
        request_context=SimpleNamespace(
            meta={"x-codex-turn-metadata": {"workspaces": {str(project): {}}}}
        )
    )


def _seed_template_recovery(project: Path) -> None:
    target = project / "owner.txt"
    target.write_bytes(b"owner bytes\n")
    journal = project / ".lockstep/.template-install.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "schema": "lockstep.template-install/v1",
                "entries": [
                    {
                        "path": "owner.txt",
                        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    }
                ],
            }
        )
    )


@pytest.mark.parametrize(
    "boundary",
    ("project_paths", "initialize_minimal", "show_template", "install_template"),
)
@pytest.mark.parametrize(
    "invalid_kind", ("traversal", "absolute", "slash", "backslash")
)
def test_direct_authoring_boundaries_reject_invalid_logical_names_without_writes(
    tmp_path: Path, boundary: str, invalid_kind: str
) -> None:
    from lockstep import templates

    probe = tmp_path / "probe"
    project = probe / "project"
    project.mkdir(parents=True)
    (probe / "owner.txt").write_bytes(b"owner bytes\n")
    before = _tree_snapshot(probe)
    name = _invalid_logical_name(invalid_kind, probe)

    with pytest.raises(authoring.AuthoringError, match="invalid workflow name"):
        if boundary == "project_paths":
            authoring.project_paths(project, name)
        elif boundary == "initialize_minimal":
            authoring.initialize_minimal(
                project,
                name,
                state_dir=(probe / "owner-state").resolve(),
            )
        elif boundary == "show_template":
            templates.show_template("reviewed-change", name)
        else:
            templates.install_template(
                "reviewed-change",
                name,
                project,
                state_dir=(probe / "owner-state").resolve(),
            )

    assert _tree_snapshot(probe) == before


def test_invalid_template_name_is_rejected_before_recovery_mutates_project(
    tmp_path: Path,
) -> None:
    from lockstep import templates

    probe = tmp_path / "probe"
    project = probe / "project"
    project.mkdir(parents=True)
    _seed_template_recovery(project)
    before = _tree_snapshot(probe)

    with pytest.raises(authoring.AuthoringError, match="invalid workflow name"):
        templates.install_template(
            "reviewed-change",
            "../../../escape",
            project,
            state_dir=(probe / "owner-state").resolve(),
        )

    assert _tree_snapshot(probe) == before


@pytest.mark.parametrize("surface", ("recipe", "template"))
@pytest.mark.parametrize(
    "invalid_kind", ("traversal", "absolute", "slash", "backslash")
)
def test_cli_authoring_rejects_invalid_logical_names_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    surface: str,
    invalid_kind: str,
) -> None:
    probe = tmp_path / "probe"
    project = probe / "project"
    project.mkdir(parents=True)
    (probe / "owner.txt").write_bytes(b"owner bytes\n")
    if surface == "template":
        _seed_template_recovery(project)
    before = _tree_snapshot(probe)
    name = _invalid_logical_name(invalid_kind, probe)
    args = (
        ("recipe", "init", name)
        if surface == "recipe"
        else ("template", "init", "reviewed-change", name)
    )

    result = _run_cli(monkeypatch, capsys, project, *args)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "invalid workflow name" in result.stderr
    assert _tree_snapshot(probe) == before


@pytest.mark.parametrize("surface", ("recipe_init", "template_show"))
@pytest.mark.parametrize(
    "invalid_kind", ("traversal", "absolute", "slash", "backslash")
)
def test_mcp_authoring_rejects_invalid_logical_names_without_writes(
    tmp_path: Path, surface: str, invalid_kind: str
) -> None:
    probe = tmp_path / "probe"
    project = probe / "project"
    project.mkdir(parents=True)
    (probe / "owner.txt").write_bytes(b"owner bytes\n")
    before = _tree_snapshot(probe)
    name = _invalid_logical_name(invalid_kind, probe)

    with pytest.raises(authoring.AuthoringError, match="invalid workflow name"):
        if surface == "recipe_init":
            server.recipe_init(name, ctx=_mcp_context(project))
        else:
            server.template_show("reviewed-change", name, ctx=_mcp_context(project))

    assert _tree_snapshot(probe) == before


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
        "peak_parallel_child_calls": 0,
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


@pytest.mark.parametrize(
    ("template", "expected"),
    (
        (
            "reviewed-change",
            {
                "compile_order": ["release-review", "release"],
                "dependencies": {
                    "release": ["release-review"],
                    "release-review": [],
                },
                "name": "release",
                "roles": {"parent": "release", "review": "release-review"},
                "sources": {
                    "parent": "parent.workflow.yaml",
                    "review": "review.workflow.yaml",
                },
                "template": "reviewed-change",
            },
        ),
        (
            "parallel-review",
            {
                "compile_order": [
                    "release-security-review",
                    "release-architecture-review",
                    "release",
                ],
                "dependencies": {
                    "release": [
                        "release-security-review",
                        "release-architecture-review",
                    ],
                    "release-security-review": [],
                    "release-architecture-review": [],
                },
                "name": "release",
                "roles": {
                    "parent": "release",
                    "security-review": "release-security-review",
                    "architecture-review": "release-architecture-review",
                },
                "sources": {
                    "parent": "parent.workflow.yaml",
                    "security-review": "security-review.workflow.yaml",
                    "architecture-review": "architecture-review.workflow.yaml",
                },
                "template": "parallel-review",
            },
        ),
    ),
)
def test_template_show_has_exact_authored_json_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    template: str,
    expected: dict,
) -> None:
    result = _run_cli(
        monkeypatch, capsys, tmp_path, "template", "show", template, "release"
    )

    assert result == _Result(
        0,
        json.dumps(expected, sort_keys=True) + "\n",
        "",
    )


@pytest.mark.parametrize("template", ("reviewed-change", "parallel-review"))
def test_template_init_has_exact_authored_text_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    template: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = _run_cli(
        monkeypatch, capsys, project, "template", "init", template, "release"
    )

    assert result == _Result(0, "initialized release\n", "")


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


def test_generated_start_rejects_stale_source_before_runtime_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = _write_minimal_workflow(project)
    assert _run_cli(monkeypatch, capsys, project, "recipe", "compile", "release").returncode == 0
    source.write_text(source.read_text().replace("terminal recipe", "stale source"))

    with pytest.raises(Exception, match="canonical match|fresh|byte-for-byte"):
        preflight_recipe(project / ".lockstep/recipes", "release")


def test_generated_marker_with_missing_declared_source_is_not_treated_as_manual(
    tmp_path: Path,
) -> None:
    recipes = tmp_path / ".lockstep/recipes"
    recipes.mkdir(parents=True)
    (recipes / "forged.recipe.yaml").write_text(
        "name: forged\n"
        "x-lockstep-generated:\n"
        "  schema: lockstep.generated/v1\n"
        "  compiler_version: '1'\n"
        "  workflow_version: '1'\n"
        "  source: ../workflows/missing.workflow.yaml\n"
        "  source_sha256: " + "a" * 64 + "\n"
        "nodes: {done: {type: passthrough}}\n"
        "edges: [{from: START, to: done}, {from: done, to: END}]\n"
    )

    with pytest.raises(Exception, match="source|generated|canonical"):
        preflight_recipe(recipes, "forged")


def test_complete_manual_yamlgraph_starts_without_a_template(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_minimal_manual(project)
    service = LockstepCommandService(tmp_path / "state", project / ".lockstep/recipes")
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
