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
from lockstep.runtime.publication import ProjectPublisher


class _EffectCoordinatorPublicationTransition:
    def _advance_publication_effect(
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
        commitment_digest: str,
    ) -> ReconcileReport:
        if record.phase == "prepared":
            claimed = self._ledger.mark_launching(
                record.effect_id,
                expected_revision=record.revision,
                lease=lease,
                runner_binding_digest=publisher.binding_digest,
                launch_commitment_digest=commitment_digest,
            )
            return self._report(run_id, claimed, "publication_claimed")
        if record.phase == "launching":
            if record.launch_commitment_digest != commitment_digest:
                raise CoordinatorLineageError(
                    "publication journal differs from durable commitment"
                )
            return self._commit_prepared_publication(
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
            )
        raise CoordinatorLineageError("publication has an impossible ledger phase")
