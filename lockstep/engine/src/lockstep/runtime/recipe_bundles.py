"""Immutable recipe dependency bundles and safe compile materialization."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile

from lockstep.runtime.blobs import BlobRef, BlobStore, DigestMismatch
from lockstep.runtime.locking import file_lock


class UnsafeBundlePath(ValueError):
    pass


class DuplicateBundlePath(ValueError):
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


def _canonical(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _safe_relative(raw: str | os.PathLike[str]) -> str:
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


def _read_regular(base: Path, source: Path) -> bytes:
    if base.is_symlink():
        raise SymlinkRejected(f"symlink input root rejected: {base}")
    base_real = base.resolve(strict=True)
    try:
        relative = source.absolute().relative_to(base.absolute())
    except ValueError as exc:
        raise UnsafeBundlePath(f"source {source} is outside recipe root {base}") from exc
    current = base
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise
        if stat.S_ISLNK(mode):
            raise SymlinkRejected(f"symlink input rejected: {current}")
    try:
        source.resolve(strict=True).relative_to(base_real)
    except ValueError as exc:
        raise UnsafeBundlePath(f"source {source} resolves outside recipe root {base}") from exc
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        if source.is_symlink():
            raise SymlinkRejected(f"symlink input rejected: {source}") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"recipe dependency is not a regular file: {source}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _read_manifest_regular(path: Path) -> bytes:
    if path.is_symlink():
        raise SymlinkRejected(f"symlink manifest rejected: {path}")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP or path.is_symlink():
            raise SymlinkRejected(f"symlink manifest rejected: {path}") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise MaterializationError(f"recipe bundle manifest is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


class RecipeBundleStore:
    def __init__(self, owner_state_dir: str | Path, blob_store: BlobStore | None = None) -> None:
        self._owner_state = Path(owner_state_dir)
        self._blob_store = blob_store or BlobStore(self._owner_state)
        self._manifests = self._owner_state / "recipe-bundles"
        self._materialized = self._owner_state / "recipe-materializations"

    def manifest_path(self, ref: RecipeBundleRef) -> Path:
        _validate_digest(ref.digest)
        return self._manifests / f"{ref.digest}.json"

    def _dependency_entries(self, root: Path, dependencies) -> list[tuple[str, Path]]:
        base = root.parent
        if isinstance(dependencies, Mapping):
            raw_entries = list(dependencies.items())
        else:
            raw_entries = []
            for item in dependencies:
                if isinstance(item, tuple) and len(item) == 2:
                    raw_entries.append(item)
                else:
                    raw_entries.append((item, base / os.fspath(item)))
        entries: list[tuple[str, Path]] = []
        for logical, source in raw_entries:
            logical_path = _safe_relative(logical)
            entries.append((logical_path, Path(source)))
        return entries

    def capture(self, root: str | Path, dependencies: Iterable | Mapping) -> RecipeBundleRef:
        root_path = Path(root)
        root_logical = _safe_relative(root_path.name)
        sources = [(root_logical, root_path)] + self._dependency_entries(root_path, dependencies)
        seen: set[str] = set()
        entries: list[RecipeBundleEntry] = []
        for logical, source in sources:
            if logical in seen:
                raise DuplicateBundlePath(f"duplicate bundle path {logical!r}")
            seen.add(logical)
            data = _read_regular(root_path.parent, source)
            blob = self._blob_store.put(data)
            entries.append(RecipeBundleEntry(logical, blob.sha256, blob.size))
        entries.sort(key=lambda entry: entry.path)
        manifest_data = {
            "schema": "lockstep.recipe-bundle/v1",
            "root": root_logical,
            "files": [
                {"path": entry.path, "sha256": entry.sha256, "size": entry.size}
                for entry in entries
            ],
        }
        encoded = _canonical(manifest_data)
        ref = RecipeBundleRef(hashlib.sha256(encoded).hexdigest())
        path = self.manifest_path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path, timeout=30.0, stale_after=300.0):
            if path.exists() or path.is_symlink():
                if _read_manifest_regular(path) != encoded:
                    raise DigestMismatch(f"recipe bundle manifest collision at {ref.digest}")
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

    def read_manifest(self, ref: RecipeBundleRef) -> RecipeBundleManifest:
        path = self.manifest_path(ref)
        try:
            encoded = _read_manifest_regular(path)
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
            root = _safe_relative(data["root"])
            entries = tuple(
                RecipeBundleEntry(
                    path=_safe_relative(item["path"]),
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
        return RecipeBundleManifest(root=root, files=entries)

    @staticmethod
    def _make_tree_read_only(directory: Path) -> None:
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise SymlinkRejected(f"symlink in materialization: {path}")
            if path.is_file():
                path.chmod(0o444)
        directories = [path for path in directory.rglob("*") if path.is_dir()]
        for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            path.chmod(0o555)
        directory.chmod(0o555)

    def _verify_materialization(self, directory: Path, manifest: RecipeBundleManifest) -> None:
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
            else:
                raise MaterializationError(f"unexpected materialized entry {relative!r}")
        if directory.stat().st_mode & 0o222:
            raise MaterializationError("materialized root directory is writable")
        if observed != set(expected):
            raise MaterializationError("materialized recipe is incomplete")
        if observed_directories != expected_directories:
            raise MaterializationError("materialized directory layout does not match manifest")

    def materialize_for_compile(self, ref: RecipeBundleRef) -> MaterializedRecipe:
        manifest = self.read_manifest(ref)
        target = self._materialized / ref.digest
        target.parent.mkdir(parents=True, exist_ok=True)
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
