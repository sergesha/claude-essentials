"""Planning is exact, immutable, identity-bound, and write-free."""
from __future__ import annotations

import dataclasses, hashlib, stat
from pathlib import Path

import pytest

import lockstep.authoring_compilation as compilation_module
from lockstep import cli
from lockstep.authoring import project_paths
from lockstep.errors import AuthoringError
from tests._authoring_gate import compile_closure, expected_compilation_image, replace_marker, tree_image, write_workflow


def _project(tmp_path: Path):
    project = tmp_path / "project"; source = write_workflow(project, "leaf")
    return project, source, project_paths(project, "leaf")


def test_leaf_planner_captures_exact_immutable_bundle_without_writes(tmp_path: Path) -> None:
    project, source_path, recipe = _project(tmp_path); before = tree_image(tmp_path)
    plan = compilation_module.plan_project_compilation(recipe)
    assert tree_image(tmp_path) == before
    assert plan.project == project.resolve() and plan.project_identity.path == project.resolve()
    source = plan.sources[0]; info = source_path.lstat()
    assert source.path == source_path.resolve() and source.content == source_path.read_bytes()
    assert source.sha256 == hashlib.sha256(source.content).hexdigest()
    assert (source.file.device, source.file.inode, source.file.size) == (info.st_dev, info.st_ino, info.st_size)
    expected = expected_compilation_image(project, ("leaf",))
    assert {item.path: item.after for item in plan.targets} == expected
    assert all(item.before is None and item.before_file is None for item in plan.targets)
    for value, field, replacement in ((plan, "sources", ()), (source, "content", b"bad"), (source.file, "inode", 0), (plan.targets[0], "after", b"bad")):
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)): setattr(value, field, replacement)


def test_leaf_planner_captures_existing_real_destination_parent_identity(tmp_path: Path) -> None:
    project, _source, recipe = _project(tmp_path); recipes = project / ".lockstep/recipes"; recipes.mkdir()
    plan = compilation_module.plan_project_compilation(recipe)
    paths = (project.resolve(), (project / ".lockstep").resolve(), recipes.resolve())
    for target in plan.targets:
        assert tuple(item.path for item in target.parents) == paths
        assert tuple((item.device, item.inode) for item in target.parents) == tuple((path.stat().st_dev, path.stat().st_ino) for path in paths)


def test_leaf_replanner_captures_present_before_and_changed_after_images(tmp_path: Path) -> None:
    project, source, recipe = _project(tmp_path); compile_closure(project, "leaf")
    old = {path: (path.read_bytes(), path.lstat()) for path in expected_compilation_image(project, ("leaf",))}
    replace_marker(source, "initial", "changed"); expected = expected_compilation_image(project, ("leaf",)); before = tree_image(tmp_path)
    plan = compilation_module.plan_project_compilation(recipe)
    assert tree_image(tmp_path) == before
    for target in plan.targets:
        content, info = old[target.path]
        assert target.before == content and target.before_file is not None
        assert (target.before_file.device, target.before_file.inode, target.before_file.mode) == (info.st_dev, info.st_ino, info.st_mode)
    assert {item.path: item.after for item in plan.targets} == expected
    assert any(expected[path] != old[path][0] for path in expected)
    assert set(expected).isdisjoint(item.path for item in plan.sources)


def test_leaf_planner_rejects_symlinked_destination_parent(tmp_path: Path) -> None:
    project, _source, recipe = _project(tmp_path); outside = tmp_path / "outside"; outside.mkdir()
    (project / ".lockstep/recipes").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AuthoringError, match="destination ancestor"): compilation_module.plan_project_compilation(recipe)


def test_leaf_planner_rejects_destination_parent_swapped_during_capture(tmp_path, monkeypatch) -> None:
    project, _source, recipe = _project(tmp_path); recipes = project / ".lockstep/recipes"; recipes.mkdir(); original = Path.resolve; swapped = False
    def resolve(path, *args, **kwargs):
        nonlocal swapped
        if path == recipes and not swapped: swapped = True; recipes.rename(recipes.with_name("old")); recipes.mkdir()
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(AuthoringError, match="destination ancestor changed"): compilation_module.plan_project_compilation(recipe)


@pytest.mark.parametrize("kind", ("dangling", "loop"))
def test_leaf_planner_rejects_unresolvable_destination_parent_symlink(tmp_path, kind) -> None:
    project, _source, recipe = _project(tmp_path); recipes = project / ".lockstep/recipes"
    recipes.symlink_to(tmp_path / "missing" if kind == "dangling" else recipes, target_is_directory=True)
    with pytest.raises(AuthoringError, match="destination ancestor"): compilation_module.plan_project_compilation(recipe)


def test_leaf_planner_shares_stable_directory_identity_across_components(tmp_path, monkeypatch) -> None:
    project, workflow, recipe = _project(tmp_path); watched = (project, project / ".lockstep", workflow.parent, recipe.recipe_path.parent)
    original = compilation_module.capture_directory; counts = {path: 0 for path in watched}
    def capture(path, *, label):
        if path in counts: counts[path] += 1
        return original(path, label=label)
    monkeypatch.setattr(compilation_module, "capture_directory", capture); plan = compilation_module.plan_project_compilation(recipe)
    assert counts == {path: 1 for path in watched}
    assert plan.project_identity == plan.sources[0].parents[0] == plan.targets[0].parents[0]


@pytest.mark.parametrize("failure", ("parse", "semantic", "missing", "cycle"))
def test_compile_planning_failure_is_write_free(tmp_path, monkeypatch, capsys, failure) -> None:
    project = tmp_path / "project"; state = tmp_path / "state"; child = write_workflow(project, "child"); parent = write_workflow(project, "parent", children=("child",)); compile_closure(project, "child", "parent")
    if failure == "parse": child.write_text("not: [valid")
    elif failure == "semantic": child.write_text(child.read_text().replace("protect: ['**']", "protect: ['src/**']"))
    elif failure == "missing": child.rename(child.with_suffix(".missing"))
    else: parent.write_text(parent.read_text().replace("workflow: child", "workflow: parent"))
    before = tree_image(project); monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state)); monkeypatch.chdir(project)
    assert cli.main(["recipe", "compile", "parent"]) == 2; capsys.readouterr(); assert tree_image(project) == before
