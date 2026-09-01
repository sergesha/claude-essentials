"""Fenced, provider-neutral disposable Git workspaces for managed effects.

The checkout is deliberately not a source of durable workflow truth.  The
small record beside it is owner-only state and exists solely to bind the
current workspace revision and the immutable snapshot from which it was
materialized.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.manifests import ProjectSnapshot as FilesystemSnapshot
from lockstep.runtime.project_paths import ProjectTreeLimits
from lockstep.runtime.project_snapshots import ProjectSnapshotRef, ProjectSnapshotStore


class WorkspaceError(RuntimeError):
    """A workspace cannot be materialized, attested, rolled over, or removed."""


WorkspacePhase = Literal["materialized", "quarantined", "released"]
WorkspacePurpose = Literal["managed_output", "no_publish_operation"]


WorkspaceLimits = ProjectTreeLimits


@dataclass(frozen=True)
class WorkspaceContext:
    """Immutable resources and owner-only paths shared by workspace helpers."""

    records: Path
    checkouts: Path
    staging: Path
    quarantine: Path
    snapshots: ProjectSnapshotStore
    blobs: BlobStore
    limits: ProjectTreeLimits


@dataclass(frozen=True)
class WorkspaceLease:
    """Current fenced authority to inspect or operate on one checkout."""

    workspace_ref: str
    effect_id: str
    request_digest: str
    input_snapshot_ref: str
    revision: int
    workspace_path: Path
    declared_writes: tuple[str, ...]
    purpose: WorkspacePurpose
    baseline: FilesystemSnapshot
    vcs_baseline_digest: str
    phase: WorkspacePhase
    rollover_snapshot_ref: str | None = None


@dataclass(frozen=True)
class NoPublishProof:
    workspace_ref: str
    purpose: Literal["no_publish_operation"]
    workspace_quarantined: bool
    rollover_snapshot_ref: None = None


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _hex(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise WorkspaceError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise WorkspaceError(f"{label} must be a bounded non-empty string")
    return value


def _workspace_digest(workspace_ref: str) -> str:
    if not isinstance(workspace_ref, str) or not workspace_ref.startswith("workspace:"):
        raise WorkspaceError("workspace reference must use the workspace: scheme")
    return _hex(workspace_ref.removeprefix("workspace:"), "workspace reference")


def _snapshot_ref(value: str) -> ProjectSnapshotRef:
    if not isinstance(value, str) or not value.startswith("snapshot:"):
        raise WorkspaceError("input snapshot reference must use the snapshot: scheme")
    return ProjectSnapshotRef(
        _hex(value.removeprefix("snapshot:"), "snapshot reference")
    )


def _stat_identity(item: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _read_regular_nofollow(
    path: Path, expected_sha256: str, *, max_bytes: int
) -> bytes:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise WorkspaceError(f"workspace manifest expected a regular file: {path}")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise WorkspaceError(f"workspace file changed during rollover: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if _stat_identity(opened) != _stat_identity(before) or not stat.S_ISREG(
            opened.st_mode
        ):
            raise WorkspaceError(f"workspace file changed during rollover: {path}")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise WorkspaceError(
                    f"workspace file exceeds {max_bytes} byte rollover limit: {path}"
                )
            chunks.append(chunk)
        if _stat_identity(os.fstat(descriptor)) != _stat_identity(before):
            raise WorkspaceError(f"workspace file changed during rollover: {path}")
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise WorkspaceError(f"workspace manifest changed during rollover: {path}")
    return data
