"""Capture and identity invariants for one immutable whole-DAG plan."""

from __future__ import annotations

import hashlib
import stat
from os import stat_result
from pathlib import Path

import pytest

from lockstep.authoring import (
    AuthoringError,
    project_paths,
)

from tests._authoring_gate import (
    assert_source_identity,
    compile_closure,
    expected_compilation_image,
    replace_marker,
    tree_image,
    write_workflow,
)


def _assert_leaf_source_identity(bundle, project: Path, source_path: Path) -> None:
    project_info = project.lstat()
    assert bundle.resolved_project == project.resolve()
    assert (bundle.project_identity.device, bundle.project_identity.inode) == (
        project_info.st_dev,
        project_info.st_ino,
    )
    assert tuple(source.role for source in bundle.sources) == ("leaf",)
    source = bundle.sources[0]
    assert_source_identity(source, project, source_path)
    assert bundle.dependency_edges == (("leaf", ()),)


def _assert_identity_paths(identities, expected_paths: tuple[Path, ...]) -> None:
    assert tuple(item.resolved_path for item in identities) == expected_paths
    assert tuple((item.device, item.inode) for item in identities) == tuple(
        (path.lstat().st_dev, path.lstat().st_ino) for path in expected_paths
    )


def _assert_present_before_images(
    bundle, project: Path, old_destinations: dict[Path, tuple[bytes, stat_result]]
) -> None:
    before_by_path = {
        image.resolved_path: image for image in bundle.before_images
    }
    assert {
        path: image.content for path, image in before_by_path.items()
    } == {path: old[0] for path, old in old_destinations.items()}
    expected_ancestors = (
        project.resolve(),
        (project / ".lockstep").resolve(),
        (project / ".lockstep/recipes").resolve(),
    )
    for path, (old_bytes, old_info) in old_destinations.items():
        image = before_by_path[path]
        assert image.sha256 == hashlib.sha256(old_bytes).hexdigest()
        assert image.mode == stat.S_IMODE(old_info.st_mode)
        assert image.leaf is not None
        assert (
            image.leaf.resolved_path,
            image.leaf.device,
            image.leaf.inode,
            image.leaf.mode,
            image.leaf.size,
            image.leaf.mtime_ns,
        ) == (
            path,
            old_info.st_dev,
            old_info.st_ino,
            old_info.st_mode,
            old_info.st_size,
            old_info.st_mtime_ns,
        )
        _assert_identity_paths(image.ancestors, expected_ancestors)


def _assert_leaf_destination_images(
    bundle, project: Path, expected: dict[Path, bytes]
) -> None:
    before_paths = tuple(image.resolved_path for image in bundle.before_images)
    after_paths = tuple(image.resolved_path for image in bundle.after_images)
    assert len(before_paths) == len(set(before_paths)) == len(expected)
    assert len(after_paths) == len(set(after_paths)) == len(expected)
    assert before_paths == after_paths
    assert {image.resolved_path: image.content for image in bundle.after_images} == expected
    assert all(
        image.content is None
        and image.sha256 is None
        and image.mode is None
        and image.leaf is None
        for image in bundle.before_images
    )
    assert all(
        image.content is not None
        and image.sha256 == hashlib.sha256(image.content).hexdigest()
        and image.mode == 0o644
        and image.leaf is None
        for image in bundle.after_images
    )
    expected_ancestors = (project.resolve(), (project / ".lockstep").resolve())
    for before, after in zip(bundle.before_images, bundle.after_images, strict=True):
        _assert_identity_paths(before.ancestors, expected_ancestors)
        _assert_identity_paths(after.ancestors, expected_ancestors)


