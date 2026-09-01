"""Crash-released cross-process serialization for native graph commits."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from lockstep.runtime.advisory_lock import (
    AdvisoryLockTimeout,
    advisory_file_lock,
)
from lockstep.runtime.owner_state import ensure_owner_directory, verify_owner_directory

InvocationLockTimeout = AdvisoryLockTimeout


class InvocationLockStore:
    """POSIX advisory locks whose ownership the kernel releases on process exit."""

    def __init__(self, owner_state: Path, *, timeout: float = 60.0) -> None:
        if os.name != "posix":
            raise RuntimeError("native invocation serialization requires POSIX advisory locks")
        self._directory = ensure_owner_directory(owner_state, "invoke-locks")
        self._timeout = timeout

    def _path(self, thread_id: str) -> Path:
        digest = hashlib.sha256(thread_id.encode()).hexdigest()
        return self._directory / f"{digest}.lock"

    @contextmanager
    def hold(self, thread_id: str) -> Iterator[None]:
        verify_owner_directory(self._directory)
        path = self._path(thread_id)
        with advisory_file_lock(path, timeout=self._timeout):
            yield
