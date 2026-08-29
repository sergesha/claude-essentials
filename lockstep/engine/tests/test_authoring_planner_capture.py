"""Planning is exact, immutable, identity-bound, and write-free."""
from __future__ import annotations

import dataclasses, hashlib, stat
from pathlib import Path

import pytest

import lockstep.authoring_bundle as bundle_module
from lockstep import cli
from lockstep.authoring import project_paths
from lockstep.errors import AuthoringError
from tests._authoring_gate import compile_closure, expected_compilation_image, replace_marker, tree_image, write_workflow


def _project(tmp_path: Path):
    project = tmp_path / "project"; source = write_workflow(project, "leaf")
    return project, source, project_paths(project, "leaf")


def test_leaf_planner_captures_exact_immutable_bundle_without_writes(tmp_path: Path) -> None:
    project, source_path, recipe = _project(tmp_path); before = tree_image(tmp_path)
    plan = bundle_module.plan_project_compilation(recipe)
    assert tree_image(tmp_path) == before
    assert plan.resolved_project == project.resolve() and plan.project_identity.resolved_path == project.resolve()
    source = plan.sources[0]; info = source_path.lstat()
    assert source.resolved_path == source_path.resolve() and source.content == source_path.read_bytes()
    assert source.sha256 == hashlib.sha256(source.content).hexdigest()
    assert (source.leaf.device, source.leaf.inode, source.leaf.size) == (info.st_dev, info.st_ino, info.st_size)
    expected = expected_compilation_image(project, ("leaf",))
    assert {item.resolved_path: item.content for item in plan.after_images} == expected
    assert all(item.content is None and item.leaf is None for item in plan.before_images)
    for value, field, replacement in ((plan, "sources", ()), (source, "content", b"bad"), (source.leaf, "inode", 0), (plan.after_images[0], "content", b"bad")):
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)): setattr(value, field, replacement)


def test_leaf_planner_captures_existing_real_destination_parent_identity(tmp_path: Path) -> None:
    project, _source, recipe = _project(tmp_path); recipes = project / ".lockstep/recipes"; recipes.mkdir()
    plan = bundle_module.plan_project_compilation(recipe)
    paths = (project.resolve(), (project / ".lockstep").resolve(), recipes.resolve())
    for image in (*plan.before_images, *plan.after_images):
        assert tuple(item.resolved_path for item in image.ancestors) == paths
        assert tuple((item.device, item.inode) for item in image.ancestors) == tuple((path.stat().st_dev, path.stat().st_ino) for path in paths)


def test_leaf_replanner_captures_present_before_and_changed_after_images(tmp_path: Path) -> None:
    project, source, recipe = _project(tmp_path); compile_closure(project, "leaf")
    old = {path: (path.read_bytes(), path.lstat()) for path in expected_compilation_image(project, ("leaf",))}
    replace_marker(source, "initial", "changed"); expected = expected_compilation_image(project, ("leaf",)); before = tree_image(tmp_path)
    plan = bundle_module.plan_project_compilation(recipe)
    assert tree_image(tmp_path) == before
    for image in plan.before_images:
        content, info = old[image.resolved_path]
        assert image.content == content and image.leaf is not None
        assert (image.leaf.device, image.leaf.inode, image.leaf.mode) == (info.st_dev, info.st_ino, info.st_mode)
    assert {item.resolved_path: item.content for item in plan.after_images} == expected
    assert any(expected[path] != old[path][0] for path in expected)
    assert set(expected).isdisjoint(item.resolved_path for item in plan.sources)


def test_leaf_planner_rejects_symlinked_destination_parent(tmp_path: Path) -> None:
    project, _source, recipe = _project(tmp_path); outside = tmp_path / "outside"; outside.mkdir()
    (project / ".lockstep/recipes").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AuthoringError, match="destination ancestor"): bundle_module.plan_project_compilation(recipe)


def test_leaf_planner_rejects_destination_parent_swapped_during_capture(tmp_path, monkeypatch) -> None:
    project, _source, recipe = _project(tmp_path); recipes = project / ".lockstep/recipes"; recipes.mkdir(); original = Path.resolve; swapped = False
    def resolve(path, *args, **kwargs):
        nonlocal swapped
        if path == recipes and not swapped: swapped = True; recipes.rename(recipes.with_name("old")); recipes.mkdir()
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(AuthoringError, match="destination ancestor changed"): bundle_module.plan_project_compilation(recipe)


@pytest.mark.parametrize("kind", ("dangling", "loop"))
def test_leaf_planner_rejects_unresolvable_destination_parent_symlink(tmp_path, kind) -> None:
    project, _source, recipe = _project(tmp_path); recipes = project / ".lockstep/recipes"
    recipes.symlink_to(tmp_path / "missing" if kind == "dangling" else recipes, target_is_directory=True)
    with pytest.raises(AuthoringError, match="destination ancestor"): bundle_module.plan_project_compilation(recipe)


def test_leaf_planner_shares_stable_directory_identity_across_components(tmp_path, monkeypatch) -> None:
    project, workflow, recipe = _project(tmp_path); watched = (project, project / ".lockstep", workflow.parent, recipe.recipe_path.parent)
    original = bundle_module._directory_identity; counts = {path: 0 for path in watched}
    def capture(path):
        if path in counts: counts[path] += 1
        return original(path)
    monkeypatch.setattr(bundle_module, "_directory_identity", capture); plan = bundle_module.plan_project_compilation(recipe)
    assert counts == {path: 1 for path in watched}
    assert plan.project_identity == plan.sources[0].ancestors[0] == plan.before_images[0].ancestors[0]


@pytest.mark.parametrize("failure", ("parse", "semantic", "missing", "cycle"))
def test_compile_planning_failure_is_write_free(tmp_path, monkeypatch, capsys, failure) -> None:
    project = tmp_path / "project"; state = tmp_path / "state"; child = write_workflow(project, "child"); parent = write_workflow(project, "parent", children=("child",)); compile_closure(project, "child", "parent")
    if failure == "parse": child.write_text("not: [valid")
    elif failure == "semantic": child.write_text(child.read_text().replace("protect: ['**']", "protect: ['src/**']"))
    elif failure == "missing": child.rename(child.with_suffix(".missing"))
    else: parent.write_text(parent.read_text().replace("workflow: child", "workflow: parent"))
    before = tree_image(project); monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state)); monkeypatch.chdir(project)
    assert cli.main(["recipe", "compile", "parent"]) == 2; capsys.readouterr(); assert tree_image(project) == before
