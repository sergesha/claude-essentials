"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from typing import Any

from lockstep.runtime._publication_values import PublicationPhase
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects._coordinator_values import (
    ReconcileAction,
    ReconcileReport,
    make_reconcile_report,
)
from lockstep.runtime.effects.descriptors import (
    parse_effect_descriptor,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.effects.models import (
    EffectResult,
    PublishDescriptor,
)
from lockstep.runtime.leases import Lease
from lockstep.runtime.native_models import NativeInterrupt
from lockstep.runtime.publication import (
    ProjectPublisher,
)


class _EffectCoordinatorPublicationRecoveryTransaction:
    def _capture_publication_successor(
        self,
        *,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: PublishDescriptor,
        effect_id: str,
    ) -> None:
        if self._snapshot_resolver is not None:
            self._snapshot_resolver.capture_successor(
                binding,
                interrupt,
                descriptor,
                effect_id,
                purpose="publication",
            )

    def _publication_recovery_receipt(
        self,
        *,
        publisher: ProjectPublisher,
        prepared_publication: Any,
        recovery_phase: PublicationPhase,
        effect_id: str,
    ) -> tuple[Any, EffectResult]:
        if recovery_phase in {
            PublicationPhase.ROLLBACK_PENDING,
            PublicationPhase.ROLLED_BACK,
        }:
            receipt = publisher.rollback_or_recover(prepared_publication)
            result = self._publication_error_result(
                effect_id, receipt.journal_digest
            )
        else:
            receipt = publisher.apply_or_recover(prepared_publication)
            result = self._publication_result(effect_id, receipt.journal_digest)
        return receipt, result

    def _guarded_publication_recovery(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        descriptor: PublishDescriptor,
        record: EffectRecord,
        lease: Lease,
        publication_lease: Lease,
        publisher: ProjectPublisher,
        prepared_publication: Any,
        recovery_phase: PublicationPhase,
    ) -> ReconcileReport | tuple[Any, EffectResult]:
        with self._runtime.commitment_guard(
            run_id, record.coordinate
        ) as guarded:
            guarded_descriptor = parse_effect_descriptor(
                self._raw_descriptor(guarded.interrupt)
            )
            current = self._ledger.get(record.effect_id)
            if not self._publication_recovery_guard_is_current(
                guarded=guarded,
                binding=binding,
                guarded_descriptor=guarded_descriptor,
                descriptor=descriptor,
                current=current,
                record=record,
                lease=lease,
                publication_lease=publication_lease,
            ):
                return make_reconcile_report(run_id, current, ReconcileAction.BUSY)
            return self._publication_recovery_receipt(
                publisher=publisher,
                prepared_publication=prepared_publication,
                recovery_phase=recovery_phase,
                effect_id=record.effect_id,
            )

    def _finalize_publication_recovery(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: PublishDescriptor,
        record: EffectRecord,
        lease: Lease,
        publisher: ProjectPublisher,
        receipt: Any,
        result: EffectResult,
    ) -> ReconcileReport:
        receipt_phase = PublicationPhase(receipt.phase)
        if receipt_phase not in {
            PublicationPhase.APPLIED,
            PublicationPhase.ROLLED_BACK,
        }:
            return make_reconcile_report(
                run_id, record, ReconcileAction.PUBLICATION_PROGRESS
            )
        if receipt_phase is PublicationPhase.APPLIED:
            self._capture_publication_successor(
                binding=binding,
                interrupt=interrupt,
                descriptor=descriptor,
                effect_id=record.effect_id,
            )
        sealed = self._ledger.seal(
            record.effect_id,
            result,
            expected_revision=record.revision,
            lease=lease,
            runner_binding_digest=publisher.binding_digest,
        )
        return make_reconcile_report(run_id, sealed, ReconcileAction.SEALED)
