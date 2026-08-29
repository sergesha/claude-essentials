"""Bounded filesystem observation for authoring planning."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from lockstep.authoring_file_observation import (
    DescriptorObservationError,
    observe_regular_descriptor,
)
from lockstep.errors import AuthoringError
from lockstep.runtime.owner_state import StorageLimitExceeded


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
        observed = observe_regular_descriptor(
            descriptor,
            max_bytes=max_bytes,
            expected_size=first.st_size,
        )
    except DescriptorObservationError as exc:
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
