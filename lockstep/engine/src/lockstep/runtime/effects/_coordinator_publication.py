"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects._coordinator_values import (
    ReconcileReport,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.effects.models import (
    PublishDescriptor,
)
from lockstep.runtime.leases import Lease
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot


class _EffectCoordinatorPublication:
    def _reconcile_publication(
        self,
        run_id: str,
        binding: RunBinding,
        snapshot: NativeSnapshot,
        descriptor: PublishDescriptor,
        interrupt: NativeInterrupt,
        effect_id: str,
        record: EffectRecord | None,
        lease: Lease,
    ) -> ReconcileReport:
        publisher = self._publisher_for(binding)
        existing = self._publication_existing_result(
            run_id=run_id,
            binding=binding,
            interrupt=interrupt,
            descriptor=descriptor,
            record=record,
            lease=lease,
            publisher=publisher,
        )
        if existing is not None:
            return existing
        request, grant, publication_request = self._publication_intent(
            binding, snapshot, interrupt, descriptor, effect_id, publisher
        )
        if record is None:
            return self._prepare_publication_effect(
                run_id=run_id,
                interrupt=interrupt,
                descriptor=descriptor,
                request=request,
                grant=grant,
                publisher=publisher,
                lease=lease,
            )
        self._validate_publication_authority(
            record=record,
            request=request,
            grant=grant,
            publisher=publisher,
        )
        prepared_publication, commitment_digest = (
            self._prepared_publication_commitment(
                publisher=publisher,
                publication_request=publication_request,
            )
        )
        return self._advance_publication_effect(
            run_id=run_id,
            binding=binding,
            interrupt=interrupt,
            descriptor=descriptor,
            record=record,
            lease=lease,
            publisher=publisher,
            request=request,
            grant=grant,
            prepared_publication=prepared_publication,
            commitment_digest=commitment_digest,
        )
