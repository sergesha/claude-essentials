"""Gate C: read commands project one captured whole-DAG observation."""

from __future__ import annotations

from pathlib import Path

import pytest

from lockstep.authoring import (
    AuthoringError,
    canonical_match,
    check_recipe,
    diff_recipe,
    project_paths,
)
from tests._authoring_gate import (
    compile_closure,
    expected_compilation_image,
    mcp_context,
    observed_compilation_image,
    replace_marker,
    write_workflow,
)


def _patch_observation_boundary(
    monkeypatch: pytest.MonkeyPatch, mutation
) -> list[str]:
    """Apply one mutation after either the legacy compile or the new plan."""

    import lockstep.authoring as authoring

    calls: list[str] = []
    mutated = False
    legacy = authoring.compile_project_source
    planned = authoring._plan_project_compilation

    def mutate_once() -> None:
        nonlocal mutated
        if not mutated:
            mutated = True
            mutation()

    def legacy_boundary(source: Path, **kwargs):
        result = legacy(source, **kwargs)
        calls.append("legacy-compile")
        mutate_once()
        return result

    def planned_boundary(recipe):
        result = planned(recipe)
        calls.append("whole-dag-plan")
        mutate_once()
        return result

    monkeypatch.setattr(authoring, "compile_project_source", legacy_boundary)
    monkeypatch.setattr(authoring, "_plan_project_compilation", planned_boundary)
    return calls


def _write_after_images(plan) -> None:
    for image in plan.bundle.after_images:
        assert image.content is not None
        image.resolved_path.write_bytes(image.content)


def test_canonical_match_uses_one_captured_transitive_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    child = write_workflow(project, "child")
    write_workflow(project, "parent", children=("child",))
    compile_closure(project, "child", "parent")
    expected_proof = canonical_match(project_paths(project, "parent"))
    destinations = expected_compilation_image(project, ("child", "parent"))

    calls = _patch_observation_boundary(
        monkeypatch, lambda: replace_marker(child, "initial", "changed")
    )
    proof = canonical_match(project_paths(project, "parent"))

    assert calls == ["whole-dag-plan"]
    assert proof.context == "canonical-match"
    assert proof.source_bundle_sha256 == expected_proof.source_bundle_sha256
    assert "description: changed" in child.read_text(encoding="utf-8")
    assert observed_compilation_image(destinations) == destinations


@pytest.mark.parametrize("adapter", ("cli", "mcp"))
def test_public_check_uses_one_captured_transitive_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adapter: str,
) -> None:
    from lockstep import cli
    from lockstep.mcp import server

    project = tmp_path / "project"
    child = write_workflow(project, "child")
    write_workflow(project, "parent", children=("child",))
    compile_closure(project, "child", "parent")
    expected_result = check_recipe(project, "parent")
    destinations = expected_compilation_image(project, ("child", "parent"))
    calls = _patch_observation_boundary(
        monkeypatch, lambda: replace_marker(child, "initial", "changed")
    )
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "owner-state"))

    if adapter == "cli":
        monkeypatch.chdir(project)
        assert cli.main(["recipe", "check", "parent"]) == 0
        captured = capsys.readouterr()
        assert captured.out == cli.json_text({"name": "parent", **expected_result})
        assert captured.err == ""
    else:
        assert server.recipe_check("parent", ctx=mcp_context(project)) == expected_result

    assert calls == ["whole-dag-plan"]
    assert "description: changed" in child.read_text(encoding="utf-8")
    assert observed_compilation_image(destinations) == destinations


@pytest.mark.parametrize("adapter", ("direct", "cli", "mcp"))
def test_diff_uses_one_captured_transitive_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adapter: str,
) -> None:
    from lockstep import cli
    from lockstep.mcp import server
    import lockstep.authoring as authoring

    project = tmp_path / "project"
    source = write_workflow(project, "leaf")
    compile_closure(project, "leaf")
    replace_marker(source, "initial", "changed")

    captured_plan = None
    original_plan = authoring._plan_project_compilation
    original_compile = authoring.compile_project_source
    calls: list[str] = []

    def planned_boundary(recipe):
        nonlocal captured_plan
        captured_plan = original_plan(recipe)
        calls.append("whole-dag-plan")
        _write_after_images(captured_plan)
        return captured_plan

    def legacy_boundary(workflow: Path, **kwargs):
        result = original_compile(workflow, **kwargs)
        calls.append("legacy-compile")
        _validated, _catalog, compiled = result
        recipe = project_paths(project, "leaf")
        recipe.recipe_path.write_bytes(compiled.recipe_bytes)
        assert recipe.dependency_path is not None
        assert recipe.source_map_path is not None
        recipe.dependency_path.write_bytes(compiled.dependency_manifest_bytes)
        recipe.source_map_path.write_bytes(compiled.source_map_bytes)
        return result

    monkeypatch.setattr(authoring, "_plan_project_compilation", planned_boundary)
    monkeypatch.setattr(authoring, "compile_project_source", legacy_boundary)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "owner-state"))

    if adapter == "direct":
        output = diff_recipe(project, "leaf")
    elif adapter == "cli":
        monkeypatch.chdir(project)
        assert cli.main(["recipe", "diff", "leaf"]) == 0
        output = capsys.readouterr().out
    else:
        output = server.recipe_diff("leaf", ctx=mcp_context(project))

    assert output
    assert str(project / ".lockstep/recipes/leaf.recipe.yaml") in output
    assert captured_plan is not None
    assert calls == ["whole-dag-plan"]


