"""Identity checks at the immutable authoring publication boundary."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from lockstep.authoring_bundle import (
    DestinationImage,
    PathIdentity,
    ProjectCompilationBundle,
    SourceIdentity,
)
from lockstep.errors import AuthoringError
from lockstep.recipe.authority import RecipeLimits
from lockstep.runtime.owner_state import StorageLimitExceeded


@dataclass(frozen=True, slots=True)
class PublishedIdentity:
    path: Path
    device: int
    inode: int
    mode: int
    size: int
    sha256: str


def validate_bundle_preconditions(bundle: ProjectCompilationBundle) -> None:
    """Revalidate the complete immutable plan without changing the project."""

    if not isinstance(bundle, ProjectCompilationBundle):
        raise TypeError("authoring publication requires a ProjectCompilationBundle")
    _validate_bundle_limits(bundle)
    _validate_directory_identity(bundle.project_identity)
    if bundle.project_identity.resolved_path != bundle.resolved_project:
        raise AuthoringError("authoring bundle project identity is inconsistent")
    for source in bundle.sources:
        validate_source(source)
    for before, after in zip(
        bundle.before_images, bundle.after_images, strict=True
    ):
        if before.resolved_path != after.resolved_path:
            raise AuthoringError("authoring destination map changed after planning")
        _validate_destination_shape(bundle.resolved_project, before, after)
        validate_destination_before(before, created_directories={})


def validate_sources(sources: tuple[SourceIdentity, ...]) -> None:
    for source in sources:
        validate_source(source)


def validate_source(source: SourceIdentity) -> None:
    for ancestor in source.ancestors:
        _validate_directory_identity(ancestor)
    content, info = _read_regular(
        source.resolved_path,
        expected_size=source.leaf.size,
    )
    expected = source.leaf
    if _leaf_facts(info) != (
        expected.device,
        expected.inode,
        expected.mode,
        expected.size,
        expected.mtime_ns,
    ):
        raise AuthoringError("workflow source identity changed after planning")
    if content != source.content or _sha256(content) != source.sha256:
        raise AuthoringError("workflow source bytes changed after planning")


def validate_destination_before(
    image: DestinationImage,
    *,
    created_directories: dict[Path, tuple[int, int]],
) -> None:
    _validate_ancestor_chain(image, created_directories=created_directories)
    if image.content is None:
        try:
            image.resolved_path.lstat()
        except FileNotFoundError:
            return
        raise AuthoringError("authoring destination was created after planning")
    leaf = image.leaf
    if leaf is None:
        raise AuthoringError("authoring destination before-image has no leaf identity")
    content, info = _read_regular(
        image.resolved_path,
        expected_size=leaf.size,
    )
    if _leaf_facts(info) != (
        leaf.device,
        leaf.inode,
        leaf.mode,
        leaf.size,
        leaf.mtime_ns,
    ):
        raise AuthoringError("authoring destination identity changed after planning")
    if content != image.content or _sha256(content) != image.sha256:
        raise AuthoringError("authoring destination bytes changed after planning")


def validate_destination_before_at(
    directory_descriptor: int, image: DestinationImage
) -> None:
    """Revalidate one leaf through its already verified parent descriptor."""

    leaf = image.resolved_path.name
    if image.content is None:
        try:
            os.stat(leaf, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise AuthoringError("authoring destination was created after planning")
    expected = image.leaf
    if expected is None:
        raise AuthoringError("authoring destination before-image has no leaf identity")
    content, info = _read_regular_at(
        directory_descriptor,
        leaf,
        image.resolved_path,
        expected_size=expected.size,
    )
    if _leaf_facts(info) != (
        expected.device,
        expected.inode,
        expected.mode,
        expected.size,
        expected.mtime_ns,
    ):
        raise AuthoringError("authoring destination identity changed after planning")
    if content != image.content or _sha256(content) != image.sha256:
        raise AuthoringError("authoring destination bytes changed after planning")


def capture_after_identity_at(
    directory_descriptor: int, image: DestinationImage
) -> PublishedIdentity:
    expected_size = len(image.content or b"")
    content, info = _read_regular_at(
        directory_descriptor,
        image.resolved_path.name,
        image.resolved_path,
        expected_size=expected_size,
    )
    if (
        content != image.content
        or _sha256(content) != image.sha256
        or stat.S_IMODE(info.st_mode) != image.mode
    ):
        raise AuthoringError("published destination does not match its after-image")
    return PublishedIdentity(
        image.resolved_path,
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        _sha256(content),
    )


def validate_after_identity_at(
    directory_descriptor: int, identity: PublishedIdentity
) -> None:
    content, info = _read_regular_at(
        directory_descriptor,
        identity.path.name,
        identity.path,
        expected_size=identity.size,
    )
    if (
        info.st_dev != identity.device
        or info.st_ino != identity.inode
        or info.st_mode != identity.mode
        or info.st_size != identity.size
        or _sha256(content) != identity.sha256
    ):
        raise AuthoringError("transaction-written destination changed unexpectedly")


def _validate_destination_shape(
    project: Path, before: DestinationImage, after: DestinationImage
) -> None:
    try:
        before.resolved_path.relative_to(project)
    except ValueError as exc:
        raise AuthoringError("authoring destination is outside its project") from exc
    if before.resolved_path == project:
        raise AuthoringError("authoring destination cannot replace the project root")
    if before.ancestors != after.ancestors or not before.ancestors:
        raise AuthoringError("authoring destination ancestors are inconsistent")
    if before.ancestors[0].resolved_path != project:
        raise AuthoringError("authoring destination is not project-bound")
    previous = project.parent
    for ancestor in before.ancestors:
        if ancestor.resolved_path.parent != previous:
            raise AuthoringError("authoring destination ancestor chain is incomplete")
        previous = ancestor.resolved_path
    if previous != before.resolved_path.parent:
        try:
            before.resolved_path.parent.relative_to(previous)
        except ValueError as exc:
            raise AuthoringError("authoring destination ancestor chain is invalid") from exc


def _validate_bundle_limits(bundle: ProjectCompilationBundle) -> None:
    limits = RecipeLimits()
    groups = (
        (
            "read set",
            len(bundle.sources),
            tuple(item.content for item in bundle.sources),
        ),
        (
            "before images",
            len(bundle.before_images),
            tuple(
                item.content
                for item in bundle.before_images
                if item.content is not None
            ),
        ),
        (
            "after images",
            len(bundle.after_images),
            tuple(
                item.content
                for item in bundle.after_images
                if item.content is not None
            ),
        ),
    )
    for label, record_count, contents in groups:
        if record_count > limits.max_files:
            raise StorageLimitExceeded(
                f"authoring {label} exceed {limits.max_files} admission limit"
            )
        if sum(map(len, contents)) > limits.max_source_bytes:
            raise StorageLimitExceeded(
                f"authoring {label} exceed the aggregate byte admission limit"
            )


def _validate_ancestor_chain(
    image: DestinationImage,
    *,
    created_directories: dict[Path, tuple[int, int]],
) -> None:
    for ancestor in image.ancestors:
        _validate_directory_identity(ancestor)
    current = image.ancestors[-1].resolved_path
    relative = image.resolved_path.parent.relative_to(current)
    for part in relative.parts:
        current /= part
        expected = created_directories.get(current)
        if expected is None:
            try:
                current.lstat()
            except FileNotFoundError:
                continue
            raise AuthoringError("destination ancestor was created after planning")
        info = current.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or current.resolve(strict=True) != current
            or (info.st_dev, info.st_ino) != expected
        ):
            raise AuthoringError("transaction-created destination ancestor changed")


def _validate_directory_identity(identity: PathIdentity) -> None:
    path = identity.resolved_path
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AuthoringError("authoring directory identity is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or resolved != path
        or (info.st_dev, info.st_ino)
        != (identity.device, identity.inode)
    ):
        raise AuthoringError("authoring directory identity changed after planning")


def _read_regular(
    path: Path, *, expected_size: int
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuthoringError(f"authoring file is unavailable: {path}") from exc
    return _read_descriptor(descriptor, path, expected_size=expected_size)


def _read_regular_at(
    directory_descriptor: int,
    leaf: str,
    path: Path,
    *,
    expected_size: int,
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(leaf, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise AuthoringError(f"authoring file is unavailable: {path}") from exc
    return _read_descriptor(descriptor, path, expected_size=expected_size)


def _read_descriptor(
    descriptor: int, path: Path, *, expected_size: int
) -> tuple[bytes, os.stat_result]:
    try:
        first = os.fstat(descriptor)
        if not stat.S_ISREG(first.st_mode):
            raise AuthoringError(f"authoring path is not a regular file: {path}")
        if first.st_size != expected_size:
            raise AuthoringError(f"authoring file size changed before reading: {path}")
        chunks: list[bytes] = []
        remaining = expected_size + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        last = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _leaf_facts(first) != _leaf_facts(last):
        raise AuthoringError(f"authoring file changed while reading: {path}")
    content = b"".join(chunks)
    if len(content) != expected_size:
        raise AuthoringError(f"authoring file size changed while reading: {path}")
    return content, first


def _leaf_facts(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
