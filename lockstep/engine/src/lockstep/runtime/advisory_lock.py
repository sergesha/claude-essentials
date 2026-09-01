"""One crash-released POSIX advisory-lock primitive for local runtime authority."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class AdvisoryLockTimeout(TimeoutError):
    pass


@contextmanager
def advisory_file_lock(
    path: Path, *, timeout: float | None = None, create: bool = True
) -> Iterator[None]:
    """Hold an owner-only file lock until release or process death.

    The caller owns namespace and parent-directory policy. The lock file is
    persistent; only the kernel lock conveys ownership, so elapsed wall time can
    never make a live critical section stealable.
    """
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise PermissionError("insecure advisory lock file")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    raise AdvisoryLockTimeout("advisory lock timed out") from exc
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
