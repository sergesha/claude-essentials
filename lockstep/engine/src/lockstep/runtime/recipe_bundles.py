"""Immutable recipe dependency bundles and safe compile materialization."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from lockstep.runtime.blobs import BlobRef, BlobStore, DigestMismatch
from lockstep.runtime.locking import file_lock
from lockstep.runtime.owner_state import (
    InsecureStatePath,
    StorageLimitExceeded,
    ensure_owner_directory,
    initialize_owner_state,
    seal_owner_file,
    take_bounded,
    verify_owner_directory,
    verify_owner_file,
)


class UnsafeBundlePath(ValueError):
    pass


class DuplicateBundlePath(ValueError):
    pass


class InvalidDependencyDAG(ValueError):
    pass


class SymlinkRejected(ValueError):
    pass


class MaterializationError(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class RecipeBundleRef:
    digest: str

    @property
    def sha256(self) -> str:
        return self.digest


@dataclass(frozen=True, order=True)
class RecipeBundleEntry:
    path: str
    sha256: str
    size: int

    @property
    def blob(self) -> BlobRef:
        return BlobRef(self.sha256, self.size)


@dataclass(frozen=True)
class RecipeBundleManifest:
    root: str
    files: tuple[RecipeBundleEntry, ...]


@dataclass(frozen=True)
class MaterializedRecipe:
    bundle: RecipeBundleRef
    directory: Path
    source_path: Path


@dataclass(frozen=True)
class RecipeBundleLimits:
    max_dependencies: int = 255
    max_files: int = 256
    max_total_bytes: int = 64 * 1024 * 1024
    max_manifest_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if min(
            self.max_dependencies,
            self.max_files,
            self.max_total_bytes,
            self.max_manifest_bytes,
        ) <= 0:
            raise ValueError("recipe bundle limits must be positive")


def _canonical(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def safe_recipe_relative_path(raw: str | os.PathLike[str]) -> str:
    text = os.fspath(raw)
    if not text or "\\" in text or "\x00" in text:
        raise UnsafeBundlePath(f"unsafe bundle path {text!r}")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise UnsafeBundlePath(f"unsafe bundle path {text!r}")
    normalized = path.as_posix()
    if normalized in ("", ".") or (path.parts and path.parts[0].endswith(":")):
        raise UnsafeBundlePath(f"unsafe bundle path {text!r}")
    return normalized


def _validate_digest(digest: str) -> None:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("recipe bundle reference must be a lowercase SHA-256 digest")


def open_recipe_source_root(base: Path) -> int:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise MaterializationError("descriptor-relative recipe capture is supported only on POSIX")
    if base.is_symlink():
        raise SymlinkRejected(f"symlink input root rejected: {base}")
    try:
        descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise SymlinkRejected(f"cannot safely hold recipe root: {base}") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise UnsafeBundlePath(f"recipe root is not a directory: {base}")
    return descriptor


def read_recipe_source_file(
    root_fd: int, relative: PurePosixPath, *, max_bytes: int
) -> bytes:
    parent_fd = os.dup(root_fd)
    try:
        for part in relative.parts[:-1]:
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise SymlinkRejected(
                    f"linked or invalid recipe directory component rejected: {relative}"
                ) from exc
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            descriptor = os.open(
                relative.parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise SymlinkRejected(f"linked recipe input rejected: {relative}") from exc
    finally:
        os.close(parent_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"recipe dependency is not a regular file: {relative}")
        if info.st_size > max_bytes:
            raise StorageLimitExceeded(
                f"recipe bundle exceeds {max_bytes} byte admission limit"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise StorageLimitExceeded(
                f"recipe bundle exceeds {max_bytes} byte admission limit"
            )
        return data
    finally:
        os.close(descriptor)


def _read_manifest_regular(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise SymlinkRejected(f"symlink manifest rejected: {path}")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP or path.is_symlink():
            raise SymlinkRejected(f"symlink manifest rejected: {path}") from exc
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise MaterializationError(f"recipe bundle manifest is not a regular file: {path}")
        if info.st_size > max_bytes:
            raise StorageLimitExceeded(
                f"recipe bundle manifest exceeds {max_bytes} byte admission limit"
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
            raise SymlinkRejected(f"symlink manifest rejected: {path}") from exc
        raise MaterializationError(f"insecure recipe bundle manifest: {path}") from exc


@dataclass(frozen=True)
class ValidatedDependencyDAG:
    """Exact files admitted by the authoritative recipe dependency loader."""

    root: str
    files: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.root, str) or not isinstance(self.files, tuple):
            raise TypeError("validated dependency DAG requires a string root and tuple files")
        root = safe_recipe_relative_path(self.root)
        normalized = tuple(safe_recipe_relative_path(path) for path in self.files)
        if root != self.root or normalized != self.files:
            raise UnsafeBundlePath("validated dependency DAG paths must be canonical")
        if len(set(normalized)) != len(normalized):
            raise DuplicateBundlePath("validated dependency DAG contains duplicate paths")
        if root not in normalized:
            raise InvalidDependencyDAG("validated dependency DAG root is absent from files")

    @classmethod
    def from_validated(
        cls,
        root: str,
        files: Iterable[str],
        *,
        max_files: int = 256,
        max_dependencies: int = 255,
    ) -> ValidatedDependencyDAG:
        """Construct the storage boundary after recipe semantics were validated."""

        if max_files <= 0 or max_dependencies < 0:
            raise ValueError("validated dependency DAG limits are invalid")
        max_admitted = min(max_files, max_dependencies + 1)
        label = (
            "recipe dependencies"
            if max_dependencies + 1 < max_files
            else "recipe files"
        )
        raw_files = take_bounded(files, max_admitted, label)
        normalized_root = safe_recipe_relative_path(root)
        normalized_files = tuple(safe_recipe_relative_path(path) for path in raw_files)
        return cls(normalized_root, normalized_files)


class RecipeBundleStore:
    def __init__(
        self,
        owner_state_dir: str | Path,
        blob_store: BlobStore | None = None,
        *,
        limits: RecipeBundleLimits | None = None,
    ) -> None:
        self._owner_state = initialize_owner_state(owner_state_dir)
        self._blob_store = blob_store or BlobStore(self._owner_state)
        self._manifests = ensure_owner_directory(self._owner_state, "recipe-bundles")
        self._materialized = ensure_owner_directory(
            self._owner_state, "recipe-materializations"
        )
        self._limits = limits or RecipeBundleLimits()

    @classmethod
    def open_readonly(
        cls,
        owner_state_dir: str | Path,
        *,
        limits: RecipeBundleLimits | None = None,
    ) -> RecipeBundleStore:
        """Open existing trusted bundle state without creating any path.

        Hook and diagnostic projections must reuse the same manifest and
        materialization verification as compilation, but they are observers:
        a missing directory is an integrity failure, never an invitation to
        initialize storage.
        """
        owner_state = Path(owner_state_dir)
        verify_owner_directory(owner_state)
        manifests = owner_state / "recipe-bundles"
        materialized = owner_state / "recipe-materializations"
        verify_owner_directory(manifests)
        verify_owner_directory(materialized)

        store = cls.__new__(cls)
        store._owner_state = owner_state
        store._blob_store = None
        store._manifests = manifests
        store._materialized = materialized
        store._limits = limits or RecipeBundleLimits()
        return store

    def manifest_path(self, ref: RecipeBundleRef) -> Path:
        _validate_digest(ref.digest)
        return self._manifests / f"{ref.digest}.json"

    def capture(
        self, source_root: str | Path, dependency_dag: ValidatedDependencyDAG
    ) -> RecipeBundleRef:
        if not isinstance(dependency_dag, ValidatedDependencyDAG):
            raise TypeError("recipe capture requires a ValidatedDependencyDAG")
        if len(dependency_dag.files) - 1 > self._limits.max_dependencies:
            raise StorageLimitExceeded(
                f"recipe dependencies exceed {self._limits.max_dependencies} admission limit"
            )
        if len(dependency_dag.files) > self._limits.max_files:
            raise StorageLimitExceeded(
                f"recipe files exceed {self._limits.max_files} admission limit"
            )

        captured: dict[str, bytes] = {}
        root_fd = open_recipe_source_root(Path(source_root))
        try:
            total = 0
            for logical in dependency_dag.files:
                relative = PurePosixPath(logical)
                data = read_recipe_source_file(
                    root_fd,
                    relative,
                    max_bytes=self._limits.max_total_bytes - total,
                )
                total += len(data)
                if total > self._limits.max_total_bytes:
                    raise StorageLimitExceeded(
                        "recipe bundle exceeds "
                        f"{self._limits.max_total_bytes} byte admission limit"
                    )
                captured[logical] = data
        finally:
            os.close(root_fd)

        entries = [
            RecipeBundleEntry(
                logical, hashlib.sha256(data).hexdigest(), len(data)
            )
            for logical, data in captured.items()
        ]
        entries.sort(key=lambda entry: entry.path)
        manifest_data = {
            "schema": "lockstep.recipe-bundle/v1",
            "root": dependency_dag.root,
            "files": [
                {"path": entry.path, "sha256": entry.sha256, "size": entry.size}
                for entry in entries
            ],
        }
        encoded = _canonical(manifest_data)
        if len(encoded) > self._limits.max_manifest_bytes:
            raise StorageLimitExceeded(
                "recipe bundle manifest exceeds "
                f"{self._limits.max_manifest_bytes} byte admission limit"
            )
        ref = RecipeBundleRef(hashlib.sha256(encoded).hexdigest())
        path = self.manifest_path(ref)
        with file_lock(path, timeout=30.0, stale_after=300.0):
            if path.exists() or path.is_symlink():
                _verify_manifest_owner(path)
                existing = _read_manifest_regular(
                    path, max_bytes=self._limits.max_manifest_bytes
                )
                if existing != encoded:
                    raise DigestMismatch(f"recipe bundle manifest collision at {ref.digest}")
            else:
                for entry in entries:
                    self._blob_store.put(
                        captured[entry.path], expected_sha256=entry.sha256
                    )
                fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
                tmp = Path(raw_tmp)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    seal_owner_file(tmp, writable=False)
                    os.replace(tmp, path)
                finally:
                    if tmp.exists():
                        tmp.unlink()
        return ref

    def read_manifest(self, ref: RecipeBundleRef) -> RecipeBundleManifest:
        path = self.manifest_path(ref)
        try:
            _verify_manifest_owner(path)
            encoded = _read_manifest_regular(path, max_bytes=self._limits.max_manifest_bytes)
        except FileNotFoundError as exc:
            raise KeyError(ref.digest) from exc
        observed = hashlib.sha256(encoded).hexdigest()
        if observed != ref.digest:
            raise DigestMismatch(
                f"recipe bundle manifest mismatch: expected {ref.digest}, observed {observed}"
            )
        try:
            data = json.loads(encoded)
            if data["schema"] != "lockstep.recipe-bundle/v1":
                raise ValueError("unknown recipe bundle schema")
            root = safe_recipe_relative_path(data["root"])
            entries = tuple(
                RecipeBundleEntry(
                    path=safe_recipe_relative_path(item["path"]),
                    sha256=item["sha256"],
                    size=int(item["size"]),
                )
                for item in data["files"]
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MaterializationError("invalid recipe bundle manifest") from exc
        if tuple(sorted(entries, key=lambda entry: entry.path)) != entries:
            raise MaterializationError("recipe bundle entries are not ordered")
        if len({entry.path for entry in entries}) != len(entries) or root not in {
            entry.path for entry in entries
        }:
            raise MaterializationError("recipe bundle paths are duplicate or root is absent")
        if (
            len(entries) > self._limits.max_files
            or len(entries) - 1 > self._limits.max_dependencies
        ):
            raise StorageLimitExceeded("recipe bundle file count exceeds admission limit")
        if sum(entry.size for entry in entries) > self._limits.max_total_bytes:
            raise StorageLimitExceeded("recipe bundle byte size exceeds admission limit")
        return RecipeBundleManifest(root=root, files=entries)

    @staticmethod
    def _make_tree_read_only(directory: Path) -> None:
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise SymlinkRejected(f"symlink in materialization: {path}")
            if path.is_file():
                path.chmod(0o400)
        directories = [path for path in directory.rglob("*") if path.is_dir()]
        for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            path.chmod(0o500)
        directory.chmod(0o500)

    def _verify_materialization(self, directory: Path, manifest: RecipeBundleManifest) -> None:
        if directory.stat().st_mode & 0o222:
            raise MaterializationError("materialized root directory is writable")
        verify_owner_directory(directory)
        expected = {entry.path: entry for entry in manifest.files}
        expected_directories: set[str] = set()
        for entry in manifest.files:
            parent = PurePosixPath(entry.path).parent
            while parent != PurePosixPath("."):
                expected_directories.add(parent.as_posix())
                parent = parent.parent
        observed: set[str] = set()
        observed_directories: set[str] = set()
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise SymlinkRejected(f"symlink in materialization: {path}")
            relative = path.relative_to(directory).as_posix()
            if path.is_dir():
                observed_directories.add(relative)
                if path.stat().st_mode & 0o222:
                    raise MaterializationError(
                        f"materialized directory is writable: {relative}"
                    )
                verify_owner_directory(path)
            elif path.is_file():
                observed.add(relative)
                entry = expected.get(relative)
                if entry is None:
                    raise MaterializationError(f"unexpected materialized file {relative!r}")
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != entry.sha256 or len(data) != entry.size:
                    raise DigestMismatch(f"materialized file mismatch: {relative}")
                if path.stat().st_mode & 0o222:
                    raise MaterializationError(f"materialized file is writable: {relative}")
                verify_owner_file(path)
            else:
                raise MaterializationError(f"unexpected materialized entry {relative!r}")
        if observed != set(expected):
            raise MaterializationError("materialized recipe is incomplete")
        if observed_directories != expected_directories:
            raise MaterializationError("materialized directory layout does not match manifest")

    def read_materialization(self, ref: RecipeBundleRef) -> MaterializedRecipe:
        """Verify and return an existing immutable materialization read-only."""
        manifest = self.read_manifest(ref)
        target = self._materialized / ref.digest
        if not target.is_dir() or target.is_symlink():
            raise MaterializationError("immutable recipe materialization is unavailable")
        self._verify_materialization(target, manifest)
        return MaterializedRecipe(
            bundle=ref,
            directory=target,
            source_path=target / manifest.root,
        )

    def materialize_for_compile(self, ref: RecipeBundleRef) -> MaterializedRecipe:
        manifest = self.read_manifest(ref)
        target = self._materialized / ref.digest
        ensure_owner_directory(
            self._owner_state, target.parent.relative_to(self._owner_state)
        )
        with file_lock(target, timeout=30.0, stale_after=300.0):
            if target.exists():
                if not target.is_dir() or target.is_symlink():
                    raise SymlinkRejected(f"invalid materialization root: {target}")
                self._verify_materialization(target, manifest)
            else:
                temp = Path(tempfile.mkdtemp(prefix=f".{ref.digest}.", dir=target.parent))
                try:
                    for entry in manifest.files:
                        destination = temp / entry.path
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(self._blob_store.read(entry.blob))
                    self._make_tree_read_only(temp)
                    os.replace(temp, target)
                finally:
                    if temp.exists():
                        for path in temp.rglob("*"):
                            try:
                                path.chmod(0o700)
                            except OSError:
                                pass
                        temp.chmod(0o700)
                        shutil.rmtree(temp)
        return MaterializedRecipe(bundle=ref, directory=target, source_path=target / manifest.root)
