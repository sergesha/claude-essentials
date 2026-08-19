"""Project-state manifests and fail-closed write-effect comparison.

The manifest deliberately records *what is in the project*, rather than
following paths to wherever a hostile symlink happens to point.  Git control
state is attested separately: it must not become an implicit write surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Iterable, Literal, Sequence
import unicodedata


class PathContractError(ValueError):
    """A path cannot safely be used as a project write surface."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_regular_nofollow(path: Path) -> bytes:
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
        raise PathContractError(f"project entry changed while capturing: {path}") from exc
    try:
        opened = os.fstat(fd)
        if opened.st_dev != expected.st_dev or opened.st_ino != expected.st_ino:
            raise PathContractError(f"project entry changed while capturing: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _optional_regular_sha256(path: Path) -> str | None:
    try:
        return _sha256_bytes(_read_regular_nofollow(path))
    except PathContractError as exc:
        if "missing regular file" in str(exc):
            return None
        raise


def _sha256_regular_nofollow(path: Path, expected: os.stat_result) -> str:
    """Hash the exact regular file lstat'd by the manifest walk.

    An ``lstat`` followed by ``Path.read_bytes`` is a symlink-swap window.
    ``O_NOFOLLOW`` plus inode/device comparison makes that swap a hard
    failure instead of an unrecorded read outside the project.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PathContractError(f"project entry changed while capturing: {path}") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
        ):
            raise PathContractError(f"project entry changed while capturing: {path}")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def _collision_key(part: str) -> str:
    return unicodedata.normalize("NFC", part).casefold()


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
    wanted = _collision_key(requested)
    for child in children:
        if _collision_key(child.name) == wanted and child.name != requested:
            raise PathContractError(
                f"path collision for {requested!r} with existing {child.name!r}"
            )


def _is_windows_alias(part: str) -> bool:
    stripped = part.rstrip(". ")
    if stripped != part or not stripped:
        return True
    base = stripped.split(".", 1)[0].upper()
    return base in {"CON", "PRN", "AUX", "NUL", "CLOCK$"} or (
        len(base) == 4 and base[:3] in {"COM", "LPT"} and base[3] in "123456789"
    )


@dataclass(frozen=True)
class ProjectWritePath:
    relative: PurePosixPath
    is_prefix: bool

    @classmethod
    def parse(cls, raw: str, project: Path) -> "ProjectWritePath":
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise PathContractError("write path must be a non-empty POSIX relative path")
        if "\\" in raw or ":" in raw or any(char in raw for char in '<>"|?*'):
            raise PathContractError(f"platform path alias is not allowed: {raw!r}")
        is_prefix = raw.endswith("/")
        body = raw[:-1] if is_prefix else raw
        if not body or body in {".", ".."} or body.startswith("/"):
            raise PathContractError(f"unsafe write path: {raw!r}")
        parts = body.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise PathContractError(f"write path is not normalized: {raw!r}")
        if any(_is_windows_alias(part) for part in parts):
            raise PathContractError(f"platform path alias is not allowed: {raw!r}")
        relative = PurePosixPath(*parts)
        if relative.parts[0] == ".git":
            raise PathContractError(".git is never a project write surface")

        root = Path(project).resolve()
        if not root.is_dir():
            raise PathContractError(f"project root is not a directory: {root}")
        current = root
        for part in parts:
            _check_component_collision(current, part)
            candidate = current / part
            try:
                mode = os.lstat(candidate).st_mode
            except FileNotFoundError:
                # No later descendant can exist if this does not.
                current = candidate
                continue
            if stat.S_ISLNK(mode):
                raise PathContractError(f"symlink is not allowed in write path: {candidate}")
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
    contents = _read_regular_nofollow(marker)
    if not contents.startswith(key) or b"\x00" in contents:
        raise PathContractError(f"malformed Git marker: {marker}")
    value = contents[len(key):].strip()
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
        if candidate.parent.name != "worktrees" or candidate.parent.parent.name != ".git":
            raise PathContractError(f"escaping Git directory marker: {marker}")
        commondir_marker = candidate / "commondir"
        common_value = _parse_marker(commondir_marker, b"").decode("utf-8", "surrogateescape")
        common_candidate = Path(common_value)
        if common_candidate.is_absolute():
            raise PathContractError(f"escaping commondir marker: {commondir_marker}")
        common_candidate = Path(os.path.normpath(candidate / common_candidate))
        if common_candidate != candidate.parent.parent:
            raise PathContractError(f"escaping commondir marker: {commondir_marker}")
        _ensure_directory_without_symlinks(common_candidate)
        return (
            candidate,
            common_candidate,
            b"file:" + _read_regular_nofollow(marker)
            + b";commondir:" + _read_regular_nofollow(commondir_marker),
        )
    raise PathContractError(f"invalid Git marker: {marker}")


def _metadata_tree_digest(root: Path, names: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for name in names:
        node = root / name
        try:
            node_mode = os.lstat(node).st_mode
        except FileNotFoundError:
            digest.update(f"absent:{name}\0".encode())
            continue
        if stat.S_ISLNK(node_mode):
            raise PathContractError(f"symlink in frozen Git metadata: {node}")
        if stat.S_ISREG(node_mode):
            digest.update(f"file:{name}\0".encode())
            digest.update(_sha256_regular_nofollow(node, os.lstat(node)).encode())
            continue
        if not stat.S_ISDIR(node_mode):
            raise PathContractError(f"invalid frozen Git metadata: {node}")
        for directory, dirs, files in os.walk(node, followlinks=False):
            directory_path = Path(directory)
            for dirname in dirs:
                directory_stat = os.lstat(directory_path / dirname)
                if stat.S_ISLNK(directory_stat.st_mode):
                    raise PathContractError(
                        f"symlink in frozen Git metadata: {directory_path / dirname}"
                    )
                if not stat.S_ISDIR(directory_stat.st_mode):
                    raise PathContractError(
                        f"invalid frozen Git metadata: {directory_path / dirname}"
                    )
            for filename in sorted(files):
                item = directory_path / filename
                relative = item.relative_to(root).as_posix()
                item_stat = os.lstat(item)
                if stat.S_ISLNK(item_stat.st_mode):
                    raise PathContractError(f"symlink in frozen Git metadata: {item}")
                if not stat.S_ISREG(item_stat.st_mode):
                    raise PathContractError(f"invalid frozen Git metadata: {item}")
                digest.update(f"file:{relative}\0".encode())
                digest.update(_sha256_regular_nofollow(item, item_stat).encode())
    return digest.hexdigest()


def capture_git_attestation(project: Path) -> GitAttestation | None:
    root = Path(project).resolve()
    git_dir, common_dir, linkage = _git_dir(root)
    if git_dir is None:
        return None
    assert common_dir is not None
    return GitAttestation(
        head_sha256=_optional_regular_sha256(git_dir / "HEAD"),
        index_sha256=_optional_regular_sha256(git_dir / "index"),
        worktree_config_sha256=_optional_regular_sha256(git_dir / "config"),
        common_config_sha256=_optional_regular_sha256(common_dir / "config"),
        common_refs_sha256=_metadata_tree_digest(common_dir, ("refs", "packed-refs")),
        linkage_sha256=_sha256_bytes(linkage),
    )


def _is_dependency(rel: str, dependencies: Sequence[str]) -> bool:
    return any(rel == root or rel.startswith(root + "/") for root in dependencies)


def capture_project(project: Path, dependencies: Iterable[ProjectWritePath | str] = ()) -> ProjectSnapshot:
    """Capture every filesystem entry below ``project`` without following links."""
    root = Path(project).resolve()
    if not root.is_dir():
        raise PathContractError(f"project root is not a directory: {root}")
    ignored: list[str] = []
    for dependency in dependencies:
        if isinstance(dependency, ProjectWritePath):
            ignored.append(dependency.relative.as_posix())
        else:
            ignored.append(ProjectWritePath.parse(dependency, root).relative.as_posix())

    entries: list[ManifestEntry] = []

    def walk(directory: Path, rel_prefix: str = "") -> None:
        with os.scandir(directory) as children:
            seen: dict[str, str] = {}
            for child in sorted(children, key=lambda item: item.name):
                key = _collision_key(child.name)
                if key in seen and seen[key] != child.name:
                    raise PathContractError(
                        f"project entry collision: {seen[key]!r} and {child.name!r}"
                    )
                seen[key] = child.name
                rel = f"{rel_prefix}/{child.name}" if rel_prefix else child.name
                if rel == ".git" or rel.startswith(".git/") or _is_dependency(rel, ignored):
                    continue
                child_path = Path(child.path)
                mode = os.lstat(child_path).st_mode
                executable = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
                if stat.S_ISLNK(mode):
                    target = os.readlink(child_path)
                    entries.append(ManifestEntry(rel, "symlink", False, _sha256_bytes(os.fsencode(target))))
                elif stat.S_ISDIR(mode):
                    entries.append(ManifestEntry(rel, "directory", executable, None))
                    walk(child_path, rel)
                elif stat.S_ISREG(mode):
                    entries.append(ManifestEntry(rel, "file", executable, _sha256_regular_nofollow(child_path, os.lstat(child_path))))
                else:
                    # Device/socket/FIFO entries have no safe portable project
                    # representation.  Treating them as a regular file would
                    # make the contract lie, so fail before execution routing.
                    raise PathContractError(f"unsupported project entry: {rel}")

    walk(root)
    return ProjectSnapshot(entries=tuple(entries), git=capture_git_attestation(root))


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
    changed = sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))
    reasons: list[str] = []
    for path in changed:
        entry = new.get(path)
        if entry is not None and entry.kind == "symlink":
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
