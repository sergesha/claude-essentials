"""Protected external-effect descriptors and durable facts."""

from lockstep.runtime.effects.descriptors import (
    build_scope_result,
    derive_effect_id,
    effective_effect_deadline,
    parse_effect_descriptor,
    parse_effect_result,
    parse_scope_result,
)
from lockstep.runtime.effects.models import (
    EffectDescriptor,
    EffectResult,
    ScopeDescriptor,
    ScopeResult,
)

__all__ = [
    "EffectDescriptor",
    "EffectResult",
    "ScopeDescriptor",
    "ScopeResult",
    "build_scope_result",
    "derive_effect_id",
    "effective_effect_deadline",
    "parse_effect_descriptor",
    "parse_effect_result",
    "parse_scope_result",
]
