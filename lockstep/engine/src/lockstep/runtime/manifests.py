"""Project-state manifests and fail-closed write-effect comparison.

The manifest deliberately records *what is in the project*, rather than
following paths to wherever a hostile symlink happens to point.  Git control
state is attested separately: it must not become an implicit write surface.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from lockstep.runtime.owner_state import StorageLimitExceeded, take_bounded
from lockstep.runtime.project_paths import (
    PortablePathError,
    PortableProjectPath,
    ProjectTreeLimits,
    portable_collision_key,
)


class PathContractError(ValueError):
    """A path cannot safely be used as a project write surface."""


# Narrow test seams for deterministic swap/mutation regressions.  Production
# leaves both as no-ops; the checks after the seam are the security boundary.
def _before_regular_hash() -> None:
    return None


def _before_directory_open() -> None:
    return None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_regular_nofollow(path: Path, *, max_bytes: int | None = None) -> bytes:
    """Read the exact regular file currently named by ``path``."""
    try:
        expected = os.lstat(path)
    except FileNotFoundError as exc:
        raise PathContractError(f"missing regular file: {path}") from exc
    if not stat.S_ISREG(expected.st_mode):
        raise PathContractError(f"expected regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PathContractError(
            f"project entry changed while capturing: {path}"
        ) from exc
    try:
        opened = os.fstat(fd)
        if _identity(opened) != _identity(expected):
            raise PathContractError(f"project entry changed while capturing: {path}")
        chunks: list[bytes] = []
        read_bytes = 0
        while chunk := os.read(fd, 1024 * 1024):
            read_bytes += len(chunk)
            if max_bytes is not None and read_bytes > max_bytes:
                raise StorageLimitExceeded(
                    f"project file exceeds {max_bytes} byte admission limit"
                )
            chunks.append(chunk)
        if _identity(os.fstat(fd)) != _identity(expected):
            raise PathContractError(f"project entry changed while capturing: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _optional_regular_sha256(path: Path, *, max_bytes: int | None = None) -> str | None:
    try:
        return _sha256_bytes(_read_regular_nofollow(path, max_bytes=max_bytes))
    except PathContractError as exc:
        if "missing regular file" in str(exc):
            return None
        raise


def _sha256_regular_nofollow(
    path: Path | str,
    expected: os.stat_result,
    *,
    dir_fd: int | None = None,
    max_bytes: int | None = None,
) -> str:
    """Hash the exact regular file lstat'd by the manifest walk.

    An ``lstat`` followed by ``Path.read_bytes`` is a symlink-swap window.
    ``O_NOFOLLOW`` plus inode/device comparison makes that swap a hard
    failure instead of an unrecorded read outside the project.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise PathContractError(
            f"project entry changed while capturing: {path}"
        ) from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(expected):
            raise PathContractError(f"project entry changed while capturing: {path}")
        _before_regular_hash()
        digest = hashlib.sha256()
        read_bytes = 0
        while chunk := os.read(fd, 1024 * 1024):
            read_bytes += len(chunk)
            if max_bytes is not None and read_bytes > max_bytes:
                raise StorageLimitExceeded(
                    f"project file exceeds {max_bytes} byte admission limit"
                )
            digest.update(chunk)
        if _identity(os.fstat(fd)) != _identity(expected):
            raise PathContractError(f"project entry changed while capturing: {path}")
        return digest.hexdigest()
    finally:
        os.close(fd)


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _check_component_collision(parent: Path, requested: str) -> None:
    """Reject a spelling that aliases an already present sibling.

    This is needed even on a case-sensitive volume: an accepted path has to
    stay unambiguous when the same workflow is later run on a case-insensitive
    or Unicode-normalizing volume.
    """
    try:
        children = list(os.scandir(parent))
    except FileNotFoundError:
        return
    except NotADirectoryError as exc:
        raise PathContractError(f"non-directory ancestor: {parent}") from exc
    wanted = portable_collision_key(requested)
    for child in children:
        if portable_collision_key(child.name) == wanted and child.name != requested:
            raise PathContractError(
                f"path collision for {requested!r} with existing {child.name!r}"
            )


