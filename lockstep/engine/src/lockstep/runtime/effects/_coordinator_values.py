"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

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


class CoordinatorLineageError(RuntimeError):
    """Durable effect facts disagree with the current public native lineage."""

class ProviderContractViolation(RuntimeError):
    """A runner returned an unbound, malformed, or authority-bearing value."""

@dataclass(frozen=True)
class ReconcileReport:
    run_id: str
    effect_id: str | None
    action: str
    phase: str | None

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
