"""Focused direct-child planner contract for the Task 12A RED freeze."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lockstep.authoring import project_paths

from tests._authoring_gate import (
    assert_source_identity,
    expected_compilation_image,
    tree_image,
    write_workflow,
)


def _role_images(
    project: Path, roles: tuple[str, ...]
) -> dict[str, dict[Path, bytes]]:
    return {
        role: expected_compilation_image(project, (role,))
        for role in roles
    }


def _assert_closure_sources(
    bundle,
    project: Path,
    paths: dict[str, Path],
    roles: tuple[str, ...],
    edges: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    assert tuple(source.role for source in bundle.sources) == roles
    assert len({source.role for source in bundle.sources}) == len(bundle.sources)
    for source in bundle.sources:
        assert_source_identity(source, project, paths[source.role])
    assert bundle.dependency_edges == edges
    assert bundle.project_identity == bundle.sources[0].ancestors[0]
    assert all(
        source.ancestors == bundle.sources[0].ancestors for source in bundle.sources
    )


def _assert_closure_destinations(
    bundle,
    project: Path,
    expected_by_role: dict[str, dict[Path, bytes]],
    roles: tuple[str, ...],
) -> None:
    expected_paths = {role: set(images) for role, images in expected_by_role.items()}
    assert sum(len(paths) for paths in expected_paths.values()) == len(
        set().union(*expected_paths.values())
    )
    expected_ancestors = (project.resolve(), (project / ".lockstep").resolve())
    for before_image, after_image in zip(
        bundle.before_images, bundle.after_images, strict=True
    ):
        assert before_image.role == after_image.role
        assert before_image.resolved_path == after_image.resolved_path
        assert (
            before_image.content,
            before_image.sha256,
            before_image.mode,
            before_image.leaf,
        ) == (None, None, None, None)
        assert after_image.content == expected_by_role[after_image.role][
            after_image.resolved_path
        ]
        assert after_image.sha256 == hashlib.sha256(after_image.content).hexdigest()
        assert after_image.mode == 0o644
        assert after_image.leaf is None
        for ancestors in (before_image.ancestors, after_image.ancestors):
            assert tuple(item.resolved_path for item in ancestors) == expected_ancestors
            assert tuple((item.device, item.inode) for item in ancestors) == tuple(
                (path.lstat().st_dev, path.lstat().st_ino) for path in expected_ancestors
            )
        assert before_image.ancestors == after_image.ancestors
        assert before_image.ancestors[0] == bundle.project_identity
        assert before_image.ancestors[1] == bundle.sources[0].ancestors[1]
    for images in (bundle.before_images, bundle.after_images):
        assert {
            role: {image.resolved_path for image in images if image.role == role}
            for role in roles
        } == expected_paths


def _role_generated_images(
    project: Path, expected_by_role: dict[str, dict[Path, bytes]]
) -> dict[str, dict[Path, bytes]]:
    return {
        role: {
            path: content
            for path, content in images.items()
            if path
            not in {
                project_paths(project, role).recipe_path,
                project_paths(project, role).dependency_path,
                project_paths(project, role).source_map_path,
            }
        }
        for role, images in expected_by_role.items()
    }


def _assert_parent_generated_images(bundle, expected_generated: dict[Path, bytes]) -> None:
    assert {
        image.resolved_path: image.content
        for image in bundle.after_images
        if image.role == "parent" and image.resolved_path in expected_generated
    } == expected_generated


def test_direct_child_planner_captures_complete_generated_bundle_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.authoring_bundle import ProjectCompilationBundle, plan_project_compilation

    project = tmp_path / "project"
    paths = {
        "child": write_workflow(project, "child"),
        "parent": write_workflow(project, "parent", children=("child",)),
    }
    roles = ("child", "parent")
    expected_by_role = _role_images(project, roles)
    expected_generated = _role_generated_images(project, expected_by_role)["parent"]
    assert expected_generated
    controlled_cwd = tmp_path / "controlled-cwd"
    controlled_cwd.mkdir()
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "owner-state"))
    monkeypatch.chdir(controlled_cwd)
    before = tree_image(tmp_path)

    try:
        bundle = plan_project_compilation(project_paths(project, "parent"))
    except Exception:
        assert tree_image(tmp_path) == before
        raise

    assert isinstance(bundle, ProjectCompilationBundle)
    assert tree_image(tmp_path) == before
    _assert_closure_sources(
        bundle,
        project,
        paths,
        roles,
        (("child", ()), ("parent", ("child",))),
    )
    _assert_closure_destinations(bundle, project, expected_by_role, roles)
    _assert_parent_generated_images(bundle, expected_generated)


def test_transitive_planner_captures_complete_three_role_bundle_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.authoring_bundle import ProjectCompilationBundle, plan_project_compilation

    project = tmp_path / "project"
    roles = ("grandchild", "child", "parent")
    paths = {
        "grandchild": write_workflow(project, "grandchild"),
        "child": write_workflow(project, "child", children=("grandchild",)),
        "parent": write_workflow(project, "parent", children=("child",)),
    }
    expected_by_role = _role_images(project, roles)
    generated_by_role = _role_generated_images(project, expected_by_role)
    assert all(expected_by_role.values())
    assert generated_by_role["child"]
    assert generated_by_role["parent"]
    assert sum(len(images) for images in expected_by_role.values()) == len(
        set().union(*(set(images) for images in expected_by_role.values()))
    )
    controlled_cwd = tmp_path / "controlled-cwd"
    controlled_cwd.mkdir()
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "owner-state"))
    monkeypatch.chdir(controlled_cwd)
    before = tree_image(tmp_path)

    try:
        bundle = plan_project_compilation(project_paths(project, "parent"))
    except Exception:
        assert tree_image(tmp_path) == before
        raise

    assert isinstance(bundle, ProjectCompilationBundle)
    assert tree_image(tmp_path) == before
    _assert_closure_sources(
        bundle,
        project,
        paths,
        roles,
        (
            ("grandchild", ()),
            ("child", ("grandchild",)),
            ("parent", ("child",)),
        ),
    )
    _assert_closure_destinations(bundle, project, expected_by_role, roles)
