"""Filesystem boundary for the owner runtime snapshot leaf."""

from __future__ import annotations

import os
from pathlib import Path

from lockstep.runtime.bounded_files import read_bounded_regular_file


MAX_OWNER_RUNTIME_SNAPSHOT_BYTES = 2 * 1024 * 1024


def runtime_snapshot_path(state_dir: Path) -> Path:
    return Path(state_dir) / "runtime-owner" / "snapshot.json"


def preflight_runtime_snapshot_file(state_dir: Path) -> None:
    """Reject a poisoned leaf before expensive provisioning preparation.

    The transition store repeats this descriptor-based read under its lock.
    This preliminary check is an early rejection only, never authority.
    """

    read_bounded_regular_file(
        runtime_snapshot_path(state_dir),
        max_bytes=MAX_OWNER_RUNTIME_SNAPSHOT_BYTES,
        label="owner runtime snapshot",
        missing_ok=True,
        required_uid=os.getuid(),
        required_mode=0o600,
    )
