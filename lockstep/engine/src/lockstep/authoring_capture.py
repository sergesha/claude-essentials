"""Bounded filesystem observation for authoring planning."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from lockstep.errors import AuthoringError
from lockstep.runtime.owner_state import StorageLimitExceeded


class _DescriptorObservationError(Exception):
    """A regular descriptor could not be observed exactly."""


class _DescriptorNotRegular(_DescriptorObservationError):
    pass


class _DescriptorTooLarge(_DescriptorObservationError):
    pass


class _DescriptorSizeMismatch(_DescriptorObservationError):
    pass


class _DescriptorChanged(_DescriptorObservationError):
    pass


@dataclass(frozen=True, slots=True)
class _RegularFileObservation:
    content: bytes
    info: os.stat_result


def _observe_regular_descriptor(
    descriptor: int,
    *,
    max_bytes: int,
    expected_size: int | None = None,
) -> _RegularFileObservation:
    """Consume one descriptor and return one stable bounded regular-file image."""

    if max_bytes < 0:
        os.close(descriptor)
        raise ValueError("descriptor observation byte ceiling must be non-negative")
    try:
        first = os.fstat(descriptor)
        if not stat.S_ISREG(first.st_mode):
            raise _DescriptorNotRegular
        if first.st_size > max_bytes:
            raise _DescriptorTooLarge
        if expected_size is not None and first.st_size != expected_size:
            raise _DescriptorSizeMismatch
        chunks: list[bytes] = []
        remaining = first.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        last = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    content = b"".join(chunks)
    if _leaf_facts(first) != _leaf_facts(last) or len(content) != first.st_size:
        raise _DescriptorChanged
    return _RegularFileObservation(content, first)


def capture_regular_file(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    expected: os.stat_result | None = None,
) -> tuple[bytes, os.stat_result]:
    """Read one exact regular non-symlink leaf within a byte ceiling."""

    try:
        first = path.lstat() if expected is None else expected
    except OSError as exc:
        raise AuthoringError(f"{label} cannot be captured") from exc
    if not stat.S_ISREG(first.st_mode):
        raise AuthoringError(f"{label} must be a regular file")
    if first.st_size > max_bytes:
        raise StorageLimitExceeded(f"{label} exceeds the file admission limit")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuthoringError(f"{label} changed while it was captured") from exc
    try:
        observed = _observe_regular_descriptor(
            descriptor,
            max_bytes=max_bytes,
            expected_size=first.st_size,
        )
    except _DescriptorObservationError as exc:
        raise AuthoringError(f"{label} changed while it was captured") from exc
    if _leaf_facts(observed.info) != _leaf_facts(first):
        raise AuthoringError(f"{label} changed while it was captured")
    try:
        named = path.lstat()
    except OSError as exc:
        raise AuthoringError(f"{label} changed while it was captured") from exc
    if _leaf_facts(named) != _leaf_facts(first):
        raise AuthoringError(f"{label} changed while it was captured")
    return observed.content, observed.info


def capture_optional_regular_file(
    path: Path, *, max_bytes: int, label: str
) -> tuple[bytes, os.stat_result] | None:
    """Capture one regular leaf or return its exact observed absence."""

    try:
        expected = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AuthoringError(f"{label} cannot be captured") from exc
    return capture_regular_file(
        path,
        max_bytes=max_bytes,
        label=label,
        expected=expected,
    )


def capture_directory(path: Path, *, label: str) -> os.stat_result:
    """Observe one canonical real directory without minting a DTO identity."""

    try:
        first = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise AuthoringError(f"{label} cannot be captured") from exc
    if not stat.S_ISDIR(first.st_mode):
        raise AuthoringError(f"{label} must be a canonical real directory")
    try:
        resolved = path.resolve(strict=True)
        last = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise AuthoringError(f"{label} cannot be captured") from exc
    if _directory_facts(first) != _directory_facts(last):
        raise AuthoringError(f"{label} changed while it was captured")
    if resolved != path:
        raise AuthoringError(f"{label} must be a canonical real directory")
    return first


def validate_directory(
    path: Path, *, device: int, inode: int, label: str
) -> None:
    """Verify a previously observed directory around another capture."""

    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AuthoringError(f"{label} changed while it was captured") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or resolved != path
        or (info.st_dev, info.st_ino) != (device, inode)
    ):
        raise AuthoringError(f"{label} changed while it was captured")


def _leaf_facts(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _directory_facts(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_ctime_ns
