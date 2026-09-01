"""Immutable project snapshot capture and lineage verification."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable
from pathlib import Path

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.project_snapshots import (
    ProjectSnapshotRef,
    ProjectSnapshotStore,
)


class RuntimeSnapshotConflict(RuntimeError):
    """An immutable runtime input was rebound or failed lineage verification."""


_MAX_LINEAGE = 10_000


def _read_regular(path: Path, expected_sha256: str, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise RuntimeSnapshotConflict(f"project snapshot file is not admissible: {path}")
        chunks: list[bytes] = []
        observed = 0
        while chunk := os.read(descriptor, min(1024 * 1024, max_bytes + 1 - observed)):
            observed += len(chunk)
            if observed > max_bytes:
                raise RuntimeSnapshotConflict(f"project snapshot file exceeds limit: {path}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)
        if identity(before) != identity(after):
            raise RuntimeSnapshotConflict(f"project snapshot file changed while reading: {path}")
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise RuntimeSnapshotConflict(f"project snapshot file changed while reading: {path}")
        return data
    finally:
        os.close(descriptor)


def capture_authoritative_snapshot(
    project: Path,
    snapshots: ProjectSnapshotStore,
    blobs: BlobStore,
    binding: RunBinding,
    *,
    previous: ProjectSnapshotRef | None,
    purpose: str,
) -> ProjectSnapshotRef:
    """Capture one complete, symlink-free project image as a chain successor."""

    if purpose not in {"run-start", "manual", "publication", "effect"}:
        raise ValueError("unsupported authoritative snapshot purpose")
    root = Path(project)
    if root.resolve() != Path(binding.project_identity).resolve():
        raise RuntimeSnapshotConflict("snapshot project differs from immutable run binding")
    from lockstep.runtime.manifests import PathContractError, capture_project

    try:
        manifest = capture_project(root, limits=snapshots.limits)
    except (OSError, PathContractError) as exc:
        raise RuntimeSnapshotConflict(f"project snapshot capture failed: {exc}") from exc
    if any(item.kind == "symlink" for item in manifest.entries):
        raise RuntimeSnapshotConflict("authoritative project snapshots reject symlinks")
    file_entries = tuple(item for item in manifest.entries if item.kind == "file")
    stored = {}
    for item in file_entries:
        assert item.sha256 is not None
        data = _read_regular(root / item.path, item.sha256, snapshots.limits.max_file_bytes)
        stored[item.path] = blobs.put(data, expected_sha256=item.sha256)
    provenance = {
        "schema": "lockstep.run-project-snapshot/v1",
        "public_run_id": binding.public_run_id,
        "project_identity": binding.project_identity,
        "definition_digest": binding.recipe_digest,
        "purpose": purpose,
    }
    return snapshots.capture(
        stored,
        declared_paths=tuple(stored),
        provenance=provenance,
        previous=previous,
    )


def verify_bound_snapshot(
    ref: ProjectSnapshotRef,
    snapshots: ProjectSnapshotStore,
    binding: RunBinding,
):
    """Read one immutable snapshot and verify its exact run/project provenance."""

    snapshot = snapshots.read(ref)
    provenance = dict(snapshot.provenance)
    if (
        provenance.get("schema") != "lockstep.run-project-snapshot/v1"
        or provenance.get("public_run_id") != binding.public_run_id
        or provenance.get("project_identity") != binding.project_identity
        or provenance.get("definition_digest") != binding.recipe_digest
        or provenance.get("purpose") not in {"run-start", "manual", "publication", "effect"}
    ):
        raise RuntimeSnapshotConflict("runtime snapshot is foreign to the immutable run binding")
    return snapshot


def _chain(ref: ProjectSnapshotRef, snapshots: ProjectSnapshotStore) -> tuple[ProjectSnapshotRef, ...]:
    result: list[ProjectSnapshotRef] = []
    seen: set[ProjectSnapshotRef] = set()
    current: ProjectSnapshotRef | None = ref
    while current is not None:
        if current in seen:
            raise RuntimeSnapshotConflict("project snapshot lineage contains a cycle")
        if len(result) >= _MAX_LINEAGE:
            raise RuntimeSnapshotConflict("project snapshot lineage exceeds public bound")
        seen.add(current)
        result.append(current)
        current = snapshots.read(current).previous
    return tuple(result)


def resolve_lineage_snapshot(
    refs: Iterable[ProjectSnapshotRef], snapshots: ProjectSnapshotStore
) -> ProjectSnapshotRef:
    """Return the greatest common ancestor of exact immutable snapshot chains."""

    selected = tuple(dict.fromkeys(refs))
    if not selected:
        raise RuntimeSnapshotConflict("runtime snapshot lineage is empty")
    chains = tuple(_chain(ref, snapshots) for ref in selected)
    common = set(chains[0])
    for chain in chains[1:]:
        common.intersection_update(chain)
    for candidate in chains[0]:
        if candidate in common:
            return candidate
    raise RuntimeSnapshotConflict("runtime snapshot lineages have no common ancestor")
