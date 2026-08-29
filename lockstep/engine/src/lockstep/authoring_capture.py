"""Bounded filesystem observation and revalidation for authoring."""

from __future__ import annotations

import hashlib, os, stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from lockstep.authoring_bundle import DirectoryIdentity, FileIdentity, PlannedTarget, SourceSnapshot
from lockstep.errors import AuthoringError
from lockstep.recipe.authority import RecipeLimits
from lockstep.runtime.owner_state import StorageLimitExceeded

class _DescriptorObservationError(Exception): pass
class _DescriptorNotRegular(_DescriptorObservationError): pass
class _DescriptorSizeInvalid(_DescriptorObservationError): pass
class _DescriptorChanged(_DescriptorObservationError): pass

@dataclass(frozen=True, slots=True)
class _RegularFileObservation:
    content: bytes; info: os.stat_result

def _facts(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
def _identity(info: os.stat_result) -> FileIdentity:
    return FileIdentity(*_facts(info))
class _AuthoringBudget:
    __slots__ = ("label", "limits", "count", "size")
    def __init__(self, label: str) -> None: self.label, self.limits, self.count, self.size = label, RecipeLimits(), 0, 0
    @property
    def max_bytes_for_next(self) -> int:
        if self.count >= self.limits.max_files: raise StorageLimitExceeded(f"{self.label} exceeds {self.limits.max_files} admission limit")
        return min(self.limits.max_file_bytes, self.limits.max_source_bytes - self.size)
    def retain(self, content: bytes | None) -> None:
        available, size = self.max_bytes_for_next, len(content or b"")
        if content is not None and not isinstance(content, bytes): raise TypeError("authoring budget contents must be bytes or absence")
        if size > available: raise StorageLimitExceeded(f"{self.label} {'contains a file exceeding' if size > self.limits.max_file_bytes else 'exceeds the aggregate byte'} admission limit")
        self.count += 1; self.size += size
def _validate_authoring_contents(label: str, contents: Iterable[bytes | None]) -> None:
    budget = _AuthoringBudget(label)
    for content in contents: budget.retain(content)
def _observe_regular_descriptor(descriptor: int, *, max_bytes: int,
                                expected_size: int | None = None) -> _RegularFileObservation:
    if max_bytes < 0:
        os.close(descriptor)
        raise ValueError("descriptor observation byte ceiling must be non-negative")
    try:
        first = os.fstat(descriptor)
        if not stat.S_ISREG(first.st_mode): raise _DescriptorNotRegular
        if first.st_size > max_bytes: raise _DescriptorSizeInvalid
        if expected_size is not None and first.st_size != expected_size: raise _DescriptorSizeInvalid
        chunks, remaining = [], first.st_size + 1
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
    if _facts(first) != _facts(last) or len(content) != first.st_size: raise _DescriptorChanged
    return _RegularFileObservation(content, first)
_READ_FLAGS = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
               | getattr(os, "O_NONBLOCK", 0))
def capture_regular_file(path: Path, *, max_bytes: int, label: str,
                         expected: os.stat_result | None = None) -> tuple[bytes, FileIdentity]:
    try:
        first = path.lstat() if expected is None else expected
    except OSError as exc:
        raise AuthoringError(f"{label} cannot be captured") from exc
    if not stat.S_ISREG(first.st_mode): raise AuthoringError(f"{label} must be a regular file")
    if first.st_size > max_bytes: raise StorageLimitExceeded(f"{label} exceeds the file admission limit")
    try:
        descriptor = os.open(path, _READ_FLAGS)
    except OSError as exc:
        raise AuthoringError(f"{label} changed while it was captured") from exc
    try:
        observed = _observe_regular_descriptor(descriptor, max_bytes=max_bytes, expected_size=first.st_size)
    except _DescriptorObservationError as exc:
        raise AuthoringError(f"{label} changed while it was captured") from exc
    try:
        named = path.lstat()
    except OSError as exc:
        raise AuthoringError(f"{label} changed while it was captured") from exc
    if _facts(observed.info) != _facts(first) or _facts(named) != _facts(first):
        raise AuthoringError(f"{label} changed while it was captured")
    return observed.content, _identity(first)
