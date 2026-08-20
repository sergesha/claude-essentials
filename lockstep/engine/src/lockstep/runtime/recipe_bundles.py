"""Immutable recipe dependency bundles and safe compile materialization."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from lockstep.runtime.blobs import BlobRef, BlobStore, DigestMismatch
from lockstep.runtime.locking import file_lock
from lockstep.runtime.owner_state import (
    InsecureStatePath,
    StorageLimitExceeded,
    ensure_owner_directory,
    initialize_owner_state,
    seal_owner_file,
    verify_owner_directory,
    verify_owner_file,
)


class UnsafeBundlePath(ValueError):
    pass


class DuplicateBundlePath(ValueError):
    pass


class SymlinkRejected(ValueError):
    pass


class MaterializationError(RuntimeError):
    pass


class RecipeDependencyError(ValueError):
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
    max_dependency_depth: int = 32

    def __post_init__(self) -> None:
        if min(
            self.max_dependencies,
            self.max_files,
            self.max_total_bytes,
            self.max_manifest_bytes,
            self.max_dependency_depth,
        ) <= 0:
            raise ValueError("recipe bundle limits must be positive")


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


def _source_relative(base: Path, source: Path) -> PurePosixPath:
    try:
        relative = source.absolute().relative_to(base.absolute())
    except ValueError as exc:
        raise UnsafeBundlePath(f"source {source} is outside recipe root {base}") from exc
    return PurePosixPath(_safe_relative(relative.as_posix()))


def _open_project_root(base: Path) -> int:
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


def _read_regular_at(root_fd: int, relative: PurePosixPath, *, max_bytes: int) -> bytes:
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
            return stream.read()
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


def _graph_references(document: Any) -> tuple[str, ...]:
    references: list[str] = []
    if not isinstance(document, Mapping):
        return ()

    def visit_nodes(nodes: Any) -> None:
        if not isinstance(nodes, Mapping):
            return
        for node in nodes.values():
            if isinstance(node, Mapping) and node.get("type") == "subgraph":
                graph = node.get("graph")
                if not isinstance(graph, str):
                    raise RecipeDependencyError("subgraph graph reference must be a path string")
                references.append(graph)

    visit_nodes(document.get("nodes"))

    def visit_flow(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit_flow(item)
            return
        if not isinstance(value, Mapping):
            return
        if "include_graph" in value:
            include = value["include_graph"]
            if isinstance(include, str):
                references.append(include)
            elif isinstance(include, Mapping) and isinstance(include.get("path"), str):
                references.append(include["path"])
            else:
                raise RecipeDependencyError("include_graph reference must contain a path string")
        graph = value.get("graph")
        if isinstance(graph, Mapping):
            visit_nodes(graph.get("nodes"))
        choose = value.get("choose")
        if isinstance(choose, Mapping):
            visit_flow(choose.get("cases"))
            visit_flow(choose.get("default"))
        repeat = value.get("repeat")
        if isinstance(repeat, Mapping):
            visit_flow(repeat.get("do"))
        parallel = value.get("parallel")
        if isinstance(parallel, Mapping):
            visit_flow(parallel.get("branches"))
        # Case/branch labels map to flow sequences. These containers have no
        # block discriminator of their own, so descend through their values.
        if not any(
            key in value
            for key in ("include_graph", "graph", "choose", "repeat", "parallel")
        ):
            for nested in value.values():
                visit_flow(nested)

    visit_flow(document.get("flow"))
    if "include_graph" in document:
        visit_flow({"include_graph": document["include_graph"]})
    return tuple(references)


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
        if len(sources) - 1 > self._limits.max_dependencies:
            raise StorageLimitExceeded(
                f"recipe dependencies exceed {self._limits.max_dependencies} admission limit"
            )
        if len(sources) > self._limits.max_files:
            raise StorageLimitExceeded(
                f"recipe files exceed {self._limits.max_files} admission limit"
            )
        seen: set[str] = set()
        logical_sources: list[tuple[str, PurePosixPath]] = []
        for logical, source in sources:
            if logical in seen:
                raise DuplicateBundlePath(f"duplicate bundle path {logical!r}")
            seen.add(logical)
            logical_sources.append((logical, _source_relative(root_path.parent, source)))

        captured: dict[str, bytes] = {}
        root_fd = _open_project_root(root_path.parent)
        try:
            total = 0
            for logical, relative in logical_sources:
                data = _read_regular_at(
                    root_fd, relative, max_bytes=self._limits.max_total_bytes
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

        self._validate_dependency_dag(root_logical, captured)
        entries = [
            RecipeBundleEntry(
                logical, hashlib.sha256(data).hexdigest(), len(data)
            )
            for logical, data in captured.items()
        ]
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

    def _validate_dependency_dag(self, root: str, captured: Mapping[str, bytes]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(path: str, depth: int) -> None:
            if depth > self._limits.max_dependency_depth:
                raise StorageLimitExceeded(
                    "recipe dependency depth exceeds "
                    f"{self._limits.max_dependency_depth} admission limit"
                )
            if path in visiting:
                raise RecipeDependencyError(f"recipe dependency cycle includes {path!r}")
            if path in visited:
                return
            visiting.add(path)
            try:
                try:
                    document = yaml.safe_load(captured[path])
                except yaml.YAMLError as exc:
                    raise RecipeDependencyError(
                        f"cannot extract dependencies from recipe {path!r}"
                    ) from exc
                for raw_reference in _graph_references(document):
                    try:
                        reference = _safe_relative(
                            (PurePosixPath(path).parent / raw_reference).as_posix()
                        )
                    except (TypeError, UnsafeBundlePath) as exc:
                        raise RecipeDependencyError(
                            f"unsafe recipe dependency reference {raw_reference!r} in {path!r}"
                        ) from exc
                    if reference not in captured:
                        raise RecipeDependencyError(
                            f"undeclared recipe dependency {reference!r} from {path!r}"
                        )
                    visit(reference, depth + 1)
            finally:
                visiting.remove(path)
            visited.add(path)

        visit(root, 1)

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

    def resolve_compile_path(
        self, materialized: MaterializedRecipe, reference: str | os.PathLike[str]
    ) -> Path:
        if materialized.directory.parent != self._materialized:
            raise UnsafeBundlePath("materialization is foreign to this bundle store")
        logical = _safe_relative(reference)
        manifest = self.read_manifest(materialized.bundle)
        if logical not in {entry.path for entry in manifest.files}:
            raise UnsafeBundlePath(f"compile path {logical!r} is not declared by the bundle")
        self._verify_materialization(materialized.directory, manifest)
        return materialized.directory / logical
