"""Fenced, provider-neutral disposable Git workspaces for managed effects.

The checkout is deliberately not a source of durable workflow truth.  The
small record beside it is owner-only state and exists solely to bind a
workspace generation, its cleanup fence, and the immutable snapshot from
which it was materialized.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.locking import file_lock
from lockstep.runtime.manifests import (
    PathContractError,
    ProjectWritePath,
    capture_project,
    compare_effect,
    snapshot_from_data,
    snapshot_to_data,
)
from lockstep.runtime.manifests import ProjectSnapshot as FilesystemSnapshot
from lockstep.runtime.owner_state import (
    InsecureStatePath,
    ensure_owner_directory,
    initialize_owner_state,
    seal_owner_file,
    verify_owner_file,
)
from lockstep.runtime.project_snapshots import ProjectSnapshotRef, ProjectSnapshotStore


class WorkspaceError(RuntimeError):
    """A workspace cannot be materialized, attested, rolled over, or removed."""


WorkspacePhase = Literal["materialized", "quarantined", "released"]


@dataclass(frozen=True)
class WorkspaceLimits:
    max_entries: int = 10_000
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        if min(self.max_entries, self.max_file_bytes, self.max_total_bytes) <= 0:
            raise ValueError("workspace limits must be positive")


@dataclass(frozen=True)
class WorkspaceLease:
    """Current fenced authority to inspect or operate on one checkout."""

    workspace_ref: str
    effect_id: str
    request_digest: str
    input_snapshot_ref: str
    generation: int
    revision: int
    cleanup_fence: int
    workspace_path: Path
    declared_writes: tuple[str, ...]
    baseline: FilesystemSnapshot
    vcs_baseline_digest: str
    phase: WorkspacePhase
    rollover_snapshot_ref: str | None = None


@dataclass(frozen=True)
class WorkspaceDeathProof:
    """Opaque provider-issued binding of process death to a cleanup fence."""

    workspace_ref: str
    generation: int
    cleanup_fence: int
    process_identity: str
    digest: str


@dataclass(frozen=True)
class WorkspaceRolloverProof:
    """Proof that a quarantined generation was sealed into a snapshot."""

    workspace_ref: str
    generation: int
    revision: int
    cleanup_fence: int
    input_snapshot_ref: str
    snapshot_ref: str


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
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
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
    return ProjectSnapshotRef(_hex(value.removeprefix("snapshot:"), "snapshot reference"))


def _collision_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _windows_alias(value: str) -> bool:
    stripped = value.rstrip(". ")
    if stripped != value or not stripped:
        return True
    base = stripped.split(".", 1)[0].upper()
    return base in {"CON", "PRN", "AUX", "NUL", "CLOCK$"} or (
        len(base) == 4 and base[:3] in {"COM", "LPT"} and base[3] in "123456789"
    )


def _validate_portable_paths(paths: tuple[str, ...]) -> None:
    """Reject traversal, reserved names, and cross-platform aliases up front."""

    siblings: dict[tuple[str, ...], dict[str, str]] = {}
    for raw in paths:
        if (
            not isinstance(raw, str)
            or not raw
            or "\\" in raw
            or any(character in raw for character in '<>:"|?*')
            or any(ord(character) < 32 for character in raw)
        ):
            raise WorkspaceError(f"unsafe snapshot path {raw!r}")
        path = PurePosixPath(raw)
        if path.is_absolute() or path.as_posix() != raw or any(
            part in {"", ".", ".."} for part in path.parts
        ):
            raise WorkspaceError(f"unsafe snapshot traversal path {raw!r}")
        for index, part in enumerate(path.parts):
            if _windows_alias(part):
                raise WorkspaceError(f"unsafe platform alias in snapshot path {raw!r}")
            if index == 0 and _collision_key(part) == _collision_key(".git"):
                raise WorkspaceError(f"Git control path is reserved: {raw!r}")
            parent = tuple(path.parts[:index])
            seen = siblings.setdefault(parent, {})
            key = _collision_key(part)
            previous = seen.get(key)
            if previous is not None and previous != part:
                raise WorkspaceError(
                    f"snapshot path collision between {previous!r} and {part!r}"
                )
            seen[key] = part


def _validate_portable_file_paths(paths: tuple[str, ...]) -> None:
    _validate_portable_paths(paths)
    files = set(paths)
    for raw in paths:
        path = PurePosixPath(raw)
        for parent in path.parents:
            if parent.as_posix() != "." and parent.as_posix() in files:
                raise WorkspaceError(
                    f"snapshot file collides with descendant path {raw!r}"
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


class LocalGitWorkspaceProvider:
    """Materialize and roll over local, fenced, disposable Git workspaces."""

    def __init__(
        self,
        owner_state_dir: str | Path,
        snapshots: ProjectSnapshotStore,
        blobs: BlobStore,
        *,
        limits: WorkspaceLimits | None = None,
    ) -> None:
        self._owner_state = initialize_owner_state(owner_state_dir)
        self._root = ensure_owner_directory(self._owner_state, "managed-workspaces")
        self._records = ensure_owner_directory(self._root, "records")
        self._checkouts = ensure_owner_directory(self._root, "checkouts")
        self._quarantine = ensure_owner_directory(self._root, "quarantine")
        self._snapshots = snapshots
        self._blobs = blobs
        self._limits = limits or WorkspaceLimits()
        self._issued_death_proofs: set[str] = set()

    def workspace_ref_for(self, effect_id: str, intent_digest: str) -> str:
        commitment = {
            "schema": "lockstep.workspace-ref/v1",
            "effect_id": _text(effect_id, "effect_id"),
            "intent_digest": _hex(intent_digest, "intent digest"),
        }
        return f"workspace:{_digest(commitment)}"

    def materialize(
        self,
        *,
        effect_id: str,
        request_digest: str,
        workspace_ref: str,
        input_snapshot_ref: str,
        declared_writes: tuple[str, ...],
    ) -> WorkspaceLease:
        effect_id = _text(effect_id, "effect_id")
        request_digest = _hex(request_digest, "request digest")
        key = _workspace_digest(workspace_ref)
        snapshot_ref = _snapshot_ref(input_snapshot_ref)
        if not isinstance(declared_writes, tuple):
            declared_writes = tuple(declared_writes)
        if len(declared_writes) > 10_000:
            raise WorkspaceError("declared writes exceed workspace admission limit")
        record_path = self._record_path(key)
        with file_lock(record_path, timeout=30.0, stale_after=300.0):
            if record_path.exists() or record_path.is_symlink():
                lease = self._read_record(key)
                expected = (
                    effect_id,
                    request_digest,
                    input_snapshot_ref,
                    tuple(declared_writes),
                )
                observed = (
                    lease.effect_id,
                    lease.request_digest,
                    lease.input_snapshot_ref,
                    lease.declared_writes,
                )
                if observed != expected:
                    raise WorkspaceError("workspace reference is bound to another request")
                if lease.phase != "materialized":
                    raise WorkspaceError(
                        "quarantined or released workspace is not reusable"
                    )
                if self._capture(lease.workspace_path) != lease.baseline:
                    raise WorkspaceError(
                        "materialized workspace drifted from its launch baseline"
                    )
                if self._vcs_tree_digest(lease.workspace_path) != lease.vcs_baseline_digest:
                    raise WorkspaceError(
                        "materialized workspace Git control state drifted"
                    )
                return lease

            snapshot_before = self._read_snapshot(snapshot_ref)
            paths = tuple(entry.path for entry in snapshot_before.files)
            _validate_portable_file_paths(paths)
            generation = 1
            checkout = self._checkout_path(key, generation)
            if checkout.exists() or checkout.is_symlink():
                raise WorkspaceError("unrecorded workspace checkout already exists")
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{key}-", dir=self._checkouts)
            )
            temporary.chmod(0o700)
            published = False
            try:
                for entry in snapshot_before.files:
                    destination = temporary.joinpath(*PurePosixPath(entry.path).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    destination.write_bytes(self._blobs.read(entry.blob))
                    destination.chmod(0o600)
                snapshot_after = self._read_snapshot(snapshot_ref)
                if snapshot_after != snapshot_before:
                    raise WorkspaceError("input snapshot changed while materializing")
                self._initialize_git(temporary)
                os.rename(temporary, checkout)
                published = True
                self._fsync_directory(self._checkouts)
                baseline = self._capture(checkout)
                self._verify_exact_input(baseline, snapshot_before)
                vcs_baseline_digest = self._vcs_tree_digest(checkout)
                # Parse only after publication: the Git attestation and every
                # collision check must bind the path the runner will receive.
                for path in declared_writes:
                    ProjectWritePath.parse(path, checkout)
            except Exception as exc:
                if temporary.exists() and not temporary.is_symlink():
                    shutil.rmtree(temporary)
                if published and checkout.exists() and not checkout.is_symlink():
                    shutil.rmtree(checkout)
                if isinstance(exc, WorkspaceError):
                    raise
                raise WorkspaceError(f"workspace materialization failed: {exc}") from exc

            lease = WorkspaceLease(
                workspace_ref=workspace_ref,
                effect_id=effect_id,
                request_digest=request_digest,
                input_snapshot_ref=input_snapshot_ref,
                generation=generation,
                revision=1,
                cleanup_fence=1,
                workspace_path=checkout,
                declared_writes=tuple(declared_writes),
                baseline=baseline,
                vcs_baseline_digest=vcs_baseline_digest,
                phase="materialized",
            )
            try:
                self._write_record(lease, snapshot_ref_out=None)
            except Exception:
                if checkout.exists() and not checkout.is_symlink():
                    shutil.rmtree(checkout)
                raise
            return lease

    def inspect(self, workspace_ref: str) -> WorkspaceLease:
        return self._read_record(_workspace_digest(workspace_ref))

    def actual_death_proof(
        self, lease: WorkspaceLease, *, process_identity: str
    ) -> WorkspaceDeathProof:
        current = self.inspect(lease.workspace_ref)
        self._validate_immutable_lease(lease, current)
        process_identity = _text(process_identity, "process identity")
        commitment = {
            "schema": "lockstep.workspace-death/v1",
            "workspace_ref": current.workspace_ref,
            "generation": current.generation,
            "cleanup_fence": current.cleanup_fence,
            "process_identity": process_identity,
        }
        proof = WorkspaceDeathProof(
            workspace_ref=current.workspace_ref,
            generation=current.generation,
            cleanup_fence=current.cleanup_fence,
            process_identity=process_identity,
            digest=_digest(commitment),
        )
        self._issued_death_proofs.add(proof.digest)
        return proof

    def quarantine_and_rollover(
        self,
        lease: WorkspaceLease,
        *,
        actual_death: WorkspaceDeathProof,
    ) -> WorkspaceRolloverProof:
        key = _workspace_digest(lease.workspace_ref)
        record_path = self._record_path(key)
        with file_lock(record_path, timeout=30.0, stale_after=300.0):
            current = self._read_record(key)
            self._validate_current_lease(lease, current)
            self._validate_death(current, actual_death)
            stored_snapshot_ref = self._stored_rollover_ref(key)
            if current.phase == "quarantined" and stored_snapshot_ref is not None:
                return self._rollover_proof(current, stored_snapshot_ref)
            if current.phase == "released":
                raise WorkspaceError("released workspace cannot be rolled over")

            if current.phase == "materialized":
                quarantined_path = self._quarantine_path(key, current.generation)
                moved = quarantined_path.exists() and not current.workspace_path.exists()
                if quarantined_path.is_symlink() or (
                    quarantined_path.exists() and not moved
                ):
                    raise WorkspaceError("workspace quarantine destination already exists")
                if not moved:
                    try:
                        os.rename(current.workspace_path, quarantined_path)
                        self._fsync_directory(self._checkouts)
                        self._fsync_directory(self._quarantine)
                    except OSError as exc:
                        raise WorkspaceError(
                            "workspace could not be durably quarantined"
                        ) from exc
                current = WorkspaceLease(
                    **{
                        **current.__dict__,
                        "revision": current.revision + 1,
                        "workspace_path": quarantined_path,
                        "phase": "quarantined",
                        "rollover_snapshot_ref": None,
                    }
                )
                # Publish the deny-reuse/quarantine fence before inspecting or
                # copying any untrusted runner output.
                self._write_record(current, snapshot_ref_out=None)

            # A normal move changes only the standard .git directory linkage
            # digest because that digest commits the checkout's absolute path.
            # Rebind that one value, while requiring every Git control file and
            # ref digest to remain identical.  This also recovers a crash after
            # the durable quarantine record but before baseline rebinding.
            relocated = self._capture(current.workspace_path)
            baseline = self._relocated_baseline(current.baseline, relocated)
            if baseline != current.baseline:
                current = WorkspaceLease(
                    **{
                        **current.__dict__,
                        "revision": current.revision + 1,
                        "baseline": baseline,
                    }
                )
                self._write_record(current, snapshot_ref_out=None)

            try:
                self._preflight_tree_limits(current.workspace_path)
                before_copy = self._capture(current.workspace_path)
                self._validate_output(current, before_copy)
                files = {}
                copied_bytes = 0
                for entry in before_copy.entries:
                    if entry.kind == "file":
                        if len(files) >= self._limits.max_entries:
                            raise WorkspaceError(
                                "workspace files exceed rollover admission limit"
                            )
                        assert entry.sha256 is not None
                        data = _read_regular_nofollow(
                            current.workspace_path.joinpath(
                                *PurePosixPath(entry.path).parts
                            ),
                            entry.sha256,
                            max_bytes=self._limits.max_file_bytes,
                        )
                        copied_bytes += len(data)
                        if copied_bytes > self._limits.max_total_bytes:
                            raise WorkspaceError(
                                "workspace bytes exceed rollover admission limit"
                            )
                        files[entry.path] = self._blobs.put(
                            data, expected_sha256=entry.sha256
                        )
                after_copy = self._capture(current.workspace_path)
                if after_copy != before_copy:
                    raise WorkspaceError(
                        "workspace changed during consistent before/copy/after capture"
                    )
                if self._vcs_tree_digest(current.workspace_path) != current.vcs_baseline_digest:
                    raise WorkspaceError(
                        "Git control state changed during rollover capture"
                    )
                previous = _snapshot_ref(current.input_snapshot_ref)
                input_snapshot = self._read_snapshot(previous)
                rolled = self._snapshots.capture(
                    files,
                    declared_paths=tuple(
                        sorted(
                            set(input_snapshot.declared_paths)
                            | set(current.declared_writes)
                        )
                    ),
                    provenance={
                        "source": "managed-workspace-rollover",
                        "workspace_ref": current.workspace_ref,
                        "request_digest": current.request_digest,
                        "generation": current.generation,
                    },
                    previous=previous,
                )
            except Exception as exc:
                if isinstance(exc, WorkspaceError):
                    raise
                raise WorkspaceError(f"workspace manifest integrity failure: {exc}") from exc

            result_ref = f"snapshot:{rolled.digest}"
            current = WorkspaceLease(
                **{
                    **current.__dict__,
                    "revision": current.revision + 1,
                    "rollover_snapshot_ref": result_ref,
                }
            )
            self._write_record(current, snapshot_ref_out=result_ref)
            return self._rollover_proof(current, result_ref)

    def release(
        self,
        lease: WorkspaceLease,
        *,
        actual_death: WorkspaceDeathProof | None,
        cleanup_fence: int,
    ) -> None:
        key = _workspace_digest(lease.workspace_ref)
        record_path = self._record_path(key)
        with file_lock(record_path, timeout=30.0, stale_after=300.0):
            current = self._read_record(key)
            self._validate_immutable_lease(lease, current)
            if cleanup_fence != current.cleanup_fence:
                raise WorkspaceError("cleanup fence is not the current workspace fence")
            if actual_death is None:
                raise WorkspaceError("actual death proof is required before cleanup")
            self._validate_death(current, actual_death)
            if current.phase == "released":
                return
            path = current.workspace_path
            expected = (
                self._checkout_path(key, current.generation)
                if current.phase == "materialized"
                else self._quarantine_path(key, current.generation)
            )
            if path != expected or path.is_symlink():
                raise WorkspaceError("workspace cleanup target failed containment check")
            if path.exists():
                shutil.rmtree(path)
                self._fsync_directory(path.parent)
            released = WorkspaceLease(
                **{**current.__dict__, "revision": current.revision + 1, "phase": "released"}
            )
            self._write_record(
                released, snapshot_ref_out=self._stored_rollover_ref(key)
            )

    def _record_path(self, key: str) -> Path:
        return self._records / f"{key}.json"

    def _checkout_path(self, key: str, generation: int) -> Path:
        return self._checkouts / f"{key}-g{generation}"

    def _quarantine_path(self, key: str, generation: int) -> Path:
        return self._quarantine / f"{key}-g{generation}"

    def _read_snapshot(self, ref: ProjectSnapshotRef):
        try:
            return self._snapshots.read(ref)
        except Exception as exc:
            raise WorkspaceError(f"input snapshot cannot be verified: {ref.digest}") from exc

    def _capture(self, workspace: Path) -> FilesystemSnapshot:
        try:
            return capture_project(workspace)
        except (OSError, PathContractError) as exc:
            raise WorkspaceError(f"workspace manifest integrity failure: {exc}") from exc

    def _preflight_tree_limits(self, workspace: Path) -> None:
        """Bound a quiescent tree before hashing or allocating file contents."""

        pending = [workspace]
        entries = 0
        total_bytes = 0
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as children:
                    for child in children:
                        if child.name == ".git" and directory == workspace:
                            continue
                        entries += 1
                        if entries > self._limits.max_entries:
                            raise WorkspaceError(
                                "workspace entries exceed rollover admission limit"
                            )
                        metadata = child.stat(follow_symlinks=False)
                        if stat.S_ISDIR(metadata.st_mode):
                            pending.append(Path(child.path))
                        elif stat.S_ISREG(metadata.st_mode):
                            if metadata.st_size > self._limits.max_file_bytes:
                                raise WorkspaceError(
                                    "workspace file exceeds rollover admission limit"
                                )
                            total_bytes += metadata.st_size
                            if total_bytes > self._limits.max_total_bytes:
                                raise WorkspaceError(
                                    "workspace bytes exceed rollover admission limit"
                                )
            except OSError as exc:
                raise WorkspaceError(
                    "workspace changed during rollover limit preflight"
                ) from exc

    def _vcs_tree_digest(self, workspace: Path) -> str:
        """Hash the complete local Git control tree without following links."""

        root = workspace / ".git"
        try:
            root_metadata = root.lstat()
        except OSError as exc:
            raise WorkspaceError("Git control directory is missing") from exc
        if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(
            root_metadata.st_mode
        ):
            raise WorkspaceError("Git control directory is not a real directory")

        digest = hashlib.sha256(b"lockstep.git-control-tree/v1\0")
        entries = 0
        total_bytes = 0

        def walk(directory: Path, relative: str, expected: os.stat_result) -> None:
            nonlocal entries, total_bytes
            try:
                names: list[str] = []
                with os.scandir(directory) as children:
                    for child in children:
                        entries += 1
                        if entries > self._limits.max_entries:
                            raise WorkspaceError(
                                "Git control entries exceed admission limit"
                            )
                        names.append(child.name)
                seen: dict[str, str] = {}
                for name in sorted(names):
                    key = _collision_key(name)
                    previous = seen.get(key)
                    if previous is not None and previous != name:
                        raise WorkspaceError(
                            f"Git control path collision: {previous!r} and {name!r}"
                        )
                    seen[key] = name
                    path = directory / name
                    item = path.lstat()
                    rel = f"{relative}/{name}" if relative else name
                    encoded = rel.encode("utf-8", "surrogateescape")
                    if stat.S_ISLNK(item.st_mode):
                        raise WorkspaceError(f"symlink in Git control state: {rel}")
                    if stat.S_ISDIR(item.st_mode):
                        digest.update(b"directory\0" + encoded + b"\0")
                        digest.update(str(stat.S_IMODE(item.st_mode)).encode() + b"\0")
                        walk(path, rel, item)
                    elif stat.S_ISREG(item.st_mode):
                        if item.st_size > self._limits.max_file_bytes:
                            raise WorkspaceError(
                                "Git control file exceeds admission limit"
                            )
                        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
                            os, "O_NOFOLLOW", 0
                        )
                        descriptor = os.open(path, flags)
                        try:
                            opened = os.fstat(descriptor)
                            if _stat_identity(opened) != _stat_identity(item):
                                raise WorkspaceError(
                                    f"Git control file changed while hashing: {rel}"
                                )
                            file_digest = hashlib.sha256()
                            observed_size = 0
                            while chunk := os.read(descriptor, 1024 * 1024):
                                observed_size += len(chunk)
                                total_bytes += len(chunk)
                                if observed_size > self._limits.max_file_bytes:
                                    raise WorkspaceError(
                                        "Git control file exceeds admission limit"
                                    )
                                if total_bytes > self._limits.max_total_bytes:
                                    raise WorkspaceError(
                                        "Git control bytes exceed admission limit"
                                    )
                                file_digest.update(chunk)
                            if _stat_identity(os.fstat(descriptor)) != _stat_identity(
                                item
                            ):
                                raise WorkspaceError(
                                    f"Git control file changed while hashing: {rel}"
                                )
                        finally:
                            os.close(descriptor)
                        digest.update(b"file\0" + encoded + b"\0")
                        digest.update(str(stat.S_IMODE(item.st_mode)).encode() + b"\0")
                        digest.update(file_digest.digest())
                    else:
                        raise WorkspaceError(
                            f"special file in Git control state: {rel}"
                        )
                if _stat_identity(directory.lstat()) != _stat_identity(expected):
                    raise WorkspaceError(
                        "Git control directory changed while hashing"
                    )
            except OSError as exc:
                raise WorkspaceError(
                    "Git control state changed while hashing"
                ) from exc

        walk(root, "", root_metadata)
        return digest.hexdigest()

    def _verify_exact_input(self, baseline: FilesystemSnapshot, snapshot) -> None:
        entries = tuple(entry for entry in baseline.entries if entry.kind == "file")
        if any(entry.kind == "symlink" for entry in baseline.entries):
            raise WorkspaceError("input workspace manifest contains a symlink")
        expected = tuple((entry.path, entry.blob.sha256) for entry in snapshot.files)
        observed = tuple((entry.path, entry.sha256) for entry in entries)
        if observed != expected:
            raise WorkspaceError("materialized workspace does not match the exact input snapshot")
        if baseline.git is None:
            raise WorkspaceError("materialized workspace lacks Git attestation")

    def _validate_output(
        self, lease: WorkspaceLease, captured: FilesystemSnapshot
    ) -> None:
        if self._vcs_tree_digest(lease.workspace_path) != lease.vcs_baseline_digest:
            raise WorkspaceError("Git control state changed")
        _validate_portable_paths(tuple(entry.path for entry in captured.entries))
        if any(entry.kind == "symlink" for entry in captured.entries):
            raise WorkspaceError("workspace manifest integrity rejects symlink output")
        try:
            allowed = tuple(
                ProjectWritePath.parse(path, lease.workspace_path)
                for path in lease.declared_writes
            )
            comparison = compare_effect(lease.baseline, captured, allowed, "pass")
        except PathContractError as exc:
            raise WorkspaceError(f"workspace manifest integrity failure: {exc}") from exc
        if comparison.integrity_error:
            raise WorkspaceError("; ".join(comparison.reasons))

    @staticmethod
    def _relocated_baseline(
        baseline: FilesystemSnapshot, relocated: FilesystemSnapshot
    ) -> FilesystemSnapshot:
        """Rebind the path-sensitive Git marker after an atomic quarantine move."""

        old_git = baseline.git
        new_git = relocated.git
        if old_git is None or new_git is None:
            raise WorkspaceError("quarantined workspace lost its Git attestation")
        old_control = (
            old_git.head_sha256,
            old_git.index_sha256,
            old_git.worktree_config_sha256,
            old_git.worktree_config_worktree_sha256,
            old_git.common_config_sha256,
            old_git.common_refs_sha256,
        )
        new_control = (
            new_git.head_sha256,
            new_git.index_sha256,
            new_git.worktree_config_sha256,
            new_git.worktree_config_worktree_sha256,
            new_git.common_config_sha256,
            new_git.common_refs_sha256,
        )
        if old_control != new_control:
            raise WorkspaceError("Git control state changed before durable quarantine")
        return FilesystemSnapshot(entries=baseline.entries, git=new_git)

    def _initialize_git(self, workspace: Path) -> None:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self._root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Lockstep",
            "GIT_AUTHOR_EMAIL": "lockstep@invalid",
            "GIT_COMMITTER_NAME": "Lockstep",
            "GIT_COMMITTER_EMAIL": "lockstep@invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        }
        commands = (
            ("git", "-c", f"core.hooksPath={os.devnull}", "init", "--initial-branch=lockstep"),
            ("git", "-c", f"core.hooksPath={os.devnull}", "add", "--all", "--"),
            (
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "commit",
                "--no-gpg-sign",
                "--no-verify",
                "--allow-empty",
                "-m",
                "Lockstep managed workspace baseline",
            ),
        )
        for command in commands:
            try:
                subprocess.run(
                    command,
                    cwd=workspace,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise WorkspaceError("disposable Git workspace initialization failed") from exc

    def _write_record(
        self, lease: WorkspaceLease, *, snapshot_ref_out: str | None
    ) -> None:
        key = _workspace_digest(lease.workspace_ref)
        data = {
            "schema": "lockstep.workspace-lease/v1",
            "workspace_ref": lease.workspace_ref,
            "effect_id": lease.effect_id,
            "request_digest": lease.request_digest,
            "input_snapshot_ref": lease.input_snapshot_ref,
            "generation": lease.generation,
            "revision": lease.revision,
            "cleanup_fence": lease.cleanup_fence,
            "declared_writes": list(lease.declared_writes),
            "baseline": snapshot_to_data(lease.baseline),
            "vcs_baseline_digest": lease.vcs_baseline_digest,
            "phase": lease.phase,
            "snapshot_ref": snapshot_ref_out,
        }
        encoded = _canonical(data)
        path = self._record_path(key)
        descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{key}.", dir=self._records)
        temporary = Path(raw_temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            seal_owner_file(temporary, writable=False)
            os.replace(temporary, path)
            self._fsync_directory(self._records)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _read_record(self, key: str) -> WorkspaceLease:
        path = self._record_path(key)
        try:
            verify_owner_file(path)
            data = json.loads(path.read_bytes())
            if not isinstance(data, dict) or data.get("schema") != "lockstep.workspace-lease/v1":
                raise WorkspaceError("invalid workspace lease record")
            workspace_ref = data["workspace_ref"]
            if _workspace_digest(workspace_ref) != key:
                raise WorkspaceError("workspace lease address mismatch")
            raw_fences = (
                data["generation"],
                data["revision"],
                data["cleanup_fence"],
            )
            if any(type(value) is not int for value in raw_fences):
                raise WorkspaceError("workspace fence values must be integers")
            generation, revision, cleanup_fence = raw_fences
            if min(generation, revision, cleanup_fence) <= 0:
                raise WorkspaceError("workspace fence values must be positive")
            phase = data["phase"]
            if phase not in {"materialized", "quarantined", "released"}:
                raise WorkspaceError("invalid workspace phase")
            declared = data["declared_writes"]
            if not isinstance(declared, list) or not all(
                isinstance(item, str) for item in declared
            ):
                raise WorkspaceError("invalid workspace declared writes")
            expected_path = (
                self._checkout_path(key, generation)
                if phase == "materialized"
                else self._quarantine_path(key, generation)
            )
            return WorkspaceLease(
                workspace_ref=workspace_ref,
                effect_id=_text(data["effect_id"], "effect_id"),
                request_digest=_hex(data["request_digest"], "request digest"),
                input_snapshot_ref=(
                    f"snapshot:{_snapshot_ref(data['input_snapshot_ref']).digest}"
                ),
                generation=generation,
                revision=revision,
                cleanup_fence=cleanup_fence,
                workspace_path=expected_path,
                declared_writes=tuple(declared),
                baseline=snapshot_from_data(data["baseline"]),
                vcs_baseline_digest=_hex(
                    data["vcs_baseline_digest"], "VCS baseline digest"
                ),
                phase=phase,
                rollover_snapshot_ref=(
                    None
                    if data.get("snapshot_ref") is None
                    else f"snapshot:{_snapshot_ref(data['snapshot_ref']).digest}"
                ),
            )
        except FileNotFoundError as exc:
            raise KeyError(f"workspace:{key}") from exc
        except (InsecureStatePath, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkspaceError("invalid or insecure workspace lease record") from exc

    def _stored_rollover_ref(self, key: str) -> str | None:
        path = self._record_path(key)
        try:
            verify_owner_file(path)
            data = json.loads(path.read_bytes())
            value = data.get("snapshot_ref")
        except (OSError, ValueError, TypeError, InsecureStatePath) as exc:
            raise WorkspaceError("cannot read workspace rollover state") from exc
        if value is None:
            return None
        return f"snapshot:{_snapshot_ref(value).digest}"

    def _validate_immutable_lease(
        self, supplied: WorkspaceLease, current: WorkspaceLease
    ) -> None:
        if not isinstance(supplied, WorkspaceLease):
            raise WorkspaceError("typed workspace lease is required")
        supplied_binding = (
            supplied.workspace_ref,
            supplied.effect_id,
            supplied.request_digest,
            supplied.input_snapshot_ref,
            supplied.generation,
            supplied.cleanup_fence,
            supplied.declared_writes,
            supplied.baseline,
            supplied.vcs_baseline_digest,
        )
        current_binding = (
            current.workspace_ref,
            current.effect_id,
            current.request_digest,
            current.input_snapshot_ref,
            current.generation,
            current.cleanup_fence,
            current.declared_writes,
            current.baseline,
            current.vcs_baseline_digest,
        )
        if supplied_binding != current_binding:
            raise WorkspaceError("workspace lease does not match the current fence")

    def _validate_current_lease(
        self, supplied: WorkspaceLease, current: WorkspaceLease
    ) -> None:
        self._validate_immutable_lease(supplied, current)
        if supplied != current:
            raise WorkspaceError("workspace lease revision is stale")

    def _validate_death(
        self, current: WorkspaceLease, proof: WorkspaceDeathProof
    ) -> None:
        if not isinstance(proof, WorkspaceDeathProof):
            raise WorkspaceError("actual death proof is required")
        commitment = {
            "schema": "lockstep.workspace-death/v1",
            "workspace_ref": proof.workspace_ref,
            "generation": proof.generation,
            "cleanup_fence": proof.cleanup_fence,
            "process_identity": proof.process_identity,
        }
        expected = _digest(commitment)
        if (
            proof.digest != expected
            or proof.digest not in self._issued_death_proofs
            or proof.workspace_ref != current.workspace_ref
            or proof.generation != current.generation
            or proof.cleanup_fence != current.cleanup_fence
        ):
            raise WorkspaceError("actual death proof does not match the current fence")

    @staticmethod
    def _rollover_proof(
        lease: WorkspaceLease, snapshot_ref: str
    ) -> WorkspaceRolloverProof:
        return WorkspaceRolloverProof(
            workspace_ref=lease.workspace_ref,
            generation=lease.generation,
            revision=lease.revision,
            cleanup_fence=lease.cleanup_fence,
            input_snapshot_ref=lease.input_snapshot_ref,
            snapshot_ref=snapshot_ref,
        )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
