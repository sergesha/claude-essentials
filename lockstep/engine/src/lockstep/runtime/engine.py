"""Compatibility name for the state-free public-service facade."""

from __future__ import annotations

from pathlib import Path

from lockstep.runtime.service import LockstepError, LockstepService

__all__ = ["Engine", "LockstepError"]


class Engine:
    """Delegate every operation; owns no workflow state or transition logic."""

    def __init__(self, state_dir: Path, recipes_dir: Path, memory_only: bool = False) -> None:
        if memory_only:
            raise ValueError("memory-only workflow state is not supported by native runtime")
        self._service = LockstepService(state_dir, recipes_dir)

    def __getattr__(self, name: str):
        return getattr(self._service, name)

    def close(self) -> None:
        self._service.close()