@dataclass(frozen=True)
class ProjectWritePath:
    relative: PurePosixPath
    is_prefix: bool

    @classmethod
    def parse(cls, raw: str, project: Path) -> ProjectWritePath:
        try:
            portable = PortableProjectPath.parse(
                raw, "prefix" if isinstance(raw, str) and raw.endswith("/") else "file"
            )
        except PortablePathError as exc:
            raise PathContractError(str(exc)) from exc
        is_prefix = portable.is_prefix
        relative = portable.relative

        root = Path(project).resolve()
        if not root.is_dir():
            raise PathContractError(f"project root is not a directory: {root}")
        current = root
        for part in relative.parts:
            _check_component_collision(current, part)
            candidate = current / part
            try:
                mode = os.lstat(candidate).st_mode
            except FileNotFoundError:
                # No later descendant can exist if this does not.
                current = candidate
                continue
            if stat.S_ISLNK(mode):
                raise PathContractError(
                    f"symlink is not allowed in write path: {candidate}"
                )
            current = candidate
        return cls(relative=relative, is_prefix=is_prefix)

    def allows(self, relative_path: str) -> bool:
        path = PurePosixPath(relative_path)
        if self.is_prefix:
            return path == self.relative or self.relative in path.parents
        return path == self.relative


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    kind: Literal["file", "directory", "symlink"]
    executable: bool
    sha256: str | None


@dataclass(frozen=True)
class GitAttestation:
    head_sha256: str | None
    index_sha256: str | None
    worktree_config_sha256: str | None
    worktree_config_worktree_sha256: str | None
    common_config_sha256: str | None
    common_refs_sha256: str
    linkage_sha256: str


@dataclass(frozen=True)
class ProjectSnapshot:
    entries: tuple[ManifestEntry, ...]
    git: GitAttestation | None


@dataclass(frozen=True)
class EffectResult:
    integrity_error: bool
    reasons: tuple[str, ...]
    baseline_eligible: bool


def snapshot_to_data(snapshot: ProjectSnapshot) -> dict[str, object]:
    """Stable, JSON-safe durable representation for an ownership boundary."""
    return {
        "entries": [
            {
                "path": entry.path,
                "kind": entry.kind,
                "executable": entry.executable,
                "sha256": entry.sha256,
            }
            for entry in snapshot.entries
        ],
        "git": None
        if snapshot.git is None
        else {
            "head_sha256": snapshot.git.head_sha256,
            "index_sha256": snapshot.git.index_sha256,
            "worktree_config_sha256": snapshot.git.worktree_config_sha256,
            "worktree_config_worktree_sha256": snapshot.git.worktree_config_worktree_sha256,
            "common_config_sha256": snapshot.git.common_config_sha256,
            "common_refs_sha256": snapshot.git.common_refs_sha256,
            "linkage_sha256": snapshot.git.linkage_sha256,
        },
    }


def snapshot_from_data(data: object) -> ProjectSnapshot:
    """Parse an owner-state snapshot defensively; malformed state fails closed."""
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise PathContractError("invalid effect snapshot")
    entries: list[ManifestEntry] = []
    for item in data["entries"]:
        if not isinstance(item, dict):
            raise PathContractError("invalid effect snapshot entry")
        path, kind, executable, digest = (
            item.get("path"),
            item.get("kind"),
            item.get("executable"),
            item.get("sha256"),
        )
        if (
            not isinstance(path, str)
            or kind not in {"file", "directory", "symlink"}
            or not isinstance(executable, bool)
            or (digest is not None and not isinstance(digest, str))
        ):
            raise PathContractError("invalid effect snapshot entry")
        entries.append(ManifestEntry(path, kind, executable, digest))
    raw_git = data.get("git")
    if raw_git is None:
        git = None
    elif isinstance(raw_git, dict):
        required = {
            "head_sha256",
            "index_sha256",
            "worktree_config_sha256",
            "worktree_config_worktree_sha256",
            "common_config_sha256",
            "common_refs_sha256",
            "linkage_sha256",
        }
        if (
            set(raw_git) != required
            or any(
                raw_git[key] is not None and not isinstance(raw_git[key], str)
                for key in required
            )
            or not isinstance(raw_git["common_refs_sha256"], str)
            or not isinstance(raw_git["linkage_sha256"], str)
        ):
            raise PathContractError("invalid Git effect snapshot")
        git = GitAttestation(**raw_git)
    else:
        raise PathContractError("invalid Git effect snapshot")
    return ProjectSnapshot(tuple(entries), git)


