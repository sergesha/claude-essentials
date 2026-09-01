"""Terminal delivery settlement for run-drive recovery."""

from __future__ import annotations

from lockstep.runtime._recovery_backfill import _bound_runtime
from lockstep.runtime.catalog import RunBinding


class _RecoveryWatchSettlement:
    def _settle_terminal_watch(self, run_id: str) -> None:
        reports = self._coordinator.reconcile_consumed(run_id)
        if any(report.action != "delivered" for report in reports):
            return
        self._effects.acknowledge_run_drive_watch(run_id)

    def _settle_after_accepted_drive(self, binding: RunBinding) -> None:
        with _bound_runtime(self._runtime, binding) as available:
            if not available:
                return
            snapshot = self._runtime.snapshot(
                binding.public_run_id, subgraphs=True
            )
            if not snapshot.pending and not snapshot.next:
                self._settle_terminal_watch(binding.public_run_id)

    def _drive_delegated_watch(self, binding: RunBinding) -> bool:
        accepted = self._drive_recovered_run(binding.public_run_id)
        if accepted:
            self._settle_after_accepted_drive(binding)
        return accepted
