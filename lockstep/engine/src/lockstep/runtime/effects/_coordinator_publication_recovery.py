"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from typing import Any

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects._coordinator_values import (
    CoordinatorLineageError,
    ReconcileReport,
)
from lockstep.runtime.effects.authority import (
    EffectGrant,
)
from lockstep.runtime.effects.descriptors import (
    parse_effect_descriptor,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.effects.models import (
    PublishDescriptor,
)
from lockstep.runtime.leases import Lease
from lockstep.runtime.native_models import NativeInterrupt
from lockstep.runtime.providers.base import (
    EffectRequest,
)
from lockstep.runtime.publication import (
    ProjectPublisher,
)


class _EffectCoordinatorPublicationRecovery:
    def _commit_publication_recovery(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: PublishDescriptor,
        record: EffectRecord,
        lease: Lease,
        publisher: ProjectPublisher,
        prepared_publication: Any,
        recovery_phase: str,
    ) -> ReconcileReport:
        publication_lease = self._publication_lease(binding)
        if publication_lease is None:
            return self._report(run_id, record, "busy")
        try:
            recovered = self._guarded_publication_recovery(
                run_id=run_id,
                binding=binding,
                descriptor=descriptor,
                record=record,
                lease=lease,
                publication_lease=publication_lease,
                publisher=publisher,
                prepared_publication=prepared_publication,
                recovery_phase=recovery_phase,
            )
            if isinstance(recovered, ReconcileReport):
                return recovered
            receipt, result = recovered
            return self._finalize_publication_recovery(
                run_id=run_id,
                binding=binding,
                interrupt=interrupt,
                descriptor=descriptor,
                record=record,
                lease=lease,
                publisher=publisher,
                receipt=receipt,
                result=result,
            )
        finally:
            self._leases.release(publication_lease)

    def _recover_publication(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: PublishDescriptor,
        record: EffectRecord,
        lease: Lease,
        publisher: ProjectPublisher,
    ) -> ReconcileReport | None:
        recovering = publisher.prepared_for(
            record.effect_id, record.request_digest or ""
        )
        if recovering is None or recovering[1] not in {
            "applying", "applied", "rollback_pending", "rolled_back"
        }:
            return None
        prepared_publication, recovery_phase = recovering
        if (
            record.launch_commitment_digest
            != publisher.commitment_digest(prepared_publication)
        ):
            raise CoordinatorLineageError(
                "recovery journal differs from durable publication commitment"
            )
        return self._commit_publication_recovery(
            run_id=run_id,
            binding=binding,
            interrupt=interrupt,
            descriptor=descriptor,
            record=record,
            lease=lease,
            publisher=publisher,
            prepared_publication=prepared_publication,
            recovery_phase=recovery_phase,
        )

    def _commit_prepared_publication(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: PublishDescriptor,
        record: EffectRecord,
        lease: Lease,
        publisher: ProjectPublisher,
        request: EffectRequest,
        grant: EffectGrant,
        prepared_publication: Any,
    ) -> ReconcileReport:
        publication_lease = self._publication_lease(binding)
        if publication_lease is None:
            return self._report(run_id, record, "busy")
        try:
            with self._runtime.commitment_guard(
                run_id, record.coordinate
            ) as guarded:
                guarded_descriptor = parse_effect_descriptor(
                    self._raw_descriptor(guarded.interrupt)
                )
                if (
                    guarded.binding != binding
                    or guarded.interrupt.coordinate != record.coordinate
                    or guarded_descriptor != descriptor
                ):
                    raise CoordinatorLineageError(
                        "publication graph authority changed before commitment"
                    )
                current = self._ledger.get(record.effect_id)
                if (
                    current.revision != record.revision
                    or current.phase != "launching"
                    or not self._leases.is_current(lease)
                    or not self._leases.is_current(publication_lease)
                ):
                    return self._report(run_id, current, "busy")
                with self._authority.commitment(
                    grant, request, prepared_publication
                ):
                    receipt = publisher.apply_or_recover(prepared_publication)
            if receipt.phase != "applied":
                return self._report(run_id, record, "publication_progress")
            self._capture_publication_successor(
                binding=binding,
                interrupt=interrupt,
                descriptor=descriptor,
                effect_id=record.effect_id,
            )
            sealed = self._ledger.seal(
                record.effect_id,
                self._publication_result(
                    record.effect_id, receipt.journal_digest
                ),
                expected_revision=record.revision,
                lease=lease,
                runner_binding_digest=publisher.binding_digest,
            )
            return self._report(run_id, sealed, "sealed")
        finally:
            self._leases.release(publication_lease)
