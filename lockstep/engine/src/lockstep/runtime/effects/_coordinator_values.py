"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from lockstep.runtime.effects.authority import (
    EffectGrant,
)
from lockstep.runtime.effects.models import (
    EffectDescriptor,
    ScopeDescriptor,
    ScopeResult,
)
from lockstep.runtime.native_models import NativeInterrupt
from lockstep.runtime.providers.base import (
    EffectRequest,
    RunnerAdapter,
)
from lockstep.runtime.publication import (
    PublicationEntry,
)

if TYPE_CHECKING:
    from lockstep.runtime.effects.ledger import EffectRecord


class CoordinatorLineageError(RuntimeError):
    """Durable effect facts disagree with the current public native lineage."""

class ProviderContractViolation(RuntimeError):
    """A runner returned an unbound, malformed, or authority-bearing value."""


class ReconcileAction(StrEnum):
    """Closed coordinator outcome consumed by the engine drive loop."""

    ACCEPTANCE_PENDING = "acceptance_pending"
    AUTHORITY_BLOCKED = "authority_blocked"
    AWAITING_DELIVERY = "awaiting_delivery"
    BUSY = "busy"
    DEADLINE_BLOCKED = "deadline_blocked"
    DELIVERED = "delivered"
    INDETERMINATE = "indeterminate"
    LAUNCH_CLAIMED = "launch_claimed"
    MANUAL_PENDING = "manual_pending"
    NO_EFFECT = "no_effect"
    PREPARED = "prepared"
    PUBLICATION_CLAIMED = "publication_claimed"
    PUBLICATION_PROGRESS = "publication_progress"
    QUIESCENCE_PENDING = "quiescence_pending"
    RUNNING = "running"
    SEALED = "sealed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class ReconcileReport:
    run_id: str
    effect_id: str | None
    action: str
    phase: str | None


def make_reconcile_report(
    run_id: str, record: EffectRecord, action: ReconcileAction
) -> ReconcileReport:
    return ReconcileReport(
        run_id, record.effect_id, action.value, str(record.phase)
    )

@dataclass(frozen=True)
class _Context:
    interrupt: NativeInterrupt
    descriptor: EffectDescriptor | ScopeDescriptor
    effect_id: str
    deadline_at: datetime | None
    scope_result: ScopeResult | None
    request: EffectRequest | None
    runner: RunnerAdapter | None
    grant: EffectGrant | None

@dataclass(frozen=True)
class _PublicationItemContext:
    entry: PublicationEntry
    intent_input: tuple[str, object]
    approval_generation: int
    consent_ref: str
