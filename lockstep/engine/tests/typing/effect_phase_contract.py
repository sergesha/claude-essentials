"""Executable negative typing contract for the effect phase domain."""

from enum import StrEnum

from lockstep.runtime._publication_values import PublicationPhase
from lockstep.runtime.effects._coordinator_values import ReconcileAction
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


def effect_phase_is_not_a_reconcile_action(
    effect: EffectRecord, action: ReconcileAction
) -> bool:
    return effect.phase == action  # type: ignore[comparison-overlap]


def publication_phase_is_not_a_reconcile_action(
    phase: PublicationPhase, action: ReconcileAction
) -> bool:
    return phase == action  # type: ignore[comparison-overlap]


def effect_phase_is_not_a_publication_phase(
    effect: EffectRecord, phase: PublicationPhase
) -> bool:
    return effect.phase == phase  # type: ignore[comparison-overlap]