def test_parent_observers_cover_every_planned_destination(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_workflow(project, "child")
    write_workflow(project, "parent", children=("child",))
    compile_closure(project, "child", "parent")
    expected = expected_compilation_image(project, ("child", "parent"))
    assert project / ".lockstep/recipes/child.recipe.yaml" in expected

    for path, canonical in expected.items():
        for mutation in ("stale", "missing"):
            if mutation == "stale":
                path.write_bytes(canonical + b"\n# stale\n")
            else:
                path.unlink()
            try:
                with pytest.raises(
                    AuthoringError, match="canonical|byte-for-byte|missing"
                ):
                    canonical_match(project_paths(project, "parent"))
                with pytest.raises(
                    AuthoringError, match="canonical|byte-for-byte|missing"
                ):
                    check_recipe(project, "parent")
                difference = diff_recipe(project, "parent")
                assert difference
                assert str(path) in difference
            finally:
                path.write_bytes(canonical)


@pytest.mark.parametrize("observer", ("canonical", "check", "diff"))
def test_parent_observer_retains_captured_stale_child_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observer: str
) -> None:
    project = tmp_path / "project"
    write_workflow(project, "child")
    write_workflow(project, "parent", children=("child",))
    compile_closure(project, "child", "parent")
    child_dependency = project / ".lockstep/recipes/child.dependencies.json"
    canonical = child_dependency.read_bytes()
    child_dependency.write_bytes(canonical + b"\n")
    calls = _patch_observation_boundary(
        monkeypatch, lambda: child_dependency.write_bytes(canonical)
    )

    if observer == "canonical":
        with pytest.raises(AuthoringError, match="canonical|byte-for-byte|missing"):
            canonical_match(project_paths(project, "parent"))
    elif observer == "check":
        with pytest.raises(AuthoringError, match="canonical|byte-for-byte|missing"):
            check_recipe(project, "parent")
    else:
        difference = diff_recipe(project, "parent")
        assert difference
        assert str(child_dependency) in difference

    assert calls == ["whole-dag-plan"]
    assert child_dependency.read_bytes() == canonical


def test_generated_preflight_uses_candidate_from_the_same_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.runtime import service
    import lockstep.authoring as authoring

    project = tmp_path / "project"
    write_workflow(project, "leaf")
    compile_closure(project, "leaf")
    expected = service.preflight_recipe(project / ".lockstep/recipes", "leaf")

    replacement = tmp_path / "replacement"
    write_workflow(replacement, "leaf", marker="changed")
    compile_closure(replacement, "leaf")
    replacement_files = {
        path.relative_to(replacement): path.read_bytes()
        for path in sorted((replacement / ".lockstep").rglob("*"))
        if path.is_file()
    }
    original_plan = authoring._plan_project_compilation
    original_compile = authoring.compile_project_source
    plan_calls = 0
    swapped = False

    def swap_generation() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        for relative, content in replacement_files.items():
            destination = project / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)

    def swap_after_plan(recipe):
        nonlocal plan_calls
        plan = original_plan(recipe)
        plan_calls += 1
        swap_generation()
        return plan

    def swap_after_legacy_compile(workflow: Path, **kwargs):
        result = original_compile(workflow, **kwargs)
        swap_generation()
        return result

    monkeypatch.setattr(authoring, "_plan_project_compilation", swap_after_plan)
    monkeypatch.setattr(
        authoring, "compile_project_source", swap_after_legacy_compile
    )
    authorized = service.preflight_recipe(project / ".lockstep/recipes", "leaf")

    assert authorized.canonical_match_proof is not None
    assert authorized.source_bundle_sha256 == expected.source_bundle_sha256
    assert authorized.canonical_match_proof == expected.canonical_match_proof
    assert plan_calls == 1
    assert "description: changed" in (
        project / ".lockstep/workflows/leaf.workflow.yaml"
    ).read_text(encoding="utf-8")


def test_manual_check_and_diff_do_not_require_workflow_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lockstep.authoring as authoring

    project = tmp_path / "project"
    recipes = project / ".lockstep/recipes"
    recipes.mkdir(parents=True)
    (recipes / "manual.recipe.yaml").write_text(
        "name: manual\nnodes: {}\nedges: []\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        authoring,
        "_plan_project_compilation",
        lambda _recipe: (_ for _ in ()).throw(AssertionError("unexpected workflow plan")),
    )

    assert check_recipe(project, "manual") == {
        "ok": True,
        "kind": "manual",
        "errors": [],
        "warnings": [],
    }
    assert diff_recipe(project, "manual") == ""


@pytest.mark.parametrize("unsafe_leaf", ("symlink", "oversized", "deep-yaml"))
def test_preflight_classification_captures_a_bounded_regular_leaf(
    tmp_path: Path, unsafe_leaf: str
) -> None:
    from lockstep.runtime import service
    from lockstep.runtime.errors import LockstepError

    recipes = tmp_path / "project/.lockstep/recipes"
    recipes.mkdir(parents=True)
    recipe = recipes / "leaf.recipe.yaml"
    if unsafe_leaf == "symlink":
        target = tmp_path / "outside.recipe.yaml"
        target.write_text("not: [valid", encoding="utf-8")
        recipe.symlink_to(target)
        expected = "regular file"
    else:
        if unsafe_leaf == "oversized":
            recipe.write_bytes(b"x" * (1024 * 1024 + 1))
            expected = "file admission limit"
        else:
            recipe.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
            expected = "YAML depth exceeds"

    with pytest.raises(LockstepError, match=expected):
        service.preflight_recipe(recipes, "leaf")