def _assert_leaf_bundle_is_deeply_immutable(bundle) -> None:
    source = bundle.sources[0]
    original = (
        bundle.sources,
        bundle.dependency_edges,
        bundle.before_images,
        bundle.after_images,
        source,
    )
    for target, attribute, replacement in (
        (bundle, "sources", ()),
        (bundle.project_identity, "inode", bundle.project_identity.inode + 1),
        (
            source.ancestors[0],
            "inode",
            source.ancestors[0].inode + 1,
        ),
        (source, "content", b"changed"),
        (source.leaf, "inode", source.leaf.inode + 1),
        (bundle.before_images[0], "content", b"changed"),
        (bundle.after_images[0], "content", b"changed"),
    ):
        with pytest.raises((AttributeError, TypeError)):
            setattr(target, attribute, replacement)
    assert original == (
        bundle.sources,
        bundle.dependency_edges,
        bundle.before_images,
        bundle.after_images,
        source,
    )


def test_leaf_planner_captures_exact_immutable_bundle_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.authoring_bundle import (
        ProjectCompilationBundle,
        plan_project_compilation,
    )

    project = tmp_path / "project"
    source_path = write_workflow(project, "leaf")
    controlled_cwd = tmp_path / "controlled-cwd"
    controlled_cwd.mkdir()
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "owner-state"))
    monkeypatch.chdir(controlled_cwd)
    before = tree_image(tmp_path)

    bundle = plan_project_compilation(project_paths(project, "leaf"))

    assert isinstance(bundle, ProjectCompilationBundle)
    assert tree_image(tmp_path) == before
    assert isinstance(bundle.sources, tuple)
    assert isinstance(bundle.dependency_edges, tuple)
    assert isinstance(bundle.before_images, tuple)
    assert isinstance(bundle.after_images, tuple)
    _assert_leaf_source_identity(bundle, project, source_path)
    full_expected = expected_compilation_image(project, ("leaf",))
    expected_paths = {
        (project / ".lockstep/recipes/leaf.recipe.yaml").resolve(),
        (project / ".lockstep/recipes/leaf.dependencies.json").resolve(),
        (project / ".lockstep/recipes/leaf.source-map.json").resolve(),
    }
    assert set(full_expected) == expected_paths
    _assert_leaf_destination_images(bundle, project, full_expected)
    assert {
        image.resolved_path for image in bundle.after_images
    } == expected_paths
    _assert_leaf_bundle_is_deeply_immutable(bundle)


def test_leaf_planner_captures_existing_real_destination_parent_identity(
    tmp_path: Path,
) -> None:
    from lockstep.authoring_bundle import plan_project_compilation

    project = tmp_path / "project"
    write_workflow(project, "leaf")
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir()

    bundle = plan_project_compilation(project_paths(project, "leaf"))

    expected = (project.resolve(), (project / ".lockstep").resolve(), recipes.resolve())
    for image in (*bundle.before_images, *bundle.after_images):
        _assert_identity_paths(image.ancestors, expected)


def test_leaf_replanner_captures_present_before_images_and_changed_after_images(
    tmp_path: Path,
) -> None:
    from lockstep.authoring_bundle import plan_project_compilation

    project = tmp_path / "project"
    source_path = write_workflow(project, "leaf")
    compile_closure(project, "leaf")
    recipe = project_paths(project, "leaf")
    exact_paths = (
        recipe.recipe_path.resolve(),
        recipe.dependency_path.resolve(),
        recipe.source_map_path.resolve(),
    )
    old_destinations = {}
    for path in exact_paths:
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode)
        old_destinations[path] = (path.read_bytes(), info)
    replace_marker(source_path, "initial", "changed")
    new_canonical = expected_compilation_image(project, ("leaf",))
    assert tuple(new_canonical) == exact_paths
    changed_paths = {
        path
        for path in exact_paths
        if new_canonical[path] != old_destinations[path][0]
    }
    assert changed_paths
    before_tree = tree_image(tmp_path)

    second_plan = plan_project_compilation(project_paths(project, "leaf"))

    assert tree_image(tmp_path) == before_tree
    _assert_present_before_images(second_plan, project, old_destinations)
    after_by_path = {
        image.resolved_path: image.content for image in second_plan.after_images
    }
    assert after_by_path == new_canonical
    assert changed_paths == {
        path for path in after_by_path if after_by_path[path] != old_destinations[path][0]
    }
    write_paths = set(after_by_path)
    assert write_paths == set(exact_paths)
    assert write_paths.isdisjoint(
        source.resolved_path for source in second_plan.sources
    )