def _ensure_directory_without_symlinks(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError as exc:
            raise PathContractError(f"missing Git metadata directory: {path}") from exc
        if stat.S_ISLNK(mode):
            raise PathContractError(f"symlink in Git metadata path: {current}")
        if not stat.S_ISDIR(mode):
            raise PathContractError(f"Git metadata path is not a directory: {current}")


def _parse_marker(marker: Path, key: bytes) -> bytes:
    try:
        mode = os.lstat(marker).st_mode
    except FileNotFoundError as exc:
        raise PathContractError(f"missing Git marker: {marker}") from exc
    if stat.S_ISLNK(mode):
        raise PathContractError(f"symlink Git marker: {marker}")
    if not stat.S_ISREG(mode):
        raise PathContractError(f"invalid Git marker: {marker}")
    contents = _read_regular_nofollow(marker, max_bytes=4096)
    if not contents.startswith(key) or b"\x00" in contents:
        raise PathContractError(f"malformed Git marker: {marker}")
    value = contents[len(key) :].strip()
    if not value or b"\n" in value or b"\r" in value:
        raise PathContractError(f"malformed Git marker: {marker}")
    return value


def _git_dir(project: Path) -> tuple[Path | None, Path | None, bytes]:
    marker = project / ".git"
    try:
        marker_stat = os.lstat(marker)
    except FileNotFoundError:
        return None, None, b"absent"
    if stat.S_ISLNK(marker_stat.st_mode):
        raise PathContractError(f"symlink Git marker: {marker}")
    if stat.S_ISDIR(marker_stat.st_mode):
        _ensure_directory_without_symlinks(marker)
        return marker, marker, b"directory:" + os.fsencode(marker)
    if stat.S_ISREG(marker_stat.st_mode):
        location = _parse_marker(marker, b"gitdir: ").decode("utf-8", "surrogateescape")
        candidate = Path(location)
        if not candidate.is_absolute():
            candidate = Path(os.path.normpath(marker.parent / candidate))
        _ensure_directory_without_symlinks(candidate)
        # A linked worktree's private Git directory is exactly
        # <common>/.git/worktrees/<worktree-id>; anything else lets a project
        # marker redirect attestation reads to arbitrary host metadata.
        if (
            candidate.parent.name != "worktrees"
            or candidate.parent.parent.name != ".git"
        ):
            raise PathContractError(f"escaping Git directory marker: {marker}")
        commondir_marker = candidate / "commondir"
        common_value = _parse_marker(commondir_marker, b"").decode(
            "utf-8", "surrogateescape"
        )
        common_candidate = Path(common_value)
        if common_candidate.is_absolute():
            raise PathContractError(f"escaping commondir marker: {commondir_marker}")
        common_candidate = Path(os.path.normpath(candidate / common_candidate))
        if common_candidate != candidate.parent.parent:
            raise PathContractError(f"escaping commondir marker: {commondir_marker}")
        _ensure_directory_without_symlinks(common_candidate)
        own_linkage_marker = candidate / "gitdir"
        own_linkage = _parse_marker(own_linkage_marker, b"").decode(
            "utf-8", "surrogateescape"
        )
        own_linkage_path = Path(own_linkage)
        if not own_linkage_path.is_absolute():
            own_linkage_path = Path(os.path.normpath(candidate / own_linkage_path))
        expected_project_marker = project / ".git"
        try:
            expected_mode = os.lstat(expected_project_marker).st_mode
        except FileNotFoundError as exc:
            raise PathContractError(
                f"missing linked-worktree marker: {expected_project_marker}"
            ) from exc
        if (
            stat.S_ISLNK(expected_mode)
            or not stat.S_ISREG(expected_mode)
            or own_linkage_path.resolve() != expected_project_marker.resolve()
        ):
            raise PathContractError(
                f"invalid linked-worktree linkage: {own_linkage_marker}"
            )
        return (
            candidate,
            common_candidate,
            b"file:"
            + _read_regular_nofollow(marker, max_bytes=4096)
            + b";commondir:"
            + _read_regular_nofollow(commondir_marker, max_bytes=4096)
            + b";gitdir:"
            + _read_regular_nofollow(own_linkage_marker, max_bytes=4096),
        )
    raise PathContractError(f"invalid Git marker: {marker}")


def _metadata_tree_digest(
    root: Path,
    names: Sequence[str],
    *,
    limits: ProjectTreeLimits | None = None,
) -> str:
    limits = limits or ProjectTreeLimits()
    digest = hashlib.sha256()
    entries = 0
    total_bytes = 0
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )

    def digest_directory(
        directory_fd: int,
        expected: os.stat_result,
        prefix: str,
        depth: int = 0,
    ) -> None:
        nonlocal entries, total_bytes
        if depth > limits.max_depth:
            raise StorageLimitExceeded(
                f"Git metadata depth exceeds {limits.max_depth} admission limit"
            )
        with os.scandir(directory_fd) as children:
            admitted = take_bounded(
                children,
                limits.max_entries - entries,
                "Git metadata entries",
            )
            for child in sorted(admitted, key=lambda item: item.name):
                entries += 1
                relative = f"{prefix}/{child.name}" if prefix else child.name
                item_stat = os.stat(
                    child.name, dir_fd=directory_fd, follow_symlinks=False
                )
                if stat.S_ISLNK(item_stat.st_mode):
                    raise PathContractError(
                        f"symlink in frozen Git metadata: {relative}"
                    )
                if stat.S_ISREG(item_stat.st_mode):
                    if item_stat.st_size > limits.max_file_bytes:
                        raise StorageLimitExceeded(
                            "Git metadata file exceeds admission limit"
                        )
                    remaining_bytes = limits.max_total_bytes - total_bytes
                    total_bytes += item_stat.st_size
                    if total_bytes > limits.max_total_bytes:
                        raise StorageLimitExceeded(
                            "Git metadata bytes exceed aggregate admission limit"
                        )
                    digest.update(f"file:{relative}\0".encode())
                    digest.update(
                        _sha256_regular_nofollow(
                            child.name,
                            item_stat,
                            dir_fd=directory_fd,
                            max_bytes=min(limits.max_file_bytes, remaining_bytes),
                        ).encode()
                    )
                elif stat.S_ISDIR(item_stat.st_mode):
                    try:
                        child_fd = os.open(child.name, flags, dir_fd=directory_fd)
                    except OSError as exc:
                        raise PathContractError(
                            f"Git metadata directory changed while capturing: {relative}"
                        ) from exc
                    try:
                        if _identity(os.fstat(child_fd)) != _identity(item_stat):
                            raise PathContractError(
                                f"Git metadata directory changed while capturing: {relative}"
                            )
                        digest_directory(child_fd, item_stat, relative, depth + 1)
                    finally:
                        os.close(child_fd)
                else:
                    raise PathContractError(f"invalid frozen Git metadata: {relative}")
        if _identity(os.fstat(directory_fd)) != _identity(expected):
            raise PathContractError(
                f"Git metadata directory changed while capturing: {prefix}"
            )

    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise PathContractError(
            f"cannot safely open frozen Git metadata: {root}"
        ) from exc
    try:
        for name in names:
            try:
                node_stat = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                digest.update(f"absent:{name}\0".encode())
                continue
            if stat.S_ISLNK(node_stat.st_mode):
                raise PathContractError(f"symlink in frozen Git metadata: {name}")
            if stat.S_ISREG(node_stat.st_mode):
                if node_stat.st_size > limits.max_file_bytes:
                    raise StorageLimitExceeded(
                        "Git metadata file exceeds admission limit"
                    )
                remaining_bytes = limits.max_total_bytes - total_bytes
                total_bytes += node_stat.st_size
                if total_bytes > limits.max_total_bytes:
                    raise StorageLimitExceeded(
                        "Git metadata bytes exceed aggregate admission limit"
                    )
                digest.update(f"file:{name}\0".encode())
                digest.update(
                    _sha256_regular_nofollow(
                        name,
                        node_stat,
                        dir_fd=root_fd,
                        max_bytes=min(limits.max_file_bytes, remaining_bytes),
                    ).encode()
                )
                continue
            if not stat.S_ISDIR(node_stat.st_mode):
                raise PathContractError(f"invalid frozen Git metadata: {name}")
            try:
                node_fd = os.open(name, flags, dir_fd=root_fd)
            except OSError as exc:
                raise PathContractError(
                    f"Git metadata directory changed while capturing: {name}"
                ) from exc
            try:
                if _identity(os.fstat(node_fd)) != _identity(node_stat):
                    raise PathContractError(
                        f"Git metadata directory changed while capturing: {name}"
                    )
                digest_directory(node_fd, node_stat, name)
            finally:
                os.close(node_fd)
    finally:
        os.close(root_fd)
    return digest.hexdigest()


