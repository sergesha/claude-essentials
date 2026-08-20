"""Generic immutable SHA-256-addressed byte storage."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import tempfile

from lockstep.runtime.locking import file_lock


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


def _validate_digest(digest: str) -> None:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("expected a lowercase SHA-256 digest")


def _read_regular(path: Path) -> bytes:
    try:
        if path.is_symlink():
            raise BlobStorageError(f"blob storage path is a symlink: {path}")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise BlobStorageError(f"cannot safely open blob storage path: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BlobStorageError(f"blob storage path is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


class BlobStore:
    def __init__(self, owner_state_dir: str | Path) -> None:
        self._directory = Path(owner_state_dir) / "blobs" / "sha256"

    def path_for(self, ref: BlobRef) -> Path:
        _validate_digest(ref.sha256)
        return self._directory / ref.sha256[:2] / ref.sha256

    def put(self, data: bytes, *, expected_sha256: str | None = None) -> BlobRef:
        if not isinstance(data, bytes):
            raise TypeError("BlobStore.put accepts bytes")
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None:
            _validate_digest(expected_sha256)
            if digest != expected_sha256:
                raise DigestMismatch(
                    f"blob digest mismatch: expected {expected_sha256}, observed {digest}"
                )
        ref = BlobRef(sha256=digest, size=len(data))
        path = self.path_for(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path, timeout=30.0, stale_after=300.0):
            if path.exists() or path.is_symlink():
                existing = _read_regular(path)
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
                tmp.chmod(0o444)
                os.replace(tmp, path)
            finally:
                if tmp.exists():
                    tmp.unlink()
        return ref

    def read(self, ref: BlobRef) -> bytes:
        path = self.path_for(ref)
        try:
            data = _read_regular(path)
        except FileNotFoundError as exc:
            raise KeyError(ref.sha256) from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != ref.sha256 or len(data) != ref.size:
            raise DigestMismatch(
                f"stored blob mismatch for {ref.sha256}: observed {digest} ({len(data)} bytes)"
            )
        return data
