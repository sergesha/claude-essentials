"""Recoverable publication of immutable artifacts into an exact project tree.

Publication is deliberately a separate external-effect port.  ``prepare`` only
writes owner-state intent; ``apply_or_recover`` is the sole project mutation
boundary and every retry decides from the durable journal plus current bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from lockstep.runtime.artifacts import ArtifactRef, ArtifactRegistry
from lockstep.runtime.blobs import BlobRef, BlobStore
from lockstep.runtime.locking import file_lock
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.owner_state import (
    InsecureStatePath,
    StorageLimitExceeded,
    ensure_owner_directory,
    initialize_owner_state,
    seal_owner_file,
    take_bounded,
    verify_owner_file,
)
from lockstep.runtime.project_paths import (
    PortableProjectPath,
    ProjectTreeLimits,
    validate_portable_project_paths,
)

_HEX = frozenset("0123456789abcdef")
_MISSING = object()


class PublicationError(RuntimeError):
    pass


class PublicationConflict(PublicationError):
    pass


class PublicationJournalError(PublicationError):
    pass


@dataclass(frozen=True)
class PublicationLimits:
    max_entries: int = 32
    max_journal_bytes: int = 1024 * 1024
    max_file_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if min(self.max_entries, self.max_journal_bytes, self.max_file_bytes) <= 0:
            raise ValueError("publication limits must be positive")


@dataclass(frozen=True)
class PublicationEntry:
    artifact_ref: ArtifactRef
    destination: str
    transformation: str = "identity"

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_ref", ArtifactRef.parse(self.artifact_ref))
        path = PortableProjectPath.parse(self.destination, "file")
        if self.transformation != "identity":
            raise ValueError("only identity artifact publication is supported")
        object.__setattr__(self, "destination", path.value)


@dataclass(frozen=True)
class PublicationRequest:
    effect_id: str
    public_run_id: str
    project_identity: str
    definition_digest: str
    coordinate: NativeCoordinate
    descriptor_digest: str
    grant_digest: str
    publisher_binding_digest: str
    consent_ref: str
    approval_generation: int
    policy_epoch: int
    config_epoch: int
    parent_capability_generation: int
    entries: tuple[PublicationEntry, ...]
    request_digest: str

    @classmethod
    def build(
        cls,
        *,
        effect_id: str,
        public_run_id: str,
        project_identity: str,
        definition_digest: str,
        coordinate: NativeCoordinate,
        descriptor_digest: str,
        grant_digest: str,
        publisher_binding_digest: str,
        consent_ref: str,
        approval_generation: int,
        policy_epoch: int,
        config_epoch: int,
        parent_capability_generation: int,
        entries: Iterable[PublicationEntry],
    ) -> PublicationRequest:
        values = take_bounded(entries, 32, "publication entries")
        if not values:
            raise ValueError("publication requires at least one entry")
        if any(not isinstance(item, PublicationEntry) for item in values):
            raise TypeError("publication entries must be closed values")
        validate_portable_project_paths(
            ((item.destination, "file") for item in values),
            limits=ProjectTreeLimits(max_entries=32),
            label="publication destinations",
        )
        scalar = {
            "effect_id": _text(effect_id, "effect_id"),
            "public_run_id": _text(public_run_id, "public_run_id"),
            "project_identity": _text(project_identity, "project_identity"),
            "definition_digest": _digest(definition_digest, "definition digest"),
            "descriptor_digest": _digest(descriptor_digest, "descriptor digest"),
            "grant_digest": _digest(grant_digest, "grant digest"),
            "publisher_binding_digest": _digest(
                publisher_binding_digest, "publisher binding digest"
            ),
            "consent_ref": _text(consent_ref, "consent_ref"),
        }
        generations = {
            "approval_generation": _counter(approval_generation, "approval generation"),
            "policy_epoch": _counter(policy_epoch, "policy epoch"),
            "config_epoch": _counter(config_epoch, "config epoch"),
            "parent_capability_generation": _counter(
                parent_capability_generation, "parent capability generation"
            ),
        }
        coordinate_data = _coordinate_data(coordinate)
        data = {
            "schema": "lockstep.publication-request/v1",
            **scalar,
            **generations,
            "coordinate": coordinate_data,
            "entries": [_entry_data(item) for item in values],
        }
        request_digest = hashlib.sha256(_canonical(data)).hexdigest()
        return cls(
            effect_id=scalar["effect_id"],
            public_run_id=scalar["public_run_id"],
            project_identity=scalar["project_identity"],
            definition_digest=scalar["definition_digest"],
            coordinate=coordinate,
            descriptor_digest=scalar["descriptor_digest"],
            grant_digest=scalar["grant_digest"],
            publisher_binding_digest=scalar["publisher_binding_digest"],
            consent_ref=scalar["consent_ref"],
            approval_generation=generations["approval_generation"],
            policy_epoch=generations["policy_epoch"],
            config_epoch=generations["config_epoch"],
            parent_capability_generation=generations[
                "parent_capability_generation"
            ],
            entries=values,
            request_digest=request_digest,
        )


@dataclass(frozen=True)
class PreparedPublication:
    journal_digest: str
    request_digest: str
    publisher_binding_digest: str


@dataclass(frozen=True)
class PublicationReceipt:
    journal_digest: str
    request_digest: str
    phase: str


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise ValueError(f"{label} must be bounded non-empty text")
    return value


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _counter(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _coordinate_data(value: NativeCoordinate) -> dict[str, str]:
    if not isinstance(value, NativeCoordinate):
        raise TypeError("publication coordinate must be NativeCoordinate")
    data = {
        "thread_id": value.thread_id,
        "checkpoint_ns": value.checkpoint_ns,
        "checkpoint_id": value.checkpoint_id,
        "task_id": value.task_id,
        "interrupt_id": value.interrupt_id,
    }
    for field, item in data.items():
        if not isinstance(item, str) or (field != "checkpoint_ns" and not item):
            raise ValueError(f"coordinate {field} must be bounded text")
        if len(item.encode()) > 4096:
            raise ValueError(f"coordinate {field} must be bounded text")
    return data


def _entry_data(entry: PublicationEntry) -> dict[str, str]:
    return {
        "artifact_ref": str(entry.artifact_ref),
        "destination": entry.destination,
        "transformation": entry.transformation,
    }


def _after_replacement(_direction: str, _index: int) -> None:
    """Crash-injection seam used to prove recovery after every replacement."""


class ProjectPublisher:
    def __init__(
        self,
        owner_state_dir: str | Path,
        project_root: str | Path,
        registry: ArtifactRegistry,
        blob_store: BlobStore,
        *,
        limits: PublicationLimits | None = None,
    ) -> None:
        self._owner_state = initialize_owner_state(owner_state_dir)
        self._limits = limits or PublicationLimits()
        self._registry = registry
        self._blobs = blob_store
        root = Path(project_root)
        try:
            root_info = root.lstat()
        except OSError as exc:
            raise PublicationConflict("project root is unavailable") from exc
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise PublicationConflict("project root must be a real directory")
        self._project = root
        binding = {
            "schema": "lockstep.project-publisher-binding/v1",
            "root": str(root.resolve(strict=True)),
            "device": root_info.st_dev,
            "inode": root_info.st_ino,
        }
        self.binding_digest = hashlib.sha256(_canonical(binding)).hexdigest()
        self._directory = ensure_owner_directory(
            self._owner_state, f"publications/{self.binding_digest}"
        )
        self._journals = ensure_owner_directory(
            self._owner_state, f"publications/{self.binding_digest}/journals"
        )
        self._active = self._directory / "active.json"

    def journal_path(self, handle: PreparedPublication) -> Path:
        self._validate_handle(handle)
        return self._journals / f"{handle.journal_digest}.json"

    def prepare(self, request: PublicationRequest) -> PreparedPublication:
        if not isinstance(request, PublicationRequest):
            raise TypeError("publication request must be a closed value")
        if request.publisher_binding_digest != self.binding_digest:
            raise PublicationConflict("publication request names another publisher")
        plan: list[dict[str, object]] = []
        root_fd = self._open_root()
        try:
            for entry in request.entries:
                record = self._registry.read(entry.artifact_ref)
                if (
                    record.public_run_id != request.public_run_id
                    or record.project_identity != request.project_identity
                    or record.definition_digest != request.definition_digest
                ):
                    raise PublicationConflict(
                        "artifact provenance does not match the publication request"
                    )
                parent_fd, leaf = self._open_parent(root_fd, entry.destination)
                try:
                    before = self._current_blob(parent_fd, leaf)
                finally:
                    os.close(parent_fd)
                plan.append(
                    {
                        **_entry_data(entry),
                        "after": _blob_data(record.blob),
                        "before": None if before is None else _blob_data(before),
                    }
                )
        finally:
            os.close(root_fd)
        journal = {
            "schema": "lockstep.publication-journal/v1",
            "phase": "prepared",
            "request_digest": request.request_digest,
            "publisher_binding_digest": self.binding_digest,
            "request": _request_data(request),
            "plan": plan,
        }
        encoded = _canonical(journal)
        if len(encoded) > self._limits.max_journal_bytes:
            raise StorageLimitExceeded("publication journal exceeds admission limit")
        journal_digest = hashlib.sha256(
            _canonical(
                {
                    "schema": "lockstep.publication-journal-key/v1",
                    "request_digest": request.request_digest,
                    "publisher_binding_digest": self.binding_digest,
                }
            )
        ).hexdigest()
        handle = PreparedPublication(
            journal_digest, request.request_digest, self.binding_digest
        )
        with file_lock(self._active, timeout=30.0, stale_after=300.0):
            active = self._read_active_optional()
            if active is not None and active != journal_digest:
                old = self._read_journal_digest(active)
                if old["phase"] not in {"applied", "rolled_back"}:
                    raise PublicationConflict(
                        "another publication is active for this project"
                    )
            journal_path = self.journal_path(handle)
            if journal_path.exists() or journal_path.is_symlink():
                existing = self._read_journal(handle)
                if existing != journal:
                    raise PublicationConflict(
                        "publication request no longer has the same preflight state"
                    )
            else:
                self._write_atomic(journal_path, encoded, mutable=True)
            self._write_atomic(
                self._active,
                _canonical(
                    {
                        "schema": "lockstep.active-publication/v1",
                        "journal_digest": journal_digest,
                    }
                ),
                mutable=True,
            )
        return handle

    def apply_or_recover(
        self, handle: PreparedPublication
    ) -> PublicationReceipt:
        path = self.journal_path(handle)
        with file_lock(path, timeout=30.0, stale_after=300.0):
            journal = self._read_journal(handle)
            phase = journal["phase"]
            if phase == "applied":
                return self._receipt(handle, "applied")
            if phase not in {"prepared", "applying"}:
                raise PublicationConflict(f"cannot apply publication in phase {phase}")
            if phase == "prepared":
                journal["phase"] = "applying"
                self._store_journal(path, journal)
            self._apply_plan(journal["plan"], direction="apply")
            journal["phase"] = "applied"
            self._store_journal(path, journal)
            return self._receipt(handle, "applied")

    def rollback_or_recover(
        self, handle: PreparedPublication
    ) -> PublicationReceipt:
        path = self.journal_path(handle)
        with file_lock(path, timeout=30.0, stale_after=300.0):
            journal = self._read_journal(handle)
            phase = journal["phase"]
            if phase == "rolled_back":
                return self._receipt(handle, "rolled_back")
            if phase not in {"applying", "rollback_pending"}:
                raise PublicationConflict(
                    f"cannot roll back publication in phase {phase}"
                )
            if phase == "applying":
                journal["phase"] = "rollback_pending"
                self._store_journal(path, journal)
            self._apply_plan(journal["plan"], direction="rollback")
            journal["phase"] = "rolled_back"
            self._store_journal(path, journal)
            return self._receipt(handle, "rolled_back")

    def _apply_plan(self, raw_plan: object, *, direction: str) -> None:
        plan = self._validate_plan(raw_plan)
        indices = range(len(plan)) if direction == "apply" else range(len(plan) - 1, -1, -1)
        root_fd = self._open_root()
        try:
            for index in indices:
                item = plan[index]
                parent_fd, leaf = self._open_parent(root_fd, item["destination"])
                try:
                    before = item["before"]
                    after = item["after"]
                    expected = before if direction == "apply" else after
                    desired = after if direction == "apply" else before
                    current = self._current_blob(parent_fd, leaf)
                    if _same_blob(current, desired):
                        continue
                    if not _same_blob(current, expected):
                        raise PublicationConflict(
                            f"publication destination changed: {item['destination']}"
                        )
                    self._replace(parent_fd, leaf, desired)
                    _after_replacement(direction, index)
                finally:
                    os.close(parent_fd)
        finally:
            os.close(root_fd)

    def _replace(self, parent_fd: int, leaf: str, desired: BlobRef | None) -> None:
        if desired is None:
            try:
                os.unlink(leaf, dir_fd=parent_fd)
            except FileNotFoundError:
                return
            os.fsync(parent_fd)
            return
        data = self._blobs.read(desired)
        temporary = f".lockstep-publish-{secrets.token_hex(16)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
            os.replace(temporary, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass

    def _open_root(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._project, flags)
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                raise PublicationConflict("project root is not a directory")
            return fd
        except OSError as exc:
            raise PublicationConflict("project root cannot be opened safely") from exc

    def _open_parent(self, root_fd: int, destination: str) -> tuple[int, str]:
        path = PurePosixPath(destination)
        current = os.dup(root_fd)
        try:
            for part in path.parts[:-1]:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                next_fd = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = next_fd
            return current, path.name
        except OSError as exc:
            os.close(current)
            raise PublicationConflict(
                f"publication parent is missing or unsafe: {destination}"
            ) from exc

    def _current_blob(self, parent_fd: int, leaf: str) -> BlobRef | None:
        try:
            info = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise PublicationConflict("publication destination is not a regular file")
        if info.st_size > self._limits.max_file_bytes:
            raise StorageLimitExceeded("publication destination exceeds admission limit")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(leaf, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise PublicationConflict("publication destination cannot be read safely") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > self._limits.max_file_bytes:
                raise PublicationConflict("publication destination changed during read")
            data = os.read(fd, self._limits.max_file_bytes + 1)
            if len(data) > self._limits.max_file_bytes:
                raise StorageLimitExceeded("publication destination exceeds admission limit")
        finally:
            os.close(fd)
        return self._blobs.put(data)

    def _read_active_optional(self) -> str | None:
        if not self._active.exists() and not self._active.is_symlink():
            return None
        data = self._read_json(self._active)
        if (
            set(data) != {"schema", "journal_digest"}
            or data["schema"] != "lockstep.active-publication/v1"
        ):
            raise PublicationJournalError("invalid active publication pointer")
        try:
            return _digest(data["journal_digest"], "journal digest")
        except ValueError as exc:
            raise PublicationJournalError("invalid active publication pointer") from exc

    def _read_journal_digest(self, digest: str) -> dict[str, object]:
        handle = PreparedPublication(digest, "0" * 64, self.binding_digest)
        return self._read_json(self.journal_path(handle))

    def _read_journal(self, handle: PreparedPublication) -> dict[str, object]:
        data = self._read_json(self.journal_path(handle))
        try:
            if set(data) != {
                "schema", "phase", "request_digest", "publisher_binding_digest",
                "request", "plan",
            }:
                raise ValueError
            if data["schema"] != "lockstep.publication-journal/v1":
                raise ValueError
            if data["phase"] not in {
                "prepared", "applying", "rollback_pending", "applied", "rolled_back"
            }:
                raise ValueError
            if data["request_digest"] != handle.request_digest:
                raise ValueError
            if data["publisher_binding_digest"] != self.binding_digest:
                raise ValueError
            self._validate_plan(data["plan"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PublicationJournalError("invalid publication journal") from exc
        return data

    def _validate_plan(self, value: object) -> list[dict[str, object]]:
        if not isinstance(value, list) or not value or len(value) > self._limits.max_entries:
            raise PublicationJournalError("invalid publication plan")
        checked: list[dict[str, object]] = []
        for item in value:
            if not isinstance(item, dict) or set(item) != {
                "artifact_ref", "destination", "transformation", "before", "after"
            }:
                raise PublicationJournalError("invalid publication plan entry")
            try:
                ArtifactRef.parse(item["artifact_ref"])
                PortableProjectPath.parse(item["destination"], "file")
                if item["transformation"] != "identity":
                    raise ValueError
                before = _blob_from_data(item["before"])
                after = _blob_from_data(item["after"])
                if after is None:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise PublicationJournalError("invalid publication plan entry") from exc
            checked.append({**item, "before": before, "after": after})
        return checked

    def _read_json(self, path: Path) -> dict[str, object]:
        try:
            verify_owner_file(path)
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > self._limits.max_journal_bytes:
                    raise PublicationJournalError("publication journal is not bounded")
                encoded = os.read(fd, self._limits.max_journal_bytes + 1)
            finally:
                os.close(fd)
            value = json.loads(encoded)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except PublicationJournalError:
            raise
        except (OSError, InsecureStatePath, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PublicationJournalError(f"cannot read publication journal: {path}") from exc

    def _store_journal(self, path: Path, journal: dict[str, object]) -> None:
        encoded = _canonical(journal)
        if len(encoded) > self._limits.max_journal_bytes:
            raise StorageLimitExceeded("publication journal exceeds admission limit")
        self._write_atomic(path, encoded, mutable=True)

    def _write_atomic(self, path: Path, encoded: bytes, *, mutable: bool) -> None:
        fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            seal_owner_file(temporary, writable=mutable)
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _validate_handle(self, handle: PreparedPublication) -> None:
        if not isinstance(handle, PreparedPublication):
            raise TypeError("publication handle must be a closed value")
        _digest(handle.journal_digest, "journal digest")
        _digest(handle.request_digest, "request digest")
        if handle.publisher_binding_digest != self.binding_digest:
            raise PublicationConflict("publication handle names another publisher")

    def _receipt(self, handle: PreparedPublication, phase: str) -> PublicationReceipt:
        return PublicationReceipt(handle.journal_digest, handle.request_digest, phase)


def _blob_data(ref: BlobRef) -> dict[str, object]:
    return {"sha256": ref.sha256, "size": ref.size}


def _blob_from_data(value: object) -> BlobRef | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"sha256", "size"}:
        raise ValueError("invalid blob reference")
    digest = _digest(value["sha256"], "blob digest")
    if type(value["size"]) is not int or value["size"] < 0:
        raise ValueError("invalid blob size")
    return BlobRef(digest, value["size"])


def _same_blob(left: BlobRef | None, right: BlobRef | None) -> bool:
    return left == right


def _request_data(request: PublicationRequest) -> dict[str, object]:
    return {
        "schema": "lockstep.publication-request/v1",
        "effect_id": request.effect_id,
        "public_run_id": request.public_run_id,
        "project_identity": request.project_identity,
        "definition_digest": request.definition_digest,
        "coordinate": _coordinate_data(request.coordinate),
        "descriptor_digest": request.descriptor_digest,
        "grant_digest": request.grant_digest,
        "publisher_binding_digest": request.publisher_binding_digest,
        "consent_ref": request.consent_ref,
        "approval_generation": request.approval_generation,
        "policy_epoch": request.policy_epoch,
        "config_epoch": request.config_epoch,
        "parent_capability_generation": request.parent_capability_generation,
        "entries": [_entry_data(item) for item in request.entries],
    }
