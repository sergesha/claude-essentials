"""Portable cross-process lock.

OS-AGNOSTIC: acquisition is an atomic ``O_CREAT | O_EXCL`` create on a
sidecar file — the one primitive POSIX and Windows both give us through
stdlib ``os``. No fcntl/flock/msvcrt anywhere in this project; this module
is the only place a lock primitive appears.

The lock is ALWAYS a sidecar (``<target>.lock``), never ``target`` itself:
callers publish via ``os.replace``, which swaps inodes, so a lock held on
the replaced file would be void.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockTimeout(RuntimeError):
    """Raised when the lock could not be acquired within the timeout."""


def _lock_path(target: Path) -> Path:
    return target.parent / (target.name + ".lock")


def _is_stale(path: Path, stale_after: float) -> bool:
    # Staleness is decided by the lock file's MODIFICATION TIME, never by
    # parsing its content. There is a window between the O_CREAT|O_EXCL
    # create and the json.dump that fills it where the lock file exists but
    # is still empty; a competing waiter reading it in that window must NOT
    # treat "fails to parse" as "stale" — that would delete a live lock out
    # from under its holder. A missing file (raced away by another waiter
    # who just broke/released it) is not stale either — the acquire loop
    # will simply retry.
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) > stale_after


@contextmanager
def file_lock(target: Path, timeout: float = 10.0, stale_after: float = 60.0) -> Iterator[None]:
    lock = _lock_path(target)
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    acquired = False
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as fh:
                json.dump({"pid": os.getpid(), "ts": time.time()}, fh)
            acquired = True
            break
        except FileExistsError:
            if _is_stale(lock, stale_after):
                try:
                    os.unlink(lock)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise LockTimeout(f"could not acquire lock for {target} within {timeout}s")
            time.sleep(0.02)
    try:
        yield
    finally:
        if acquired:
            try:
                os.unlink(lock)
            except OSError:
                pass