def capture_git_attestation(
    project: Path, *, limits: ProjectTreeLimits | None = None
) -> GitAttestation | None:
    limits = limits or ProjectTreeLimits()
    root = Path(project).resolve()
    git_dir, common_dir, linkage = _git_dir(root)
    if git_dir is None:
        return None
    assert common_dir is not None
    return GitAttestation(
        head_sha256=_optional_regular_sha256(
            git_dir / "HEAD", max_bytes=limits.max_file_bytes
        ),
        index_sha256=_optional_regular_sha256(
            git_dir / "index", max_bytes=limits.max_file_bytes
        ),
        worktree_config_sha256=_optional_regular_sha256(
            git_dir / "config", max_bytes=limits.max_file_bytes
        ),
        worktree_config_worktree_sha256=_optional_regular_sha256(
            git_dir / "config.worktree", max_bytes=limits.max_file_bytes
        ),
        common_config_sha256=_optional_regular_sha256(
            common_dir / "config", max_bytes=limits.max_file_bytes
        ),
        common_refs_sha256=_metadata_tree_digest(
            common_dir, ("refs", "packed-refs"), limits=limits
        ),
        linkage_sha256=_sha256_bytes(linkage),
    )


def _is_dependency(rel: str, dependencies: Sequence[str]) -> bool:
    return any(rel == root or rel.startswith(root + "/") for root in dependencies)


