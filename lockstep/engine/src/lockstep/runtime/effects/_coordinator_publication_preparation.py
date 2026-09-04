"""Prepare the durable effect record for a new publication."""

from __future__ import annotations

from lockstep.runtime.effects._coordinator_values import (
    ReconcileAction,
    ReconcileReport,
    make_reconcile_report,
)
from lockstep.runtime.effects.authority import EffectGrant
from lockstep.runtime.effects.models import PublishDescriptor
from lockstep.runtime.leases import Lease
from lockstep.runtime.native_models import NativeInterrupt
from lockstep.runtime.providers.base import EffectRequest
from lockstep.runtime.publication import ProjectPublisher


class _EffectCoordinatorPublicationPreparation:
    def _prepare_publication_effect(
        self,
        *,
        run_id: str,
        interrupt: NativeInterrupt,
        descriptor: PublishDescriptor,
        request: EffectRequest,
        grant: EffectGrant,
        publisher: ProjectPublisher,
        lease: Lease,
    ) -> ReconcileReport:
        prepared = self._ledger.prepare(
            interrupt.coordinate,
            descriptor,
            deadline_at=None,
            runner_binding_digest=publisher.binding_digest,
            workspace_ref=None,
            request_digest=request.request_digest,
            grant_digest=grant.digest,
            lease=lease,
        )
        return make_reconcile_report(run_id, prepared, ReconcileAction.PREPARED)
