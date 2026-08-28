"""Shared semantic namespace and mutation probes for authoring crash tests."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest


@dataclass(frozen=True, slots=True)
class NamespaceEntry:
    kind: str
    mode: int
    device: int
    inode: int
    content: bytes | None = None
    symlink_target: str | None = None


def namespace_entry(path: Path) -> NamespaceEntry:
    """Capture stable namespace semantics without volatile directory facts."""

    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISREG(info.st_mode):
        return NamespaceEntry(
            "regular", mode, info.st_dev, info.st_ino, content=path.read_bytes()
        )
    if stat.S_ISDIR(info.st_mode):
        return NamespaceEntry("directory", mode, info.st_dev, info.st_ino)
    if stat.S_ISLNK(info.st_mode):
        return NamespaceEntry(
            "symlink",
            mode,
            info.st_dev,
            info.st_ino,
            symlink_target=os.readlink(path),
        )
    return NamespaceEntry("non-regular", mode, info.st_dev, info.st_ino)


def namespace_image(root: Path) -> dict[str, NamespaceEntry]:
    return {
        ".": namespace_entry(root),
        **{
            path.relative_to(root).as_posix(): namespace_entry(path)
            for path in sorted(root.rglob("*"))
        },
    }


def directory_identity(path: Path) -> tuple[int, int, int]:
    observed = namespace_entry(path)
    assert observed.kind == "directory"
    return observed.device, observed.inode, observed.mode


def opaque_lock_identities(
    owner_before: dict[str, NamespaceEntry],
    owner_after: dict[str, NamespaceEntry],
) -> frozenset[tuple[int, int]]:
    return frozenset(
        (entry.device, entry.inode)
        for path, entry in owner_after.items()
        if path not in owner_before
        and entry.kind == "regular"
        and entry.mode == 0o600
        and entry.content == b""
    )


def _open_can_mutate(flags: int) -> bool:
    access_mode = flags & getattr(os, "O_ACCMODE", os.O_WRONLY | os.O_RDWR)
    mutation_flags = os.O_CREAT | os.O_EXCL | os.O_TRUNC
    mutation_flags |= getattr(os, "O_TMPFILE", 0)
    return access_mode != os.O_RDONLY or bool(flags & mutation_flags)


def _opened_path_identity(
    path: object, directory_fd: int | None
) -> tuple[int, int] | None:
    try:
        info = os.stat(path, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return None
    return info.st_dev, info.st_ino


_MUTATION_SYSCALLS = (
    "chmod",
    "chown",
    "fchown",
    "fchmod",
    "ftruncate",
    "link",
    "lchown",
    "mkfifo",
    "mkdir",
    "mknod",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "symlink",
    "truncate",
    "unlink",
    "utime",
    "write",
    "writev",
    "chflags",
    "fchflags",
    "pwrite",
    "pwritev",
    "removexattr",
    "setxattr",
)


def _instrument_named_mutation(
    monkeypatch: pytest.MonkeyPatch, calls: list[str], name: str
) -> None:
    original: Callable[..., object] = getattr(os, name)

    def record(*args, **kwargs):
        calls.append(name)
        return original(*args, **kwargs)

    monkeypatch.setattr(os, name, record)


def _write_open_is_allowed(
    path: object,
    flags: int,
    directory_fd: int | None,
    allowed_identities: frozenset[tuple[int, int]],
) -> bool:
    destructive_flags = os.O_EXCL | os.O_TRUNC | getattr(os, "O_TMPFILE", 0)
    return (
        not flags & destructive_flags
        and _opened_path_identity(path, directory_fd) in allowed_identities
    )


def install_mutation_syscall_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    allowed_write_open_identities: frozenset[tuple[int, int]] = frozenset(),
    record_fsync: bool = False,
) -> list[str]:
    """Record namespace/write mutations while allowing a known lock reopen."""

    calls: list[str] = []
    for name in _MUTATION_SYSCALLS:
        if hasattr(os, name):
            _instrument_named_mutation(monkeypatch, calls, name)
    original_open = os.open

    def record_open(path, flags, mode=0o777, *, dir_fd=None):
        if _open_can_mutate(flags) and not _write_open_is_allowed(
            path, flags, dir_fd, allowed_write_open_identities
        ):
            calls.append("open")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", record_open)
    if record_fsync:
        original_fsync = os.fsync

        def record_fsync_call(descriptor: int) -> None:
            calls.append("fsync")
            original_fsync(descriptor)

        monkeypatch.setattr(os, "fsync", record_fsync_call)
    return calls
