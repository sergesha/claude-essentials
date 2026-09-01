"""Provider-neutral immutable project snapshot manifests over blob references."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lockstep.runtime.blobs import BlobRef, BlobStore, DigestMismatch
from lockstep.runtime.locking import file_lock
from lockstep.runtime.owner_state import (
    InsecureStatePath,
    StorageLimitExceeded,
    ensure_owner_directory,
    fsync_owner_directory,
    initialize_owner_state,
    seal_owner_file,
    take_bounded,
    verify_owner_file,
)
from lockstep.runtime.project_paths import (
    PortablePathCollision,
    PortablePathError,
    ProjectTreeLimits,
    validate_portable_project_paths,
)

UnsafeSnapshotPath = PortablePathError


class UndeclaredSnapshotPath(ValueError):
    pass


DuplicateSnapshotPath = PortablePathCollision


class SnapshotStorageError(RuntimeError):
    pass


class FrozenJSONMapping(Mapping[str, object]):
    """Immutable mapping composed solely from recursively frozen tuple data."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, object]) -> None:
        object.__setattr__(self, "_items", tuple(values.items()))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("frozen JSON mapping is immutable")

    def __getitem__(self, key: str) -> object:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self):
        return (key for key, _value in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return repr(dict(self._items))


class FrozenJSONSequence(tuple):
    """Immutable JSON sequence that retains value equality with decoded lists."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return tuple.__eq__(self, tuple(other))
        return NotImplemented

    __hash__ = tuple.__hash__


@dataclass(frozen=True, order=True)
class ProjectSnapshotRef:
    digest: str

    @property
    def sha256(self) -> str:
        return self.digest


@dataclass(frozen=True, order=True)
class SnapshotFile:
    path: str
    blob: BlobRef


@dataclass(frozen=True)
class ProjectSnapshot:
    ref: ProjectSnapshotRef
    files: tuple[SnapshotFile, ...]
    declared_paths: tuple[str, ...]
    provenance: Mapping[str, object]
    previous: ProjectSnapshotRef | None


@dataclass(frozen=True)
class SnapshotLimits(ProjectTreeLimits):
    max_files: int = 10_000
    max_declared_paths: int = 10_000
    max_manifest_bytes: int = 4 * 1024 * 1024
    max_provenance_bytes: int = 256 * 1024
    max_provenance_depth: int = 32
    max_provenance_nodes: int = 10_000
    max_provenance_items: int = 10_000
    max_provenance_scalar_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        super().__post_init__()
        if min(
            self.max_files,
            self.max_declared_paths,
            self.max_manifest_bytes,
            self.max_provenance_bytes,
            self.max_provenance_depth,
            self.max_provenance_nodes,
            self.max_provenance_items,
            self.max_provenance_scalar_bytes,
        ) <= 0:
            raise ValueError("snapshot limits must be positive")


def _canonical(data: object) -> bytes:
    return json.dumps(
        _plain_json(data),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return FrozenJSONMapping({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return FrozenJSONSequence(_freeze_json(item) for item in value)
    return value


def _validate_provenance(value: object, limits: SnapshotLimits) -> None:
    pending = [(value, 1)]
    nodes = 0
    items = 0
    scalar_bytes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > limits.max_provenance_nodes:
            raise StorageLimitExceeded(
                "snapshot provenance nodes exceed "
                f"{limits.max_provenance_nodes} admission limit"
            )
        if depth > limits.max_provenance_depth:
            raise StorageLimitExceeded(
                "snapshot provenance depth exceeds "
                f"{limits.max_provenance_depth} admission limit"
            )
        if isinstance(current, Mapping):
            remaining = limits.max_provenance_items - items
            children = take_bounded(
                current.items(), remaining, "snapshot provenance items"
            )
            items += len(children)
            for key, child in children:
                if not isinstance(key, str):
                    raise TypeError("snapshot provenance must be a string-keyed mapping")
                scalar_bytes += len(key.encode())
                pending.append((child, depth + 1))
        elif isinstance(current, (list, tuple)):
            remaining = limits.max_provenance_items - items
            children = take_bounded(current, remaining, "snapshot provenance items")
            items += len(children)
            pending.extend((child, depth + 1) for child in children)
        elif current is None or isinstance(current, (bool, int, float, str)):
            try:
                scalar_bytes += len(
                    json.dumps(
                        current,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode()
                )
            except (TypeError, ValueError) as exc:
                raise TypeError("snapshot provenance must be JSON serializable") from exc
        else:
            raise TypeError("snapshot provenance must be JSON serializable")
        if scalar_bytes > limits.max_provenance_scalar_bytes:
            raise StorageLimitExceeded(
                "snapshot provenance scalar bytes exceed "
                f"{limits.max_provenance_scalar_bytes} admission limit"
            )


def _validate_digest(digest: str) -> None:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("project snapshot reference must be a lowercase SHA-256 digest")


def _read_manifest_regular(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise SnapshotStorageError(f"snapshot manifest symlink rejected: {path}")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP or path.is_symlink():
            raise SnapshotStorageError(f"snapshot manifest symlink rejected: {path}") from exc
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotStorageError(f"snapshot manifest is not a regular file: {path}")
        if info.st_size > max_bytes:
            raise StorageLimitExceeded(
                f"snapshot manifest exceeds {max_bytes} byte admission limit"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _verify_manifest_owner(path: Path) -> None:
    try:
        verify_owner_file(path)
    except InsecureStatePath as exc:
        if path.is_symlink():
            raise SnapshotStorageError(f"snapshot manifest symlink rejected: {path}") from exc
        raise SnapshotStorageError(f"insecure snapshot manifest: {path}") from exc


class ProjectSnapshotStore:
    def __init__(
        self,
        owner_state_dir: str | Path,
        blob_store: BlobStore | None = None,
        *,
        limits: SnapshotLimits | None = None,
    ) -> None:
        self._owner_state = initialize_owner_state(owner_state_dir)
        self._directory = ensure_owner_directory(self._owner_state, "project-snapshots")
        self._blob_store = blob_store or BlobStore(self._owner_state)
        self._limits = limits or SnapshotLimits()

    @property
    def limits(self) -> SnapshotLimits:
        return self._limits

    def manifest_path(self, ref: ProjectSnapshotRef) -> Path:
        _validate_digest(ref.digest)
        return self._directory / f"{ref.digest}.json"

    def capture(
        self,
        files: Mapping[str, BlobRef] | Iterable[tuple[str, BlobRef]],
        *,
        declared_paths: Iterable[str],
        provenance: Mapping[str, object],
        previous: ProjectSnapshotRef | None = None,
    ) -> ProjectSnapshotRef:
        file_values = files.items() if isinstance(files, Mapping) else files
        raw_files = take_bounded(
            file_values, self._limits.max_files, "snapshot files"
        )
        portable_files = validate_portable_project_paths(
            ((raw_path, "file") for raw_path, _blob in raw_files),
            limits=self._limits,
            label="snapshot entries",
        )
        entries: list[SnapshotFile] = []
        total_bytes = 0
        for (_raw_path, blob), portable in zip(raw_files, portable_files, strict=True):
            if not isinstance(blob, BlobRef):
                raise TypeError("project snapshots contain BlobRef values")
            if blob.size > self._limits.max_file_bytes:
                raise StorageLimitExceeded(
                    "snapshot file exceeds "
                    f"{self._limits.max_file_bytes} byte admission limit"
                )
            total_bytes += blob.size
            if total_bytes > self._limits.max_total_bytes:
                raise StorageLimitExceeded(
                    f"snapshot bytes exceed {self._limits.max_total_bytes} admission limit"
                )
            self._blob_store.read(blob)
            entries.append(SnapshotFile(path=portable.value, blob=blob))
        entries.sort(key=lambda entry: entry.path)
        raw_declarations = take_bounded(
            declared_paths,
            self._limits.max_declared_paths,
            "snapshot declared paths",
        )
        portable_declarations = validate_portable_project_paths(
            (
                (path, "prefix" if isinstance(path, str) and path.endswith("/") else "file")
                for path in raw_declarations
            ),
            limits=self._limits,
            label="snapshot declared path entries",
        )
        declarations = tuple(sorted(item.value for item in portable_declarations))
        for entry in entries:
            if not any(
                entry.path == declaration
                or (declaration.endswith("/") and entry.path.startswith(declaration))
                for declaration in declarations
            ):
                raise UndeclaredSnapshotPath(f"snapshot path {entry.path!r} is not declared")
        if not isinstance(provenance, Mapping):
            raise TypeError("snapshot provenance must be a string-keyed mapping")
        _validate_provenance(provenance, self._limits)
        provenance_data = _freeze_json(provenance)
        try:
            provenance_encoded = _canonical(provenance_data)
        except (TypeError, ValueError) as exc:
            raise TypeError("snapshot provenance must be JSON serializable") from exc
        if len(provenance_encoded) > self._limits.max_provenance_bytes:
            raise StorageLimitExceeded(
                "snapshot provenance exceeds "
                f"{self._limits.max_provenance_bytes} byte admission limit"
            )
        if previous is not None:
            self.read(previous)
        data = {
            "schema": "lockstep.project-snapshot/v1",
            "files": [
                {
                    "path": entry.path,
                    "blob": {"sha256": entry.blob.sha256, "size": entry.blob.size},
                }
                for entry in entries
            ],
            "declared_paths": list(declarations),
            "provenance": provenance_data,
            "previous": previous.digest if previous is not None else None,
        }
        encoded = _canonical(data)
        if len(encoded) > self._limits.max_manifest_bytes:
            raise StorageLimitExceeded(
                f"snapshot manifest exceeds {self._limits.max_manifest_bytes} byte admission limit"
            )
        ref = ProjectSnapshotRef(hashlib.sha256(encoded).hexdigest())
        path = self.manifest_path(ref)
        with file_lock(path, timeout=30.0, stale_after=300.0):
            if path.exists() or path.is_symlink():
                _verify_manifest_owner(path)
                existing = _read_manifest_regular(
                    path, max_bytes=self._limits.max_manifest_bytes
                )
                if existing != encoded:
                    raise DigestMismatch(f"project snapshot manifest collision at {ref.digest}")
            else:
                fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
                tmp = Path(raw_tmp)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    seal_owner_file(tmp, writable=False)
                    os.replace(tmp, path)
                    fsync_owner_directory(path.parent)
                finally:
                    if tmp.exists():
                        tmp.unlink()
        return ref

    def read(self, ref: ProjectSnapshotRef) -> ProjectSnapshot:
        path = self.manifest_path(ref)
        try:
            _verify_manifest_owner(path)
            encoded = _read_manifest_regular(path, max_bytes=self._limits.max_manifest_bytes)
        except FileNotFoundError as exc:
            raise KeyError(ref.digest) from exc
        observed = hashlib.sha256(encoded).hexdigest()
        if observed != ref.digest:
            raise DigestMismatch(
                f"project snapshot manifest mismatch: expected {ref.digest}, observed {observed}"
            )
        try:
            data = json.loads(encoded)
            if data["schema"] != "lockstep.project-snapshot/v1":
                raise ValueError("unknown project snapshot schema")
            raw_entries = data["files"]
            raw_declarations = data["declared_paths"]
            if not isinstance(raw_entries, list) or not isinstance(
                raw_declarations, list
            ):
                raise TypeError("snapshot paths must be arrays")
            if len(raw_entries) > self._limits.max_files:
                raise StorageLimitExceeded("snapshot file count exceeds admission limit")
            if len(raw_declarations) > self._limits.max_declared_paths:
                raise StorageLimitExceeded(
                    "snapshot declared path count exceeds admission limit"
                )
            portable_entries = validate_portable_project_paths(
                ((item["path"], "file") for item in raw_entries),
                limits=self._limits,
                label="snapshot entries",
            )
            entries = tuple(
                SnapshotFile(
                    path=portable.value,
                    blob=BlobRef(item["blob"]["sha256"], int(item["blob"]["size"])),
                )
                for item, portable in zip(raw_entries, portable_entries, strict=True)
            )
            portable_declarations = validate_portable_project_paths(
                (
                    (
                        item,
                        "prefix"
                        if isinstance(item, str) and item.endswith("/")
                        else "file",
                    )
                    for item in raw_declarations
                ),
                limits=self._limits,
                label="snapshot declared path entries",
            )
            declarations = tuple(item.value for item in portable_declarations)
            provenance = data["provenance"]
            if not isinstance(provenance, dict):
                raise TypeError("provenance is not an object")
            _validate_provenance(provenance, self._limits)
            previous = (
                ProjectSnapshotRef(data["previous"]) if data["previous"] is not None else None
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid project snapshot manifest") from exc
        if tuple(sorted(entries, key=lambda entry: entry.path)) != entries:
            raise ValueError("project snapshot entries are not ordered")
        if len({entry.path for entry in entries}) != len(entries):
            raise DuplicateSnapshotPath("duplicate path in project snapshot manifest")
        if sum(entry.blob.size for entry in entries) > self._limits.max_total_bytes:
            raise StorageLimitExceeded("snapshot byte size exceeds admission limit")
        if any(entry.blob.size > self._limits.max_file_bytes for entry in entries):
            raise StorageLimitExceeded("snapshot file size exceeds admission limit")
        provenance_encoded = _canonical(provenance)
        if len(provenance_encoded) > self._limits.max_provenance_bytes:
            raise StorageLimitExceeded("snapshot provenance exceeds admission limit")
        for entry in entries:
            if not any(
                entry.path == declaration
                or (declaration.endswith("/") and entry.path.startswith(declaration))
                for declaration in declarations
            ):
                raise UndeclaredSnapshotPath(f"snapshot path {entry.path!r} is not declared")
            self._blob_store.read(entry.blob)
        return ProjectSnapshot(
            ref=ref,
            files=entries,
            declared_paths=declarations,
            provenance=_freeze_json(provenance),
            previous=previous,
        )