def capture_optional_regular_file(path: Path, *, max_bytes: int,
                                  label: str) -> tuple[bytes, FileIdentity] | None:
    try:
        expected = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AuthoringError(f"{label} cannot be captured") from exc
    return capture_regular_file(path, max_bytes=max_bytes, label=label, expected=expected)
def capture_directory(path: Path, *, label: str) -> DirectoryIdentity:
    try:
        first = path.lstat()
    except FileNotFoundError: raise
    except OSError as exc:
        raise AuthoringError(f"{label} cannot be captured") from exc
    if not stat.S_ISDIR(first.st_mode): raise AuthoringError(f"{label} must be a canonical real directory")
    try:
        resolved = path.resolve(strict=True)
        last = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise AuthoringError(f"{label} cannot be captured") from exc
    first_facts = first.st_dev, first.st_ino, first.st_mode, first.st_ctime_ns; last_facts = last.st_dev, last.st_ino, last.st_mode, last.st_ctime_ns
    if first_facts != last_facts: raise AuthoringError(f"{label} changed while it was captured")
    if resolved != path: raise AuthoringError(f"{label} must be a canonical real directory")
    return DirectoryIdentity(path, first.st_dev, first.st_ino)
def validate_directory(identity: DirectoryIdentity, *, label: str) -> None:
    try:
        info, resolved = identity.path.lstat(), identity.path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AuthoringError(f"{label} changed while it was captured") from exc
    if (not stat.S_ISDIR(info.st_mode) or resolved != identity.path
            or (info.st_dev, info.st_ino) != (identity.device, identity.inode)):
        raise AuthoringError(f"{label} changed while it was captured")
def _read_exact(descriptor: int, path: Path, expected: FileIdentity) -> bytes:
    try:
        observed = _observe_regular_descriptor(
            descriptor, max_bytes=expected.size, expected_size=expected.size)
    except _DescriptorNotRegular as exc:
        raise AuthoringError(f"authoring path is not a regular file: {path}") from exc
    except _DescriptorSizeInvalid as exc:
        raise AuthoringError(f"authoring file size changed before reading: {path}") from exc
    except _DescriptorChanged as exc:
        raise AuthoringError(f"authoring file changed while reading: {path}") from exc
    if _identity(observed.info) != expected: raise AuthoringError("authoring file identity changed after planning")
    return observed.content
def validate_sources(sources: tuple[SourceSnapshot, ...]) -> None:
    for source in sources:
        for parent in source.parents:
            validate_directory(parent, label="workflow source parent")
        try:
            descriptor = os.open(source.path, _READ_FLAGS)
        except OSError as exc:
            raise AuthoringError(f"authoring file is unavailable: {source.path}") from exc
        content = _read_exact(descriptor, source.path, source.file)
        if content != source.content or hashlib.sha256(content).hexdigest() != source.sha256:
            raise AuthoringError("workflow source bytes changed after planning")
def _validate_target_before(target: PlannedTarget, parent: int | None = None) -> None:
    path = target.path if parent is None else target.path.name
    if target.before is None:
        try:
            os.stat(path, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise AuthoringError("authoring destination was created after planning")
    try:
        descriptor = os.open(path, _READ_FLAGS, dir_fd=parent)
    except OSError as exc:
        raise AuthoringError(f"authoring file is unavailable: {target.path}") from exc
    content = _read_exact(descriptor, target.path, target.before_file)
    if content != target.before or hashlib.sha256(content).hexdigest() != target.before_sha256:
        raise AuthoringError("authoring destination bytes changed after planning")
def validate_target(target: PlannedTarget) -> None:
    for parent in target.parents:
        validate_directory(parent, label="destination ancestor")
    current = target.parents[-1].path
    for part in target.path.parent.relative_to(current).parts:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            continue
        raise AuthoringError("destination ancestor was created after planning")
    _validate_target_before(target)
def validate_target_at(parent_descriptor: int, target: PlannedTarget) -> None:
    _validate_target_before(target, parent_descriptor)