def capture_project(
    project: Path,
    dependencies: Iterable[ProjectWritePath | str] = (),
    *,
    limits: ProjectTreeLimits | None = None,
) -> ProjectSnapshot:
    """Capture every filesystem entry below ``project`` without following links."""
    limits = limits or ProjectTreeLimits()
    supplied_root = Path(project)
    try:
        if stat.S_ISLNK(os.lstat(supplied_root).st_mode):
            raise PathContractError(
                f"project root may not be a symlink: {supplied_root}"
            )
    except FileNotFoundError:
        pass
    root = supplied_root.resolve()
    if not root.is_dir():
        raise PathContractError(f"project root is not a directory: {root}")
    ignored: list[str] = []
    for dependency in dependencies:
        if isinstance(dependency, ProjectWritePath):
            ignored.append(dependency.relative.as_posix())
        else:
            ignored.append(ProjectWritePath.parse(dependency, root).relative.as_posix())

    entries: list[ManifestEntry] = []
    total_bytes = 0

    def walk(
        directory_fd: int,
        expected_directory: os.stat_result,
        rel_prefix: str = "",
        depth: int = 0,
    ) -> None:
        nonlocal total_bytes
        if depth > limits.max_depth:
            raise StorageLimitExceeded(
                f"project depth exceeds {limits.max_depth} admission limit"
            )
        with os.scandir(directory_fd) as children:
            seen: dict[str, str] = {}
            admitted = take_bounded(
                children,
                limits.max_entries - len(entries),
                "project entries",
            )
            for child in sorted(admitted, key=lambda item: item.name):
                key = portable_collision_key(child.name)
                if key in seen and seen[key] != child.name:
                    raise PathContractError(
                        f"project entry collision: {seen[key]!r} and {child.name!r}"
                    )
                seen[key] = child.name
                rel = f"{rel_prefix}/{child.name}" if rel_prefix else child.name
                if (
                    portable_collision_key(child.name) == portable_collision_key(".git")
                    and child.name != ".git"
                ):
                    raise PathContractError(
                        f"reserved .git alias in project: {child.name!r}"
                    )
                if (
                    rel == ".git"
                    or rel.startswith(".git/")
                    or _is_dependency(rel, ignored)
                ):
                    continue
                if len(entries) >= limits.max_entries:
                    raise StorageLimitExceeded(
                        f"project entries exceed {limits.max_entries} admission limit"
                    )
                try:
                    child_stat = os.stat(
                        child.name, dir_fd=directory_fd, follow_symlinks=False
                    )
                except FileNotFoundError as exc:
                    raise PathContractError(
                        f"project entry changed while capturing: {rel}"
                    ) from exc
                mode = child_stat.st_mode
                executable = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
                if stat.S_ISLNK(mode):
                    try:
                        target = os.readlink(child.name, dir_fd=directory_fd)
                    except OSError as exc:
                        raise PathContractError(
                            f"project entry changed while capturing: {rel}"
                        ) from exc
                    if _identity(
                        os.stat(child.name, dir_fd=directory_fd, follow_symlinks=False)
                    ) != _identity(child_stat):
                        raise PathContractError(
                            f"project entry changed while capturing: {rel}"
                        )
                    target_bytes = os.fsencode(target)
                    total_bytes += len(target_bytes)
                    if total_bytes > limits.max_total_bytes:
                        raise StorageLimitExceeded(
                            "project bytes exceed aggregate admission limit"
                        )
                    entries.append(
                        ManifestEntry(
                            rel, "symlink", False, _sha256_bytes(target_bytes)
                        )
                    )
                elif stat.S_ISDIR(mode):
                    entries.append(ManifestEntry(rel, "directory", executable, None))
                    flags = (
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    _before_directory_open()
                    try:
                        child_fd = os.open(child.name, flags, dir_fd=directory_fd)
                    except OSError as exc:
                        raise PathContractError(
                            f"project directory changed while capturing: {rel}"
                        ) from exc
                    try:
                        opened = os.fstat(child_fd)
                        if _identity(opened) != _identity(child_stat):
                            raise PathContractError(
                                f"project directory changed while capturing: {rel}"
                            )
                        walk(child_fd, child_stat, rel, depth + 1)
                        if _identity(os.fstat(child_fd)) != _identity(child_stat):
                            raise PathContractError(
                                f"project directory changed while capturing: {rel}"
                            )
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(mode):
                    if child_stat.st_size > limits.max_file_bytes:
                        raise StorageLimitExceeded(
                            f"project file exceeds {limits.max_file_bytes} byte admission limit"
                        )
                    remaining_bytes = limits.max_total_bytes - total_bytes
                    total_bytes += child_stat.st_size
                    if total_bytes > limits.max_total_bytes:
                        raise StorageLimitExceeded(
                            "project bytes exceed aggregate admission limit"
                        )
                    entries.append(
                        ManifestEntry(
                            rel,
                            "file",
                            executable,
                            _sha256_regular_nofollow(
                                child.name,
                                child_stat,
                                dir_fd=directory_fd,
                                max_bytes=min(limits.max_file_bytes, remaining_bytes),
                            ),
                        )
                    )
                else:
                    # Device/socket/FIFO entries have no safe portable project
                    # representation.  Treating them as a regular file would
                    # make the contract lie, so fail before execution routing.
                    raise PathContractError(f"unsupported project entry: {rel}")

        if _identity(os.fstat(directory_fd)) != _identity(expected_directory):
            raise PathContractError(
                f"project directory changed while capturing: {rel_prefix or root}"
            )

    root_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_fd = os.open(root, root_flags)
    except OSError as exc:
        raise PathContractError(f"cannot safely open project root: {root}") from exc
    try:
        root_stat = os.fstat(root_fd)
        walk(root_fd, root_stat)
    finally:
        os.close(root_fd)
    return ProjectSnapshot(
        entries=tuple(entries), git=capture_git_attestation(root, limits=limits)
    )


def compare_effect(
    before: ProjectSnapshot,
    after: ProjectSnapshot,
    allowed: Sequence[ProjectWritePath],
    outcome: str,
) -> EffectResult:
    """Return an integrity result for the complete project/Git delta."""
    if outcome not in {"pass", "fail", "error"}:
        raise ValueError(f"invalid effect outcome: {outcome!r}")
    old = {entry.path: entry for entry in before.entries}
    new = {entry.path: entry for entry in after.entries}
    changed = sorted(
        path for path in old.keys() | new.keys() if old.get(path) != new.get(path)
    )
    reasons: list[str] = []
    for path in changed:
        old_entry = old.get(path)
        new_entry = new.get(path)
        if (old_entry is not None and old_entry.kind == "symlink") or (
            new_entry is not None and new_entry.kind == "symlink"
        ):
            reasons.append(f"integrity: symlink output is forbidden: {path}")
        elif not any(surface.allows(path) for surface in allowed):
            reasons.append(f"integrity: undeclared project mutation: {path}")
    if before.git != after.git:
        reasons.append("integrity: Git control state changed")
    return EffectResult(
        integrity_error=bool(reasons),
        reasons=tuple(reasons),
        baseline_eligible=(outcome == "pass" and not reasons),
    )
