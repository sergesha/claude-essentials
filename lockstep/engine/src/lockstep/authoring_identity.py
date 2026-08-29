"""Identity checks at the immutable authoring publication boundary."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lockstep.authoring_bundle import (
    DestinationImage,
    PathIdentity,
    ProjectCompilationBundle,
    SourceIdentity,
)
from lockstep.authoring_file_observation import (
    DescriptorChanged,
    DescriptorNotRegular,
    DescriptorObservationError,
    DescriptorSizeMismatch,
    DescriptorTooLarge,
    RegularFileObservation,
    observe_regular_descriptor,
)
from lockstep.authoring_limits import validate_authoring_contents
from lockstep.errors import AuthoringError


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
        expected.ctime_ns,
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
        leaf.ctime_ns,
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
        expected.ctime_ns,
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


def classify_destination_ownership_at(
    directory_descriptor: int,
    before: DestinationImage,
    after: PublishedIdentity,
) -> Literal["before", "after"]:
    """Classify one reserved mutation from a single verified-parent observation."""

    expected_sizes = {after.size}
    if before.content is not None:
        expected_sizes.add(len(before.content))
    observed = _observe_destination_at(
        directory_descriptor,
        before.resolved_path,
        max_bytes=max(expected_sizes),
    )
    if observed is not None and observed.info.st_size not in expected_sizes:
        raise AuthoringError(
            "reserved authoring destination matches neither transaction image"
        )
    before_matches = _matches_before_image(observed, before)
    after_matches = _matches_published_image(observed, after)
    if before_matches == after_matches:
        raise AuthoringError(
            "reserved authoring destination ownership is ambiguous"
        )
    return "before" if before_matches else "after"


def _observe_destination_at(
    directory_descriptor: int,
    path: Path,
    *,
    max_bytes: int,
) -> RegularFileObservation | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path.name, flags, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AuthoringError(f"authoring destination is unavailable: {path}") from exc
    try:
        return observe_regular_descriptor(descriptor, max_bytes=max_bytes)
    except DescriptorObservationError as exc:
        raise AuthoringError(
            "reserved authoring destination matches neither transaction image"
        ) from exc


def _matches_before_image(
    observed: RegularFileObservation | None,
    before: DestinationImage,
) -> bool:
    if before.content is None:
        return observed is None
    expected = before.leaf
    if observed is None or expected is None:
        return False
    return (
        _leaf_facts(observed.info)
        == (
            expected.device,
            expected.inode,
            expected.mode,
            expected.size,
            expected.mtime_ns,
            expected.ctime_ns,
        )
        and observed.content == before.content
        and _sha256(observed.content) == before.sha256
    )


def _matches_published_image(
    observed: RegularFileObservation | None,
    after: PublishedIdentity,
) -> bool:
    if observed is None:
        return False
    info = observed.info
    return (
        info.st_dev == after.device
        and info.st_ino == after.inode
        and info.st_mode == after.mode
        and info.st_size == after.size
        and _sha256(observed.content) == after.sha256
    )


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
    groups = (
        (
            "authoring read set",
            (item.content for item in bundle.sources),
        ),
        (
            "authoring before images",
            (
                item.content
                for item in bundle.before_images
            ),
        ),
        (
            "authoring after images",
            (
                item.content
                for item in bundle.after_images
            ),
        ),
    )
    for label, contents in groups:
        validate_authoring_contents(label, contents)


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
        observed = observe_regular_descriptor(
            descriptor,
            max_bytes=expected_size,
            expected_size=expected_size,
        )
    except DescriptorNotRegular as exc:
        raise AuthoringError(f"authoring path is not a regular file: {path}") from exc
    except (DescriptorTooLarge, DescriptorSizeMismatch) as exc:
        raise AuthoringError(
            f"authoring file size changed before reading: {path}"
        ) from exc
    except DescriptorChanged as exc:
        raise AuthoringError(f"authoring file changed while reading: {path}") from exc
    return observed.content, observed.info


def _leaf_facts(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
