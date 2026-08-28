"""Publisher preflight rejects oversized immutable bundles before mutation."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from lockstep.authoring_bundle import (
    DestinationImage,
    LeafIdentity,
    PathIdentity,
    ProjectCompilationBundle,
    SourceIdentity,
)
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.runtime.owner_state import StorageLimitExceeded
from tests._authoring_gate import tree_image


def _path_identity(path: Path) -> PathIdentity:
    info = path.stat()
    return PathIdentity(path, info.st_dev, info.st_ino)


def _leaf(path: Path) -> LeafIdentity:
    info = path.lstat()
    return LeafIdentity(
        path,
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _bundle(
    tmp_path: Path,
    *,
    group: str,
    source_count: int,
    destination_count: int,
    size: int,
) -> ProjectCompilationBundle:
    project = tmp_path / "project"
    inputs = project / ".lockstep" / "inputs"
    recipes = project / ".lockstep" / "recipes"
    inputs.mkdir(parents=True)
    recipes.mkdir()
    project_identity = _path_identity(project)
    source_ancestors = (
        project_identity,
        _path_identity(project / ".lockstep"),
        _path_identity(inputs),
    )
    destination_ancestors = (
        project_identity,
        _path_identity(project / ".lockstep"),
        _path_identity(recipes),
    )
    role_count = max(source_count, destination_count)
    roles = tuple(f"role-{index}" for index in range(role_count))
    sources: list[SourceIdentity] = []
    for index, role in enumerate(roles[:source_count]):
        content = b"r" * size if group == "read" else b"r"
        path = inputs / f"source-{index}"
        path.write_bytes(content)
        sources.append(
            SourceIdentity(
                role,
                path,
                content,
                hashlib.sha256(content).hexdigest(),
                _leaf(path),
                source_ancestors,
            )
        )
    before: list[DestinationImage] = []
    after: list[DestinationImage] = []
    for index, role in enumerate(roles[:destination_count]):
        path = recipes / f"destination-{index}"
        before_content = b"b" * size if group == "before" else None
        if before_content is not None:
            path.write_bytes(before_content)
            leaf = _leaf(path)
            before.append(
                DestinationImage(
                    role,
                    path,
                    before_content,
                    hashlib.sha256(before_content).hexdigest(),
                    stat.S_IMODE(leaf.mode),
                    leaf,
                    destination_ancestors,
                )
            )
        else:
            before.append(
                DestinationImage(
                    role,
                    path,
                    None,
                    None,
                    None,
                    None,
                    destination_ancestors,
                )
            )
        after_content = b"a" * size if group == "after" else b"ok"
        after.append(
            DestinationImage(
                role,
                path,
                after_content,
                hashlib.sha256(after_content).hexdigest(),
                0o644,
                None,
                destination_ancestors,
            )
        )
    return ProjectCompilationBundle(
        project,
        project_identity,
        tuple(sources),
        tuple((role, ()) for role in roles),
        tuple(before),
        tuple(after),
    )


@pytest.mark.parametrize(
    ("group", "source_count", "destination_count", "size", "reason"),
    (
        ("read", 257, 1, 1, "authoring read set exceeds 256 admission limit"),
        ("paired", 0, 257, 1, "authoring before images exceeds 256 admission limit"),
        (
            "read",
            5,
            1,
            900_000,
            "authoring read set exceeds the aggregate byte admission limit",
        ),
        (
            "before",
            0,
            5,
            900_000,
            "authoring before images exceeds the aggregate byte admission limit",
        ),
        (
            "after",
            0,
            5,
            900_000,
            "authoring after images exceeds the aggregate byte admission limit",
        ),
    ),
)
def test_publisher_revalidation_rejects_each_limit_before_owner_namespace_creation(
    tmp_path: Path,
    group: str,
    source_count: int,
    destination_count: int,
    size: int,
    reason: str,
) -> None:
    bundle = _bundle(
        tmp_path,
        group=group,
        source_count=source_count,
        destination_count=destination_count,
        size=size,
    )
    owner = (tmp_path / "owner").resolve()
    before = tree_image(bundle.resolved_project)
    with pytest.raises(StorageLimitExceeded, match=reason):
        AuthoringPublisher(owner).publish(bundle)
    assert tree_image(bundle.resolved_project) == before
    assert not owner.exists()
