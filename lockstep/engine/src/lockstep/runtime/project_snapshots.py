"""Provider-neutral immutable project snapshot manifests over blob references."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat

from lockstep.runtime.blobs import BlobRef, BlobStore, DigestMismatch
from lockstep.runtime.locking import file_lock


class UnsafeSnapshotPath(ValueError):
    pass


class UndeclaredSnapshotPath(ValueError):
    pass


class DuplicateSnapshotPath(ValueError):
    pass


class SnapshotStorageError(RuntimeError):
    pass


class FrozenJSONMapping(dict):
    """JSON-serializable mapping with no mutation surface after construction."""

    def __init__(self, *args, **kwargs) -> None:
        if getattr(self, "_sealed", False):
            raise TypeError("frozen JSON mapping is immutable")
        dict.__init__(self, *args, **kwargs)
        self._sealed = True

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("frozen JSON mapping is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


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


def _canonical(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return FrozenJSONMapping({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenJSONSequence(_freeze_json(item) for item in value)
    return value


def _safe_path(raw: str, *, allow_prefix: bool = False) -> str:
    if not raw or "\\" in raw or "\x00" in raw or any(char in raw for char in "*?["):
        raise UnsafeSnapshotPath(f"unsafe snapshot path {raw!r}")
    is_prefix = allow_prefix and raw.endswith("/")
    body = raw[:-1] if is_prefix else raw
    path = PurePosixPath(body)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise UnsafeSnapshotPath(f"unsafe snapshot path {raw!r}")
    normalized = path.as_posix()
    if normalized in ("", ".") or (path.parts and path.parts[0].endswith(":")):
        raise UnsafeSnapshotPath(f"unsafe snapshot path {raw!r}")
    return normalized + ("/" if is_prefix else "")


def _validate_digest(digest: str) -> None:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("project snapshot reference must be a lowercase SHA-256 digest")


def _read_manifest_regular(path: Path) -> bytes:
    if path.is_symlink():
        raise SnapshotStorageError(f"snapshot manifest symlink rejected: {path}")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP or path.is_symlink():
            raise SnapshotStorageError(f"snapshot manifest symlink rejected: {path}") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SnapshotStorageError(f"snapshot manifest is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


class ProjectSnapshotStore:
    def __init__(self, owner_state_dir: str | Path, blob_store: BlobStore | None = None) -> None:
        self._directory = Path(owner_state_dir) / "project-snapshots"
        self._blob_store = blob_store or BlobStore(owner_state_dir)

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
        raw_files = list(files.items()) if isinstance(files, Mapping) else list(files)
        entries: list[SnapshotFile] = []
        seen: set[str] = set()
        for raw_path, blob in raw_files:
            path = _safe_path(raw_path)
            if path in seen:
                raise DuplicateSnapshotPath(f"duplicate snapshot path {path!r}")
            if not isinstance(blob, BlobRef):
                raise TypeError("project snapshots contain BlobRef values")
            self._blob_store.read(blob)
            seen.add(path)
            entries.append(SnapshotFile(path=path, blob=blob))
        entries.sort(key=lambda entry: entry.path)
        normalized_declarations = [
            _safe_path(path, allow_prefix=True) for path in declared_paths
        ]
        if len(set(normalized_declarations)) != len(normalized_declarations):
            raise DuplicateSnapshotPath("duplicate declared snapshot path")
        declarations = tuple(sorted(normalized_declarations))
        for entry in entries:
            if not any(
                entry.path == declaration
                or (declaration.endswith("/") and entry.path.startswith(declaration))
                for declaration in declarations
            ):
                raise UndeclaredSnapshotPath(f"snapshot path {entry.path!r} is not declared")
        if not isinstance(provenance, Mapping) or any(not isinstance(key, str) for key in provenance):
            raise TypeError("snapshot provenance must be a string-keyed mapping")
        provenance_data = dict(provenance)
        try:
            _canonical(provenance_data)
        except (TypeError, ValueError) as exc:
            raise TypeError("snapshot provenance must be JSON serializable") from exc
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
        ref = ProjectSnapshotRef(hashlib.sha256(encoded).hexdigest())
        path = self.manifest_path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path, timeout=30.0, stale_after=300.0):
            if path.exists() or path.is_symlink():
                if _read_manifest_regular(path) != encoded:
                    raise DigestMismatch(f"project snapshot manifest collision at {ref.digest}")
            else:
                tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
                try:
                    tmp.write_bytes(encoded)
                    tmp.chmod(0o444)
                    os.replace(tmp, path)
                finally:
                    if tmp.exists():
                        tmp.unlink()
        return ref

    def read(self, ref: ProjectSnapshotRef) -> ProjectSnapshot:
        path = self.manifest_path(ref)
        try:
            encoded = _read_manifest_regular(path)
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
            entries = tuple(
                SnapshotFile(
                    path=_safe_path(item["path"]),
                    blob=BlobRef(item["blob"]["sha256"], int(item["blob"]["size"])),
                )
                for item in data["files"]
            )
            declarations = tuple(
                _safe_path(item, allow_prefix=True) for item in data["declared_paths"]
            )
            provenance = data["provenance"]
            if not isinstance(provenance, dict):
                raise ValueError("provenance is not an object")
            previous = (
                ProjectSnapshotRef(data["previous"]) if data["previous"] is not None else None
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid project snapshot manifest") from exc
        if tuple(sorted(entries, key=lambda entry: entry.path)) != entries:
            raise ValueError("project snapshot entries are not ordered")
        if len({entry.path for entry in entries}) != len(entries):
            raise DuplicateSnapshotPath("duplicate path in project snapshot manifest")
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
