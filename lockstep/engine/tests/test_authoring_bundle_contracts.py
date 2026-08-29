"""Immutable whole-project bundle contracts retained through Task 4."""
from __future__ import annotations

import dataclasses, inspect
from pathlib import Path

import pytest

from lockstep.authoring import project_paths
from lockstep.authoring_bundle import DestinationImage, ProjectCompilationBundle, SourceIdentity, plan_project_compilation
from lockstep.authoring_identity import validate_bundle_preconditions
from lockstep.authoring_publisher import AuthoringPublisher
from tests._authoring_gate import write_workflow


def _bundle(tmp_path: Path) -> ProjectCompilationBundle:
    project = tmp_path / "project"; project.mkdir()
    write_workflow(project, "child"); write_workflow(project, "parent", children=("child",))
    return plan_project_compilation(project_paths(project, "parent"))


def test_whole_dag_bundle_contracts_are_explicit() -> None:
    assert tuple(field.name for field in dataclasses.fields(ProjectCompilationBundle)) == (
        "resolved_project", "project_identity", "sources", "dependency_edges", "before_images", "after_images",
    )
    assert tuple(field.name for field in dataclasses.fields(SourceIdentity)) == (
        "role", "resolved_path", "content", "sha256", "leaf", "ancestors",
    )
    assert tuple(field.name for field in dataclasses.fields(DestinationImage)) == (
        "role", "resolved_path", "content", "sha256", "mode", "leaf", "ancestors",
    )


def test_publisher_surface_has_only_frozen_authoring_operations() -> None:
    operations = {name for name, value in inspect.getmembers(AuthoringPublisher, inspect.isfunction) if not name.startswith("_")}
    assert operations == {"require_ready", "publish", "observe"}


def test_planner_emits_closed_child_first_topology_and_complete_inventory(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    assert bundle.dependency_edges == (("child", ()), ("parent", ("child",)))
    assert tuple(source.role for source in bundle.sources) == ("child", "parent")
    assert {image.role for image in bundle.after_images} == {"child", "parent"}


def test_targets_are_unique_owned_and_bound_to_exact_project_parent(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    paths = tuple(image.resolved_path for image in bundle.after_images)
    assert len(paths) == len(set(paths))
    assert all(image.ancestors[0] == bundle.project_identity for image in (*bundle.before_images, *bundle.after_images))


def test_bundle_rejects_partial_source_inventory(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    with pytest.raises(ValueError, match="complete or empty"):
        dataclasses.replace(bundle, sources=bundle.sources[:1])


def test_bundle_requires_nonempty_closed_topology(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    with pytest.raises(ValueError, match="non-empty"):
        dataclasses.replace(bundle, sources=(), dependency_edges=())
    with pytest.raises(ValueError, match="earlier child"):
        dataclasses.replace(bundle, dependency_edges=(("parent", ("child",)), ("child", ())))


def test_bundle_rejects_unowned_or_duplicate_targets(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    foreign_before = dataclasses.replace(bundle.before_images[0], role="foreign")
    foreign_after = dataclasses.replace(bundle.after_images[0], role="foreign")
    with pytest.raises(ValueError, match="roles"):
        dataclasses.replace(bundle, before_images=(foreign_before, *bundle.before_images[1:]), after_images=(foreign_after, *bundle.after_images[1:]))
    with pytest.raises(ValueError, match="match exactly"):
        dataclasses.replace(bundle, before_images=(bundle.before_images[0], bundle.before_images[0], *bundle.before_images[2:]),
            after_images=(bundle.after_images[0], bundle.after_images[0], *bundle.after_images[2:]))


def test_bundle_rejects_mismatched_paired_parent_identity(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    changed = dataclasses.replace(bundle.after_images[0], ancestors=bundle.after_images[0].ancestors[1:])
    with pytest.raises(ValueError, match="paired destination ancestors"):
        dataclasses.replace(bundle, after_images=(changed, *bundle.after_images[1:]))


def test_matched_foreign_parent_chain_fails_at_live_precondition_boundary(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path); before, after = bundle.before_images[0], bundle.after_images[0]
    assert before.ancestors == after.ancestors and before.ancestors[0] == bundle.project_identity
    foreign = before.ancestors[1:]
    changed = dataclasses.replace(bundle,
        before_images=(dataclasses.replace(before, ancestors=foreign), *bundle.before_images[1:]),
        after_images=(dataclasses.replace(after, ancestors=foreign), *bundle.after_images[1:]))
    with pytest.raises(Exception, match="ancestor|project"): validate_bundle_preconditions(changed)
