"""Resolve existing publication effects before preparing a new transition."""

from __future__ import annotations

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects._coordinator_values import (
    ReconcileAction,
    ReconcileReport,
    make_reconcile_report,
)
from lockstep.runtime.effects.ledger import EffectRecord
from lockstep.runtime.effects.models import PublishDescriptor
from lockstep.runtime.leases import Lease
from lockstep.runtime.native_models import NativeInterrupt
from lockstep.runtime.publication import ProjectPublisher


class _EffectCoordinatorPublicationExisting:
    def _publication_existing_result(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: PublishDescriptor,
        record: EffectRecord | None,
        lease: Lease,
        publisher: ProjectPublisher,
    ) -> ReconcileReport | None:
        if record is not None and record.phase in {"sealed", "indeterminate"}:
            return make_reconcile_report(run_id, record, ReconcileAction.AWAITING_DELIVERY)
        if record is not None and record.phase == "launching":
            recovered = self._recover_publication(
                run_id=run_id,
                binding=binding,
                interrupt=interrupt,
                descriptor=descriptor,
                record=record,
                lease=lease,
                publisher=publisher,
            )
            if recovered is not None:
                return recovered
        return None
