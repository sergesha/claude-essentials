"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from lockstep.runtime.effects._coordinator_values import (
    CoordinatorLineageError,
    ReconcileReport,
)
from lockstep.runtime.effects.descriptors import (
    parse_effect_descriptor,
)
from lockstep.runtime.effects.ledger import (
    StaleEffectLease,
    StaleEffectRevision,
)
from lockstep.runtime.leases import LeaseUnavailable


class _EffectCoordinatorReconciliation:
    def reconcile(
        self,
        run_id: str,
        *,
        coordinate=None,
        expected_descriptor_digest: str | None = None,
    ) -> ReconcileReport:
        binding, snapshot, records, pending = self._reconcile_inventory(run_id)
        missing = self._recover_missing_effect(
            run_id=run_id,
            records=records,
            pending=pending,
            coordinate=coordinate,
        )
        if missing is not None:
            return missing
        selected = self._select_reconcile_effect(
            run_id=run_id,
            records=records,
            pending=pending,
            coordinate=coordinate,
            expected_descriptor_digest=expected_descriptor_digest,
        )
        if selected is None:
            return ReconcileReport(run_id, None, "no_effect", None)
        if isinstance(selected, ReconcileReport):
            return selected
        interrupt, record = selected

        _descriptor, effect_id = self._identity(
            run_id, binding, interrupt, record
        )
        try:
            lease = self._acquire(effect_id)
        except LeaseUnavailable:
            return ReconcileReport(
                run_id,
                effect_id,
                "busy",
                None if record is None else record.phase,
            )
        try:
            return self._reconcile_owned_effect(
                run_id=run_id,
                binding=binding,
                snapshot=snapshot,
                interrupt=interrupt,
                effect_id=effect_id,
                record=record,
                lease=lease,
            )
        except (StaleEffectLease, StaleEffectRevision):
            current = self._ledger.get(effect_id)
            return self._report(run_id, current, "busy")
        finally:
            self._leases.release(lease)

    def reconcile_one(
        self,
        run_id: str,
        coordinate,
        *,
        expected_descriptor_digest: str | None = None,
    ) -> ReconcileReport:
        """Advance one exact current native interrupt by one monotonic decision."""

        return self.reconcile(
            run_id,
            coordinate=coordinate,
            expected_descriptor_digest=expected_descriptor_digest,
        )

    def reconcile_pending(self, run_id: str) -> tuple[ReconcileReport, ...]:
        """Sweep the current native task set once without owning branch progress."""

        binding = self._binding(run_id)
        snapshot = self._runtime.snapshot(run_id, subgraphs=True)
        protected = self._protected(snapshot)
        if len(protected) > self.MAX_DUE_PER_SCAN:
            raise CoordinatorLineageError(
                "run exceeds the bounded pending effect capacity"
            )
        if any(
            interrupt.coordinate.thread_id != binding.thread_id
            for interrupt in protected
        ):
            raise CoordinatorLineageError(
                "pending sweep contains a foreign native thread"
            )
        return tuple(
            self.reconcile_one(
                run_id,
                interrupt.coordinate,
                expected_descriptor_digest=parse_effect_descriptor(
                    self._raw_descriptor(interrupt)
                ).digest,
            )
            for interrupt in protected
        )

    def reconcile_consumed(self, run_id: str) -> tuple[ReconcileReport, ...]:
        """Drain exact post-commit effect facts absent from native pending state."""

        binding = self._binding(run_id)
        snapshot = self._runtime.snapshot(run_id, subgraphs=True)
        if self._protected(snapshot):
            raise CoordinatorLineageError(
                "consumed-effect recovery requires no protected pending tasks"
            )
        records = self._ledger.list_nonterminal_for_thread(
            binding.thread_id, limit=self.MAX_DUE_PER_SCAN + 1
        )
        if len(records) > self.MAX_DUE_PER_SCAN:
            raise CoordinatorLineageError(
                "run exceeds the bounded nonterminal effect capacity"
            )
        return tuple(
            self.reconcile_one(
                run_id,
                record.coordinate,
                expected_descriptor_digest=record.descriptor_digest,
            )
            for record in records
        )

    def reconcile_due(self, now: datetime) -> tuple[ReconcileReport, ...]:
        reports = []
        for record in self._ledger.list_due(now, limit=self.MAX_DUE_PER_SCAN):
            binding = self._catalog.find_by_thread(record.coordinate.thread_id)
            reports.append(
                self.reconcile_one(
                    binding.public_run_id,
                    record.coordinate,
                    expected_descriptor_digest=record.descriptor_digest,
                )
            )
        return tuple(reports)

    def next_wakeup_delay(self, now: datetime) -> float:
        deadline = self._ledger.next_deadline()
        if deadline is None:
            return 1.0
        current = now.astimezone(UTC)
        remaining = (deadline - current).total_seconds()
        return 1.0 if remaining <= 0 else min(1.0, remaining)

    def wait_and_reconcile_due(
        self, wakeup: Callable[[float], object]
    ) -> tuple[ReconcileReport, ...]:
        """Run one externally owned, deterministic deadline-wakeup cycle."""

        wakeup(self.next_wakeup_delay(self._now()))
        return self.reconcile_due(self._now())
