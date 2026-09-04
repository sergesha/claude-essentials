"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects._coordinator_values import (
    CoordinatorLineageError,
    ReconcileAction,
    ReconcileReport,
    make_reconcile_report,
)
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.effects.models import (
    AcceptDescriptor,
    DecisionDescriptor,
    EffectDescriptor,
    PublishDescriptor,
    ScopeDescriptor,
)
from lockstep.runtime.leases import Lease, LeaseUnavailable
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot


class _EffectCoordinatorOrchestration:
    def _reconcile_inventory(
        self,
        run_id: str,
    ) -> tuple[
        RunBinding,
        NativeSnapshot,
        tuple[EffectRecord, ...],
        dict[Any, NativeInterrupt],
    ]:
        binding = self._binding(run_id)
        snapshot = self._runtime.snapshot(run_id, subgraphs=True)
        records = self._ledger.list_nonterminal_for_thread(
            binding.thread_id, limit=self.MAX_DUE_PER_SCAN + 1
        )
        if len(records) > self.MAX_DUE_PER_SCAN:
            raise CoordinatorLineageError(
                "run exceeds the bounded nonterminal effect capacity"
            )
        pending = {
            interrupt.coordinate: interrupt
            for interrupt in self._protected(snapshot)
        }
        return binding, snapshot, records, pending

    def _recover_missing_effect(
        self,
        *,
        run_id: str,
        records: tuple[EffectRecord, ...],
        pending: Mapping[Any, NativeInterrupt],
        coordinate: Any,
    ) -> ReconcileReport | None:
        missing_records = tuple(
            item for item in records if item.coordinate not in pending
        )
        for missing in missing_records:
            lineage = self._protected_lineage(
                run_id, missing.coordinate, missing.descriptor_digest
            )
            if lineage == "descended" and missing.phase in {
                "sealed", "indeterminate"
            }:
                if coordinate is not None and coordinate != missing.coordinate:
                    continue
                try:
                    lease = self._acquire(missing.effect_id)
                except LeaseUnavailable:
                    return make_reconcile_report(run_id, missing, ReconcileAction.BUSY)
                try:
                    delivered = self._ledger.mark_delivered(
                        missing.effect_id,
                        expected_revision=missing.revision,
                        lease=lease,
                    )
                finally:
                    self._leases.release(lease)
                return make_reconcile_report(run_id, delivered, ReconcileAction.DELIVERED)
            raise CoordinatorLineageError(
                "nonterminal effect is absent from compatible native lineage"
            )
        return None

    def _delivered_coordinate_retry(
        self,
        *,
        run_id: str,
        coordinate: Any,
        expected_descriptor_digest: str | None,
    ) -> ReconcileReport | None:
        if expected_descriptor_digest is None:
            return None
        effect_id = derive_effect_id(coordinate, expected_descriptor_digest)
        try:
            delivered = self._ledger.get(effect_id)
        except KeyError:
            return None
        if (
            delivered.phase == "delivered"
            and delivered.coordinate == coordinate
            and delivered.descriptor_digest == expected_descriptor_digest
            and self._protected_lineage(
                run_id, coordinate, expected_descriptor_digest
            )
            == "descended"
        ):
            return make_reconcile_report(run_id, delivered, ReconcileAction.DELIVERED)
        return None

    def _select_reconcile_effect(
        self,
        *,
        run_id: str,
        records: tuple[EffectRecord, ...],
        pending: Mapping[Any, NativeInterrupt],
        coordinate: Any,
        expected_descriptor_digest: str | None,
    ) -> tuple[NativeInterrupt, EffectRecord | None] | ReconcileReport | None:
        records_by_coordinate = {item.coordinate: item for item in records}
        if coordinate is not None:
            interrupt = pending.get(coordinate)
            if interrupt is None:
                delivered = self._delivered_coordinate_retry(
                    run_id=run_id,
                    coordinate=coordinate,
                    expected_descriptor_digest=expected_descriptor_digest,
                )
                if delivered is not None:
                    return delivered
                raise CoordinatorLineageError(
                    "selected effect coordinate is not exactly pending"
                )
            return interrupt, records_by_coordinate.get(coordinate)
        active = [
            item
            for item in records
            if item.phase not in {"sealed", "indeterminate"}
        ]
        if active:
            record = active[0]
            return pending[record.coordinate], record
        unrecorded = [
            interrupt
            for current_coordinate, interrupt in pending.items()
            if current_coordinate not in records_by_coordinate
        ]
        if unrecorded:
            return unrecorded[0], None
        if records:
            record = records[0]
            return pending[record.coordinate], record
        return None

    def _current_record_under_lease(
        self,
        *,
        run_id: str,
        effect_id: str,
        record: EffectRecord | None,
        lease: Lease,
    ) -> EffectRecord | ReconcileReport | None:
        if record is not None:
            current = self._ledger.get(record.effect_id)
            if (
                current.revision != record.revision
                or current.phase != record.phase
                or not self._leases.is_current(lease)
            ):
                return make_reconcile_report(run_id, current, ReconcileAction.BUSY)
            return current
        try:
            return self._ledger.get(effect_id)
        except KeyError:
            return None

    def _reconcile_special_descriptor(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        snapshot: NativeSnapshot,
        interrupt: NativeInterrupt,
        descriptor: Any,
        effect_id: str,
        record: EffectRecord | None,
        lease: Lease,
    ) -> ReconcileReport | None:
        if isinstance(descriptor, DecisionDescriptor):
            return self._reconcile_decision(
                run_id,
                binding,
                interrupt,
                descriptor,
                effect_id,
                record,
            )
        if isinstance(descriptor, AcceptDescriptor):
            return self._reconcile_acceptance(
                run_id, descriptor, interrupt, effect_id, record, lease
            )
        if isinstance(descriptor, PublishDescriptor):
            return self._reconcile_publication(
                run_id,
                binding,
                snapshot,
                descriptor,
                interrupt,
                effect_id,
                record,
                lease,
            )
        return None

    def _reconcile_owned_effect(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        snapshot: NativeSnapshot,
        interrupt: NativeInterrupt,
        effect_id: str,
        record: EffectRecord | None,
        lease: Lease,
    ) -> ReconcileReport:
        current = self._current_record_under_lease(
            run_id=run_id,
            effect_id=effect_id,
            record=record,
            lease=lease,
        )
        if isinstance(current, ReconcileReport):
            return current
        record = current
        descriptor, checked_effect_id = self._identity(
            run_id, binding, interrupt, record
        )
        if checked_effect_id != effect_id or not self._leases.is_current(lease):
            return ReconcileReport(
                run_id,
                effect_id,
                ReconcileAction.BUSY.value,
                None if record is None else record.phase,
            )
        special = self._reconcile_special_descriptor(
            run_id=run_id,
            binding=binding,
            snapshot=snapshot,
            interrupt=interrupt,
            descriptor=descriptor,
            effect_id=effect_id,
            record=record,
            lease=lease,
        )
        if special is not None:
            return special
        assert isinstance(descriptor, (EffectDescriptor, ScopeDescriptor))
        context = self._reconcile_context(
            run_id=run_id,
            binding=binding,
            snapshot=snapshot,
            interrupt=interrupt,
            descriptor=descriptor,
            effect_id=effect_id,
            record=record,
            lease=lease,
        )
        if isinstance(context, ReconcileReport):
            return context
        return self._dispatch_effect_phase(
            run_id=run_id,
            binding=binding,
            context=context,
            record=record,
            lease=lease,
        )
