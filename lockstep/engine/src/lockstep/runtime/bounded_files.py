"""Descriptor-based bounded reads for untrusted local file paths."""

from __future__ import annotations

import os
from pathlib import Path
import stat


def read_bounded_regular_file(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    missing_ok: bool = False,
    required_uid: int | None = None,
    required_mode: int | None = None,
) -> bytes | None:
    """Read one no-follow regular-file descriptor without blocking on a FIFO."""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{label} must be a regular non-symlink file")
        if required_uid is not None and info.st_uid != required_uid:
            raise PermissionError(f"{label} is not owner-controlled")
        if required_mode is not None and stat.S_IMODE(info.st_mode) != required_mode:
            raise PermissionError(f"{label} has an insecure mode")
        if info.st_size > max_bytes:
            raise ValueError(f"{label} exceeds {max_bytes} bytes")

        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > max_bytes:
            raise ValueError(f"{label} exceeds {max_bytes} bytes")
        return encoded
    finally:
        os.close(descriptor)
