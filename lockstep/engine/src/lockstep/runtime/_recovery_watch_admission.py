"""Per-watch admission and integrity isolation for run-drive recovery."""

from __future__ import annotations

import logging

from lockstep.runtime._recovery_watch_errors import _RUN_DRIVE_INTEGRITY_ERRORS
from lockstep.runtime.effects.ledger import RunDriveWatch

_LOG = logging.getLogger("lockstep.runtime.recovery_driver")


class _RecoveryWatchAdmission:
    def _accepted_from(
        self,
        watches: tuple[RunDriveWatch, ...],
        *,
        project_identity: str | None,
        limit: int,
    ) -> tuple[str, ...]:
        accepted = []
        for watch in watches:
            if self._try_drive_run_watch(watch, project_identity):
                accepted.append(watch.public_run_id)
                if len(accepted) == limit:
                    break
        return tuple(accepted)

    def _try_drive_run_watch(
        self, watch: RunDriveWatch, project_identity: str | None
    ) -> bool:
        try:
            return (
                not self._exclude_run_drive(watch.public_run_id)
                and self._matches_project(watch, project_identity)
                and self._drive_run_watch(watch)
            )
        except _RUN_DRIVE_INTEGRITY_ERRORS as exc:
            _LOG.warning(
                "run-drive recovery skipped %s after %s",
                watch.public_run_id,
                type(exc).__name__,
            )
            return False

    def _matches_project(
        self, watch: RunDriveWatch, project_identity: str | None
    ) -> bool:
        if project_identity is None:
            return True
        binding = self._catalog.get(watch.public_run_id)
        return binding.project_identity == project_identity
