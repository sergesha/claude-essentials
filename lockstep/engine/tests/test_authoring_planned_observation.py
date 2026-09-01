"""Read commands project one captured whole-DAG observation."""
from __future__ import annotations

from pathlib import Path

import pytest

import lockstep.authoring as authoring
from lockstep.authoring import AuthoringError, canonical_match, check_recipe, diff_recipe, project_paths
from tests._authoring_gate import compile_closure, expected_compilation_image, mcp_context, replace_marker, write_workflow


def _boundary(monkeypatch, mutation):
    original = authoring.compile_project; calls = []
    def plan(recipe):
        value = original(recipe); calls.append(value); mutation(); return value
    monkeypatch.setattr(authoring, "compile_project", plan); return calls


def _parent(tmp_path):
    project = tmp_path / "project"; child = write_workflow(project, "child"); write_workflow(project, "parent", children=("child",)); compile_closure(project, "child", "parent")
    return project, child


def test_canonical_match_uses_one_captured_transitive_plan(tmp_path, monkeypatch) -> None:
    project, child = _parent(tmp_path); expected = canonical_match(project_paths(project, "parent"))
    calls = _boundary(monkeypatch, lambda: replace_marker(child, "initial", "changed")); observed = canonical_match(project_paths(project, "parent"))
    assert len(calls) == 1 and observed.source_bundle_sha256 == expected.source_bundle_sha256
    assert "description: changed" in child.read_text()


@pytest.mark.parametrize("operation", ("compile", "estimate"))
def test_public_compilation_projections_use_one_captured_transitive_plan(tmp_path, monkeypatch, operation) -> None:
    project, child = _parent(tmp_path); parent = project / ".lockstep/workflows/parent.workflow.yaml"
    expected = authoring.compile_project_source(parent) if operation == "compile" else authoring.estimate_recipe(project, "parent")
    def mutate(): replace_marker(child, "initial", "changed")
    calls = _boundary(monkeypatch, mutate)
    observed = authoring.compile_project_source(parent) if operation == "compile" else authoring.estimate_recipe(project, "parent")
    assert observed == expected and len(calls) == 1


@pytest.mark.parametrize("adapter", ("cli", "mcp"))
def test_public_check_uses_one_captured_transitive_plan(tmp_path, monkeypatch, capsys, adapter) -> None:
    from lockstep import cli
    from lockstep.mcp import server
    project, child = _parent(tmp_path); expected = check_recipe(project, "parent")
    calls = _boundary(monkeypatch, lambda: replace_marker(child, "initial", "changed")); monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "state"))
    if adapter == "cli": monkeypatch.chdir(project); assert cli.main(["recipe", "check", "parent"]) == 0; capsys.readouterr()
    else: assert server.recipe_check("parent", ctx=mcp_context(project)) == expected
    assert len(calls) == 1 and "description: changed" in child.read_text()


@pytest.mark.parametrize("adapter", ("direct", "cli", "mcp"))
def test_diff_uses_one_captured_transitive_plan(tmp_path, monkeypatch, capsys, adapter) -> None:
    from lockstep import cli
    from lockstep.mcp import server
    project = tmp_path / "project"; source = write_workflow(project, "leaf"); compile_closure(project, "leaf"); replace_marker(source, "initial", "changed")
    calls = []
    def after_plan():
        plan = calls[-1]
        for target in plan.plan.targets: target.path.write_bytes(target.after)
    calls = _boundary(monkeypatch, after_plan); monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "state"))
    if adapter == "direct": output = diff_recipe(project, "leaf")
    elif adapter == "cli": monkeypatch.chdir(project); assert cli.main(["recipe", "diff", "leaf"]) == 0; output = capsys.readouterr().out
    else: output = server.recipe_diff("leaf", ctx=mcp_context(project))
    assert output and str(project / ".lockstep/recipes/leaf.recipe.yaml") in output and len(calls) == 1


def test_parent_observers_cover_every_planned_destination(tmp_path) -> None:
    project, _child = _parent(tmp_path); expected = expected_compilation_image(project, ("child", "parent"))
    for path, canonical in expected.items():
        for missing in (False, True):
            if missing: path.unlink()
            else: path.write_bytes(canonical + b"\n# stale\n")
            try:
                with pytest.raises(AuthoringError): canonical_match(project_paths(project, "parent"))
                with pytest.raises(AuthoringError): check_recipe(project, "parent")
                assert str(path) in diff_recipe(project, "parent")
            finally: path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(canonical)


@pytest.mark.parametrize("observer", ("canonical", "check", "diff"))
def test_parent_observer_retains_captured_stale_child_destination(tmp_path, monkeypatch, observer) -> None:
    project, _child = _parent(tmp_path); path = project / ".lockstep/recipes/child.dependencies.json"; canonical = path.read_bytes(); path.write_bytes(canonical + b"\n")
    calls = _boundary(monkeypatch, lambda: path.write_bytes(canonical))
    if observer == "canonical":
        with pytest.raises(AuthoringError): canonical_match(project_paths(project, "parent"))
    elif observer == "check":
        with pytest.raises(AuthoringError): check_recipe(project, "parent")
    else: assert str(path) in diff_recipe(project, "parent")
    assert len(calls) == 1 and path.read_bytes() == canonical


def test_generated_preflight_uses_candidate_from_same_plan(tmp_path, monkeypatch) -> None:
    from lockstep.runtime import service
    project = tmp_path / "project"; write_workflow(project, "leaf"); compile_closure(project, "leaf"); expected = service.preflight_recipe(project / ".lockstep/recipes", "leaf")
    replacement = tmp_path / "replacement"; write_workflow(replacement, "leaf", marker="changed"); compile_closure(replacement, "leaf")
    files = {path.relative_to(replacement): path.read_bytes() for path in (replacement / ".lockstep").rglob("*") if path.is_file()}; calls = []
    def swap():
        for relative, content in files.items(): target = project / relative; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(content)
    calls = _boundary(monkeypatch, swap); observed = service.preflight_recipe(project / ".lockstep/recipes", "leaf")
    assert len(calls) == 1 and observed.source_bundle_sha256 == expected.source_bundle_sha256
    assert observed.canonical_match_proof == expected.canonical_match_proof


def test_manual_check_and_diff_do_not_require_workflow_plan(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"; recipes = project / ".lockstep/recipes"; recipes.mkdir(parents=True); (recipes / "manual.recipe.yaml").write_text("name: manual\nnodes: {}\nedges: []\n")
    monkeypatch.setattr(authoring, "compile_project", lambda _recipe: (_ for _ in ()).throw(AssertionError("planned")))
    assert check_recipe(project, "manual")["ok"] is True and diff_recipe(project, "manual") == ""


@pytest.mark.parametrize(("unsafe", "message"), (("symlink", "regular file"), ("oversized", "file admission limit"), ("deep", "YAML depth exceeds")))
def test_preflight_classification_captures_bounded_regular_leaf(tmp_path, unsafe, message) -> None:
    from lockstep.runtime import service
    from lockstep.runtime.errors import LockstepError
    recipes = tmp_path / "project/.lockstep/recipes"; recipes.mkdir(parents=True); recipe = recipes / "leaf.recipe.yaml"
    if unsafe == "symlink": target = tmp_path / "outside"; target.write_text("bad"); recipe.symlink_to(target)
    elif unsafe == "oversized": recipe.write_bytes(b"x" * (1024 * 1024 + 1))
    else: recipe.write_text("[" * 2000 + "0" + "]" * 2000)
    with pytest.raises(LockstepError, match=message): service.preflight_recipe(recipes, "leaf")
