"""Immutable authoring bundle and public publisher contracts."""

from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path

import pytest

from lockstep.authoring import project_paths
from tests._authoring_gate import write_workflow


def test_whole_dag_bundle_contracts_are_explicit() -> None:
    from lockstep.authoring import AuthoredRecipe as PublicAuthoredRecipe
    from lockstep.authoring_bundle import (
        AuthoredRecipe,
        DestinationImage,
        ProjectCompilationBundle,
        SourceIdentity,
    )

    assert all(
        isinstance(contract, type)
        for contract in (SourceIdentity, DestinationImage, ProjectCompilationBundle)
    )
    assert PublicAuthoredRecipe is AuthoredRecipe


def test_authoring_publisher_surface_has_only_the_frozen_operations() -> None:
    from lockstep.authoring_publisher import AuthoringPublisher

    assert tuple(inspect.signature(AuthoringPublisher.__init__).parameters) == (
        "self",
        "state_dir",
    )
    assert tuple(inspect.signature(AuthoringPublisher.publish).parameters) == (
        "self",
        "bundle",
    )
    assert tuple(inspect.signature(AuthoringPublisher.recover).parameters) == (
        "self",
        "project",
    )


def _two_role_bundle(project: Path):
    from lockstep.authoring_bundle import plan_project_compilation

    write_workflow(project, "child")
    write_workflow(project, "parent", children=("child",))
    return plan_project_compilation(project_paths(project, "parent"))


def test_destination_only_bundle_roles_are_owned_by_dependency_topology(
    tmp_path: Path,
) -> None:
    ordinary = _two_role_bundle(tmp_path / "project")
    template = replace(ordinary, sources=())

    assert template.sources == ()
    assert tuple(role for role, _children in template.dependency_edges) == (
        "child",
        "parent",
    )
    assert {image.role for image in template.after_images} == {"child", "parent"}


def test_bundle_rejects_a_partial_source_role_inventory(tmp_path: Path) -> None:
    ordinary = _two_role_bundle(tmp_path / "project")
    with pytest.raises(ValueError):
        replace(ordinary, sources=ordinary.sources[:1])


def test_destination_only_bundle_requires_nonempty_topology(tmp_path: Path) -> None:
    ordinary = _two_role_bundle(tmp_path / "project")
    with pytest.raises(ValueError):
        replace(ordinary, sources=(), dependency_edges=())


def test_destination_only_bundle_rejects_an_unowned_write_role(
    tmp_path: Path,
) -> None:
    ordinary = _two_role_bundle(tmp_path / "project")
    changed_before = replace(ordinary.before_images[0], role="foreign")
    changed_after = replace(ordinary.after_images[0], role="foreign")

    with pytest.raises(ValueError):
        replace(
            ordinary,
            sources=(),
            before_images=(changed_before, *ordinary.before_images[1:]),
            after_images=(changed_after, *ordinary.after_images[1:]),
        )


def test_project_compilation_bundle_rejects_mismatched_paired_ancestors(
    tmp_path: Path,
) -> None:
    from lockstep.authoring_bundle import plan_project_compilation

    project = tmp_path / "project"
    write_workflow(project, "leaf")
    bundle = plan_project_compilation(project_paths(project, "leaf"))
    changed_after = replace(bundle.after_images[0], ancestors=())

    with pytest.raises(ValueError, match="paired destination ancestors"):
        replace(bundle, after_images=(changed_after, *bundle.after_images[1:]))
