"""One-watch drive orchestration for run-drive recovery."""

from __future__ import annotations

from lockstep.runtime._recovery_backfill import _bound_runtime
from lockstep.runtime.effects._coordinator_values import ReconcileAction
from lockstep.runtime.effects.ledger import RunDriveWatch
from lockstep.runtime.effects.models import (
    AcceptDescriptor,
    DecisionDescriptor,
    EffectDescriptor,
    PublishDescriptor,
    ScopeDescriptor,
)


class _RecoveryWatchDrive:
    def _drive_run_watch(self, watch: RunDriveWatch) -> bool:
        binding = self._catalog.get(watch.public_run_id)
        with _bound_runtime(self._runtime, binding) as available:
            if not available:
                return False
            snapshot = self._snapshot_for_run_drive_watch(binding, watch)
            if snapshot is None:
                return False
            if not snapshot.pending and not snapshot.next:
                self._settle_terminal_watch(watch.public_run_id)
                return False
            if not self._has_multiple_engine_owned_effects(snapshot):
                pending = self._pending_run_drive_descriptor(snapshot)
                if pending is None:
                    return False
                interrupt, descriptor = pending
                if isinstance(descriptor, DecisionDescriptor):
                    report = self._coordinator.reconcile_one(
                        watch.public_run_id,
                        interrupt.coordinate,
                        expected_descriptor_digest=descriptor.digest,
                    )
                    accepted = (
                        ReconcileAction(report.action) is ReconcileAction.DELIVERED
                    )
                    if accepted:
                        self._settle_after_accepted_drive(binding)
                    return accepted
                if not isinstance(
                    descriptor,
                    (
                        EffectDescriptor,
                        ScopeDescriptor,
                        AcceptDescriptor,
                        PublishDescriptor,
                    ),
                ):
                    return False
        return self._drive_delegated_watch(binding)
