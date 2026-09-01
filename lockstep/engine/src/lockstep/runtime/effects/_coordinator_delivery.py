"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from lockstep.runtime.effects._coordinator_values import (
    CoordinatorLineageError,
)
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_descriptor,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.leases import Lease, LeaseUnavailable
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot
from lockstep.runtime.status import ScenarioStatus, project_status


class _EffectCoordinatorDelivery:
    def _requested_delivery_ids(
        self,
        snapshot: NativeSnapshot,
        interrupt_ids: Sequence[str] | None,
    ) -> set[str] | None:
        if interrupt_ids is None:
            return None
        if (
            not interrupt_ids
            or len(interrupt_ids) > self.MAX_DUE_PER_SCAN
            or any(not isinstance(item, str) or not item for item in interrupt_ids)
            or len(set(interrupt_ids)) != len(interrupt_ids)
        ):
            raise CoordinatorLineageError(
                "requested interrupt selectors must be a bounded unique list"
            )
        requested = set(interrupt_ids)
        pending_protected_ids = {
            interrupt.coordinate.interrupt_id
            for interrupt in self._protected(snapshot)
        }
        unknown = requested - pending_protected_ids
        if unknown:
            raise CoordinatorLineageError(
                f"requested interrupt is not an exact pending effect: "
                f"{sorted(unknown)}"
            )
        return requested

    def _deliverable_records(
        self,
        snapshot: NativeSnapshot,
        requested: set[str] | None,
    ) -> list[tuple[EffectRecord, NativeInterrupt]]:
        deliverable = []
        for interrupt in self._protected(snapshot):
            if (
                requested is not None
                and interrupt.coordinate.interrupt_id not in requested
            ):
                continue
            descriptor = parse_effect_descriptor(self._raw_descriptor(interrupt))
            effect_id = derive_effect_id(
                interrupt.coordinate, descriptor.digest
            )
            try:
                record = self._ledger.get(effect_id)
            except KeyError:
                continue
            if (
                record.phase in {"sealed", "indeterminate"}
                and record.coordinate == interrupt.coordinate
                and record.descriptor_digest == descriptor.digest
                and record.result is not None
            ):
                deliverable.append((record, interrupt))
        return deliverable

    def _lock_deliverable_records(
        self,
        deliverable: list[tuple[EffectRecord, NativeInterrupt]],
        held: dict[str, Lease],
    ) -> list[tuple[EffectRecord, NativeInterrupt]] | None:
        current_deliverable = []
        for stale_record, interrupt in sorted(
            deliverable, key=lambda item: item[0].effect_id
        ):
            try:
                held[stale_record.effect_id] = self._acquire(
                    stale_record.effect_id
                )
            except LeaseUnavailable:
                return None
            current = self._ledger.get(stale_record.effect_id)
            if (
                current.revision != stale_record.revision
                or current.phase not in {"sealed", "indeterminate"}
                or current.coordinate != interrupt.coordinate
                or current.descriptor_digest != stale_record.descriptor_digest
                or current.result is None
                or not self._leases.is_current(held[current.effect_id])
            ):
                return None
            current_deliverable.append((current, interrupt))
        return current_deliverable

    def _commit_deliverable_records(
        self,
        *,
        run_id: str,
        snapshot: NativeSnapshot,
        current_deliverable: list[tuple[EffectRecord, NativeInterrupt]],
        held: Mapping[str, Lease],
    ) -> NativeSnapshot:
        native_order = {
            interrupt.coordinate: index
            for index, interrupt in enumerate(self._protected(snapshot))
        }
        current_deliverable.sort(
            key=lambda item: native_order[item[1].coordinate]
        )
        source = current_deliverable[0][1].coordinate
        results = {
            interrupt.coordinate.interrupt_id: record.result.to_dict()
            for record, interrupt in current_deliverable
        }
        committed = self._runtime.resume(run_id, source, results)
        if any(
            pending.coordinate.interrupt_id in results
            for pending in committed.pending
        ):
            raise CoordinatorLineageError(
                "native resume returned without consuming the exact delivered "
                "interrupts"
            )
        for current, _interrupt in current_deliverable:
            if (
                self._protected_lineage(
                    run_id,
                    current.coordinate,
                    current.descriptor_digest,
                )
                != "descended"
            ):
                raise CoordinatorLineageError(
                    "native commit does not descend from the delivered source "
                    "interrupt"
                )
            self._ledger.mark_delivered(
                current.effect_id,
                expected_revision=current.revision,
                lease=held[current.effect_id],
            )
        return committed

    def deliver_ready(
        self, run_id: str, interrupt_ids: Sequence[str] | None = None
    ) -> ScenarioStatus:
        binding = self._binding(run_id)
        snapshot = self._runtime.snapshot(run_id, subgraphs=True)
        requested = self._requested_delivery_ids(snapshot, interrupt_ids)
        deliverable = self._deliverable_records(snapshot, requested)
        if not deliverable:
            return project_status(binding, snapshot, self._leases, self._ledger)
        held: dict[str, Lease] = {}
        try:
            current_deliverable = self._lock_deliverable_records(
                deliverable, held
            )
            if current_deliverable is None:
                current_snapshot = self._runtime.snapshot(
                    run_id, subgraphs=True
                )
                return project_status(
                    binding, current_snapshot, self._leases, self._ledger
                )
            committed = self._commit_deliverable_records(
                run_id=run_id,
                snapshot=snapshot,
                current_deliverable=current_deliverable,
                held=held,
            )
        finally:
            for lease in reversed(tuple(held.values())):
                self._leases.release(lease)
        return project_status(binding, committed, self._leases, self._ledger)
