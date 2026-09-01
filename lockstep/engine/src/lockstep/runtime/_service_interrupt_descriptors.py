"""Capability owner extracted from the command-service facade."""

from __future__ import annotations

from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.effects.models import EffectDescriptor
from lockstep.runtime.engine_drive_service import (
    ProtectedDescriptor,
)
from lockstep.runtime.errors import LockstepError


class _ServiceInterruptDescriptors:
    @staticmethod
    def _protected_interrupt_descriptor(
        interrupt,
    ) -> ProtectedDescriptor | None:
        value = interrupt.value
        if not isinstance(value, dict):
            return None
        raw = value.get("lockstep_effect")
        if not isinstance(raw, dict) or raw.get("schema") != "lockstep.effect/v1":
            return None
        try:
            descriptor = parse_effect_descriptor(raw)
        except (TypeError, ValueError) as exc:
            raise LockstepError("invalid protected worker interrupt") from exc
        return descriptor

    @staticmethod
    def _protected_descriptor(interrupt) -> EffectDescriptor | None:
        descriptor = LockstepCommandService._protected_interrupt_descriptor(interrupt)
        return descriptor if isinstance(descriptor, EffectDescriptor) else None


# Preserve the original class-qualified static dispatch without importing the
# public facade back into this dependency module.
LockstepCommandService = _ServiceInterruptDescriptors
