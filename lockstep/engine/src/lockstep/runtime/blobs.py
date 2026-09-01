"""Generic immutable SHA-256-addressed byte storage."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lockstep.runtime.locking import file_lock
from lockstep.runtime.owner_state import (
    InsecureStatePath,
    StorageLimitExceeded,
    ensure_owner_directory,
    fsync_owner_directory,
    initialize_owner_state,
    seal_owner_file,
    verify_owner_file,
)


class DigestMismatch(RuntimeError):
    """Bytes do not match the digest that names them."""


class BlobStorageError(RuntimeError):
    """The owner-state blob path is not a contained regular file."""


@dataclass(frozen=True, order=True)
class BlobRef:
    sha256: str
    size: int

    @property
    def digest(self) -> str:
        return self.sha256


@dataclass(frozen=True)
class BlobLimits:
    max_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")


def _validate_digest(digest: str) -> None:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("expected a lowercase SHA-256 digest")


def _read_regular(path: Path, *, max_bytes: int) -> bytes:
    try:
        if path.is_symlink():
            raise BlobStorageError(f"blob storage path is a symlink: {path}")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise BlobStorageError(f"cannot safely open blob storage path: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BlobStorageError(f"blob storage path is not a regular file: {path}")
        if info.st_size > max_bytes:
            raise StorageLimitExceeded(
                f"stored blob exceeds {max_bytes} byte admission limit"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


class BlobStore:
    def __init__(
        self,
        owner_state_dir: str | Path,
        *,
        limits: BlobLimits | None = None,
    ) -> None:
        self._owner_state = initialize_owner_state(owner_state_dir)
        self._directory = ensure_owner_directory(self._owner_state, "blobs/sha256")
        self._limits = limits or BlobLimits()

    def path_for(self, ref: BlobRef) -> Path:
        _validate_digest(ref.sha256)
        return self._directory / ref.sha256[:2] / ref.sha256

    def put(self, data: bytes, *, expected_sha256: str | None = None) -> BlobRef:
        if not isinstance(data, bytes):
            raise TypeError("BlobStore.put accepts bytes")
        if len(data) > self._limits.max_bytes:
            raise StorageLimitExceeded(
                f"blob exceeds {self._limits.max_bytes} byte admission limit"
            )
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None:
            _validate_digest(expected_sha256)
            if digest != expected_sha256:
                raise DigestMismatch(
                    f"blob digest mismatch: expected {expected_sha256}, observed {digest}"
                )
        ref = BlobRef(sha256=digest, size=len(data))
        path = self.path_for(ref)
        ensure_owner_directory(
            self._owner_state, path.parent.relative_to(self._owner_state)
        )
        with file_lock(path, timeout=30.0, stale_after=300.0):
            if path.exists() or path.is_symlink():
                try:
                    verify_owner_file(path)
                except InsecureStatePath as exc:
                    raise BlobStorageError(f"unsafe blob storage path: {path}") from exc
                existing = _read_regular(path, max_bytes=self._limits.max_bytes)
                if existing != data or hashlib.sha256(existing).hexdigest() != digest:
                    raise DigestMismatch(f"stored blob {digest} does not match its address")
                return ref
            fd, raw_tmp = tempfile.mkstemp(prefix=f".{digest}.", dir=path.parent)
            tmp = Path(raw_tmp)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                seal_owner_file(tmp, writable=False)
                os.replace(tmp, path)
                fsync_owner_directory(path.parent)
            finally:
                if tmp.exists():
                    tmp.unlink()
        return ref

    def read(self, ref: BlobRef) -> bytes:
        if ref.size < 0 or ref.size > self._limits.max_bytes:
            raise StorageLimitExceeded(
                f"blob reference exceeds {self._limits.max_bytes} byte admission limit"
            )
        path = self.path_for(ref)
        try:
            try:
                verify_owner_file(path)
            except InsecureStatePath as exc:
                raise BlobStorageError(f"unsafe blob storage path: {path}") from exc
            data = _read_regular(path, max_bytes=self._limits.max_bytes)
        except FileNotFoundError as exc:
            raise KeyError(ref.sha256) from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != ref.sha256 or len(data) != ref.size:
            raise DigestMismatch(
                f"stored blob mismatch for {ref.sha256}: observed {digest} ({len(data)} bytes)"
            )
        return data
