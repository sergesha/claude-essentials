"""Owner-only filesystem boundary for Lockstep's local trusted state.

The local MVP trusts the OS account and does not claim isolation from another
process running as that same user.  These checks prevent accidental exposure to
other OS identities and reject workspace-controlled links at the state boundary.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from itertools import islice
from pathlib import Path, PurePath
from typing import TypeVar

_T = TypeVar("_T")


class InsecureStatePath(RuntimeError):
    """A trusted-state path is linked, foreign-owned, or accessible to others."""


class StorageLimitExceeded(ValueError):
    """Input exceeds a configured trusted-store admission ceiling."""


def sqlite_readonly_uri(path: Path) -> str:
    """Return an escaped URI for one absolute SQLite read-only path."""

    return Path(path).absolute().as_uri() + "?mode=ro"


def take_bounded(values: Iterable[_T], max_items: int, label: str) -> tuple[_T, ...]:
    """Consume at most one item beyond a configured cardinality ceiling."""

    if max_items < 0:
        raise ValueError("item limit cannot be negative")
    items = tuple(islice(iter(values), max_items + 1))
    if len(items) > max_items:
        raise StorageLimitExceeded(f"{label} exceed {max_items} admission limit")
    return items


def _verify_owner(stat_result: os.stat_result, path: Path) -> None:
    if os.name != "posix":
        raise InsecureStatePath("owner-only state is supported only on POSIX")
    if stat_result.st_uid != os.getuid():
        raise InsecureStatePath(f"state path is not owned by the current user: {path}")
    if stat_result.st_mode & 0o077:
        raise InsecureStatePath(f"state path is not owner-only: {path}")


def verify_owner_directory(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InsecureStatePath(f"state directory is not a real directory: {path}")
    _verify_owner(info, path)


def initialize_owner_state(path: str | Path) -> Path:
    """Create an owner-state root as 0700, or verify an existing one."""

    root = Path(path)
    if root.exists() or root.is_symlink():
        verify_owner_directory(root)
        return root
    try:
        root.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        pass
    verify_owner_directory(root)
    return root


def fsync_owner_directory(path: Path) -> None:
    """Make a completed owner-state namespace mutation directory-durable."""

    verify_owner_directory(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_owner_directory(root: Path, relative: str | PurePath) -> Path:
    """Create and verify each descendant below an already verified state root."""

    initialize_owner_state(root)
    parts = PurePath(relative).parts
    if not parts or PurePath(relative).is_absolute() or any(part in ("", "..") for part in parts):
        raise InsecureStatePath(f"invalid owner-state relative directory: {relative}")
    current = root
    for part in parts:
        current = current / part
        if current.exists() or current.is_symlink():
            verify_owner_directory(current)
        else:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                # A concurrent trusted-store writer won creation. Verification
                # below rejects links, foreign ownership, or insecure modes.
                pass
            verify_owner_directory(current)
            # The directory itself can be synced by later leaf publication,
            # but its name is durable only after syncing the verified parent.
            fsync_owner_directory(current.parent)
    return current


def verify_owner_file(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise InsecureStatePath(f"state file is not a regular file: {path}")
    _verify_owner(info, path)


def seal_owner_file(path: Path, *, writable: bool) -> None:
    """Set the mode of a newly created trusted-state file and verify it."""

    path.chmod(0o600 if writable else 0o400)
    verify_owner_file(path)
