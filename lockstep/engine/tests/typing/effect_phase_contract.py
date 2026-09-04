"""Executable negative typing contract for the effect phase domain."""

from enum import StrEnum

from lockstep.runtime.effects.ledger import EffectRecord
from lockstep.runtime.read_resources import ProjectedEffect


class ForeignPhase(StrEnum):
    PREPARED = "prepared"


def ledger_phase_is_not_a_foreign_phase(
    effect: EffectRecord, foreign: ForeignPhase
) -> bool:
    return effect.phase == foreign  # type: ignore[comparison-overlap]


def projected_phase_is_not_a_foreign_phase(
    effect: ProjectedEffect, foreign: ForeignPhase
) -> bool:
    return effect.phase == foreign  # type: ignore[comparison-overlap]
