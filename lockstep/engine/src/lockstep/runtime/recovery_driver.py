"""Private command-side owner of future run-drive recovery policy."""

from __future__ import annotations

from lockstep.runtime.effects.ledger import RunDriveWatch


class RecoveryDriver:
    """Inert command-owned boundary for future run-drive recovery policy."""

    def _sweep_run_drive_watches(
        self,
        *,
        project_identity: str | None,
        limit: int,
    ) -> tuple[str, ...]:
        return ()

    def _drive_run_watch(self, watch: RunDriveWatch) -> bool:
        return False
