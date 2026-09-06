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
    writes: tuple[str, ...] | None = None,
) -> ProjectSnapshotRef:
    """Capture one complete, symlink-free project image as a chain successor."""

    if purpose not in {"run-start", "manual", "publication", "effect"}:
        raise ValueError("unsupported authoritative snapshot purpose")
    root = Path(project)
    if root.resolve() != Path(binding.project_identity).resolve():
        raise RuntimeSnapshotConflict("snapshot project differs from immutable run binding")
    from lockstep.runtime.manifests import (
        PathContractError,
        ProjectWritePath,
        capture_project,
    )

    try:
        manifest = capture_project(root, limits=snapshots.limits)
    except (OSError, PathContractError) as exc:
        raise RuntimeSnapshotConflict(f"project snapshot capture failed: {exc}") from exc
    if any(item.kind == "symlink" for item in manifest.entries):
        raise RuntimeSnapshotConflict("authoritative project snapshots reject symlinks")
    file_entries = tuple(item for item in manifest.entries if item.kind == "file")
    surfaces = None if writes is None else tuple(
        ProjectWritePath.parse(path, root) for path in writes
    )
    stored = {}
    if surfaces is not None:
        if previous is None:
            raise RuntimeSnapshotConflict("branch snapshot requires a previous snapshot")
        stored = {
            item.path: item.blob for item in snapshots.read(previous).files
            if not any(surface.allows(item.path) for surface in surfaces)
        }
    for item in file_entries:
        if surfaces is not None and not any(surface.allows(item.path) for surface in surfaces):
            continue
        assert item.sha256 is not None
        data = _read_regular(root / item.path, item.sha256, snapshots.limits.max_file_bytes)
        stored[item.path] = blobs.put(data, expected_sha256=item.sha256)
    provenance: dict[str, object] = {
        "schema": "lockstep.run-project-snapshot/v1",
        "public_run_id": binding.public_run_id,
        "project_identity": binding.project_identity,
        "definition_digest": binding.recipe_digest,
        "purpose": purpose,
    }
    if writes is not None:
        provenance["parallel_manual"] = True
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
    active: set[ProjectSnapshotRef] = set()
    pending = [(ref, False)]
    while pending:
        current, leaving = pending.pop()
        if leaving:
            active.remove(current)
            seen.add(current)
            result.append(current)
            continue
        if current in active:
            raise RuntimeSnapshotConflict("project snapshot lineage contains a cycle")
        if current in seen:
            continue
        if len(seen) + len(active) >= _MAX_LINEAGE:
            raise RuntimeSnapshotConflict("project snapshot lineage exceeds public bound")
        active.add(current)
        snapshot = snapshots.read(current)
        parents = tuple(ProjectSnapshotRef(digest) for digest in snapshot.provenance.get("merged_from", ()))
        if snapshot.previous is not None:
            parents = (*parents, snapshot.previous)
        pending.append((current, True))
        pending.extend((parent, False) for parent in parents)
    return tuple(reversed(result))


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


def merge_lineage_snapshots(
    refs: Iterable[ProjectSnapshotRef], snapshots: ProjectSnapshotStore, binding: RunBinding
) -> ProjectSnapshotRef:
    """Merge accepted disjoint deltas without capturing unaccepted live files."""

    selected = tuple(sorted(set(refs)))
    ancestors = {ref: set(_chain(ref, snapshots)[1:]) for ref in selected}
    tips = tuple(ref for ref in selected if not any(
        ref in chain for other, chain in ancestors.items() if other != ref
    ))
    common = resolve_lineage_snapshot(tips, snapshots)
    if len(tips) == 1:
        return tips[0]
    shared = set(_chain(common, snapshots))
    if not any(
        snapshots.read(ref).provenance.get("parallel_manual") is True
        for tip in tips
        for ref in (ancestors[tip] | {tip}) - shared
    ):
        # A manual snapshot already shared by all branches is an earlier
        # completed parallel block, not a reason to change this fan-in policy.
        return common
    baseline = {item.path: item.blob for item in snapshots.read(common).files}
    changes = {}
    for ref in tips:
        snapshot = snapshots.read(ref)
        if snapshot.provenance.get("schema") == "lockstep.run-project-snapshot/v1":
            verify_bound_snapshot(ref, snapshots, binding)
        files = {item.path: item.blob for item in snapshot.files}
        for path in baseline.keys() | files.keys():
            value = files.get(path)
            if value == baseline.get(path):
                continue
            if path in changes and changes[path] != value:
                raise RuntimeSnapshotConflict(f"parallel snapshots contain conflicting changes: {path}")
            changes[path] = value
    merged = dict(baseline)
    for path, value in changes.items():
        if value is None:
            merged.pop(path, None)
        else:
            merged[path] = value
    return snapshots.capture(
        merged, declared_paths=tuple(merged), previous=common,
        provenance={
            "schema": "lockstep.run-project-snapshot/v1",
            "public_run_id": binding.public_run_id,
            "project_identity": binding.project_identity,
            "definition_digest": binding.recipe_digest,
            "purpose": "effect",
            "merged_from": [ref.digest for ref in tips],
        },
    )
