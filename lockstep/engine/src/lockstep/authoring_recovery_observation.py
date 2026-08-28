"""Shared bounded file observations for authoring recovery policies."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from lockstep.authoring_recovery_model import (
    RecoveryBeforeImage,
    RecoveryWriteEntry,
)
from lockstep.errors import AuthoringError
from lockstep.recipe.authority import RecipeLimits


_MAX_FILE_BYTES = RecipeLimits().max_file_bytes


@dataclass(frozen=True, slots=True)
class ObservedRecoveryFile:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str
    content: bytes


def observe_recovery_file(
    parent_descriptor: int, leaf: str, path: Path
) -> ObservedRecoveryFile | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(leaf, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AuthoringError(
            f"authoring recovery path is unavailable: {path}"
        ) from exc
    try:
        first = os.fstat(descriptor)
        if not stat.S_ISREG(first.st_mode):
            raise AuthoringError(
                f"authoring recovery path is not regular: {path}"
            )
        if first.st_size > _MAX_FILE_BYTES:
            raise AuthoringError(
                f"authoring recovery path exceeds its byte limit: {path}"
            )
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
    if file_facts(first) != file_facts(last):
        raise AuthoringError(
            f"authoring recovery path changed while reading: {path}"
        )
    content = b"".join(chunks)
    if len(content) != first.st_size:
        raise AuthoringError(f"authoring recovery path changed size: {path}")
    return ObservedRecoveryFile(
        first.st_dev,
        first.st_ino,
        first.st_mode,
        first.st_size,
        first.st_mtime_ns,
        first.st_ctime_ns,
        hashlib.sha256(content).hexdigest(),
        content,
    )


def matches_captured_before(
    observed: ObservedRecoveryFile | None, before: RecoveryBeforeImage
) -> bool:
    if before.absent:
        return observed is None
    leaf = before.leaf
    return bool(
        observed is not None
        and leaf is not None
        and before.content is not None
        and (
            observed.device,
            observed.inode,
            observed.mode,
            observed.size,
            observed.mtime_ns,
            observed.ctime_ns,
        )
        == (
            leaf.device,
            leaf.inode,
            leaf.mode,
            leaf.size,
            leaf.mtime_ns,
            leaf.ctime_ns,
        )
        and observed.content == before.content
        and observed.sha256 == before.sha256
    )


def matches_planned_after(
    observed: ObservedRecoveryFile | None, entry: RecoveryWriteEntry
) -> bool:
    return observed is not None and matches_after_file(observed, entry)


def matches_after_file(
    observed: ObservedRecoveryFile, entry: RecoveryWriteEntry
) -> bool:
    after = entry.after
    return (
        observed.size == after.size
        and observed.sha256 == after.sha256
        and stat.S_IMODE(observed.mode) == after.mode
    )


def matches_desired_before(
    observed: ObservedRecoveryFile | None, before: RecoveryBeforeImage
) -> bool:
    if before.absent:
        return observed is None
    return bool(
        observed is not None
        and before.content is not None
        and observed.content == before.content
        and observed.sha256 == before.sha256
        and stat.S_IMODE(observed.mode) == before.mode
    )


def require_same_recovery_file(
    parent_descriptor: int,
    leaf: str,
    path: Path,
    expected: ObservedRecoveryFile,
) -> ObservedRecoveryFile:
    observed = observe_recovery_file(parent_descriptor, leaf, path)
    if observed is None or observed != expected:
        raise AuthoringError(
            f"authoring recovery path changed before mutation: {path}"
        )
    return observed


def same_recovery_file_identity(
    left: ObservedRecoveryFile, right: ObservedRecoveryFile
) -> bool:
    return (left.device, left.inode) == (right.device, right.inode)


def fsync_recovery_regular(parent_descriptor: int, leaf: str) -> None:
    descriptor = os.open(
        leaf,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AuthoringError("authoring recovery destination is not regular")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def file_facts(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
