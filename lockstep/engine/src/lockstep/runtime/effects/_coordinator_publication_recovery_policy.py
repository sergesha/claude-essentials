"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from typing import Any

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.effects.models import (
    EffectDescriptor,
    PublishDescriptor,
    ScopeDescriptor,
)
from lockstep.runtime.leases import Lease


class _EffectCoordinatorPublicationRecoveryPolicy:
    def _publication_recovery_guard_is_current(
        self,
        *,
        guarded: Any,
        binding: RunBinding,
        guarded_descriptor: EffectDescriptor | ScopeDescriptor,
        descriptor: PublishDescriptor,
        current: EffectRecord,
        record: EffectRecord,
        lease: Lease,
        publication_lease: Lease,
    ) -> bool:
        return (
            guarded.binding == binding
            and guarded.interrupt.coordinate == record.coordinate
            and guarded_descriptor == descriptor
            and current.revision == record.revision
            and current.phase == "launching"
            and self._leases.is_current(lease)
            and self._leases.is_current(publication_lease)
        )
