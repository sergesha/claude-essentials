"""Private command-side owner of future run-drive recovery policy."""

from __future__ import annotations

from lockstep.runtime.effects.ledger import RunDriveWatch


class RecoveryDriver:
    """Inert R2a boundary for one future automatic run-drive attempt."""

    def _drive_run_watch(self, watch: RunDriveWatch) -> bool:
        return False