def test_leaf_planner_rejects_symlinked_destination_parent(tmp_path: Path) -> None:
    from lockstep.authoring_bundle import plan_project_compilation

    project = tmp_path / "project"
    write_workflow(project, "leaf")
    target = tmp_path / "outside-recipes"
    target.mkdir()
    (project / ".lockstep" / "recipes").symlink_to(target, target_is_directory=True)

    with pytest.raises(AuthoringError, match="destination ancestor"):
        plan_project_compilation(project_paths(project, "leaf"))


def test_leaf_planner_rejects_destination_parent_swapped_during_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lockstep.authoring_bundle as bundle_module

    project = tmp_path / "project"
    write_workflow(project, "leaf")
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir()
    original_resolve = Path.resolve
    swapped = False

    def swap_during_resolve(path: Path, *args, **kwargs) -> Path:
        nonlocal swapped
        if path == recipes and not swapped:
            swapped = True
            recipes.rename(recipes.with_name("recipes-old"))
            recipes.mkdir()
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", swap_during_resolve)

    with pytest.raises(AuthoringError, match="destination ancestor changed"):
        bundle_module.plan_project_compilation(project_paths(project, "leaf"))


@pytest.mark.parametrize("kind", ("dangling", "loop"))
def test_leaf_planner_rejects_unresolvable_destination_parent_symlink(
    tmp_path: Path, kind: str
) -> None:
    from lockstep.authoring_bundle import plan_project_compilation

    project = tmp_path / "project"
    write_workflow(project, "leaf")
    recipes = project / ".lockstep" / "recipes"
    target = tmp_path / "missing-recipes" if kind == "dangling" else recipes
    recipes.symlink_to(target, target_is_directory=True)

    with pytest.raises(AuthoringError, match="destination ancestor"):
        plan_project_compilation(project_paths(project, "leaf"))


def test_leaf_planner_shares_stable_directory_identity_across_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lockstep.authoring_bundle as bundle_module

    project = tmp_path / "project"
    workflow = write_workflow(project, "leaf")
    recipe = project_paths(project, "leaf")
    watched = (project, project / ".lockstep", workflow.parent, recipe.recipe_path.parent)
    original_capture = bundle_module._directory_identity
    captures = {path: 0 for path in watched}

    def count_capture(path: Path):
        if path in captures:
            captures[path] += 1
        return original_capture(path)

    monkeypatch.setattr(bundle_module, "_directory_identity", count_capture)
    bundle = bundle_module.plan_project_compilation(recipe)

    source = bundle.sources[0]
    destination_ancestors = bundle.before_images[0].ancestors
    assert bundle.project_identity == source.ancestors[0] == destination_ancestors[0]
    assert source.ancestors[1] == destination_ancestors[1]
    assert captures == {
        project: 1,
        project / ".lockstep": 1,
        workflow.parent: 1,
        recipe.recipe_path.parent: 1,
    }


@pytest.mark.parametrize("failure", ("parse", "semantic", "missing", "cycle"))
def test_compile_planning_failure_is_write_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    project = tmp_path / "project"
    state = tmp_path / "owner-state"
    child = write_workflow(project, "child")
    parent = write_workflow(project, "parent", children=("child",))
    compile_closure(project, "child", "parent")

    if failure == "parse":
        child.write_text("not: [valid", encoding="utf-8")
    elif failure == "semantic":
        child.write_text(child.read_text().replace("protect: ['**']", "protect: ['src/**']"))
    elif failure == "missing":
        child.rename(child.with_suffix(".missing"))
    else:
        parent.write_text(parent.read_text().replace("workflow: child", "workflow: parent"))
    expected_project = tree_image(project)

    from lockstep import cli

    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state))
    monkeypatch.chdir(project)
    assert cli.main(["recipe", "compile", "parent"]) == 2
    capsys.readouterr()

    assert tree_image(project) == expected_project
