"""Policy-free stable descriptor observation for authoring files."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass


class DescriptorObservationError(Exception):
    """A regular descriptor could not be observed exactly."""


class DescriptorNotRegular(DescriptorObservationError):
    """The opened descriptor does not identify a regular file."""


class DescriptorTooLarge(DescriptorObservationError):
    """The opened regular file exceeds the caller's byte ceiling."""


class DescriptorSizeMismatch(DescriptorObservationError):
    """The opened regular file does not have the caller's exact size."""


class DescriptorChanged(DescriptorObservationError):
    """The opened regular file changed during its bounded observation."""


@dataclass(frozen=True, slots=True)
class RegularFileObservation:
    content: bytes
    info: os.stat_result


def observe_regular_descriptor(
    descriptor: int,
    *,
    max_bytes: int,
    expected_size: int | None = None,
) -> RegularFileObservation:
    """Consume one descriptor and return one stable bounded regular-file image."""

    if max_bytes < 0:
        os.close(descriptor)
        raise ValueError("descriptor observation byte ceiling must be non-negative")
    try:
        first = os.fstat(descriptor)
        if not stat.S_ISREG(first.st_mode):
            raise DescriptorNotRegular
        if first.st_size > max_bytes:
            raise DescriptorTooLarge
        if expected_size is not None and first.st_size != expected_size:
            raise DescriptorSizeMismatch
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
    if _file_facts(first) != _file_facts(last) or len(content) != first.st_size:
        raise DescriptorChanged
    return RegularFileObservation(content, first)


def _file_facts(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
