"""Compatibility name for the state-free public-service facade."""

from __future__ import annotations

from pathlib import Path

from lockstep.runtime.errors import LockstepError
from lockstep.runtime.projection import RuntimeProjection
from lockstep.runtime.service import LockstepCommandService

__all__ = ["Engine", "LockstepError"]


class Engine:
    """Explicit selector for passive observation or command capabilities."""

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("use Engine.observe(...) or Engine.command(...)")

    @staticmethod
    def observe(state_dir: Path, recipes_dir: Path) -> RuntimeProjection:
        """Select the passive observation capability."""

        return RuntimeProjection(state_dir, recipes_dir)

    @staticmethod
    def command(
        state_dir: Path,
        recipes_dir: Path,
    ) -> LockstepCommandService:
        return LockstepCommandService(state_dir, recipes_dir)
