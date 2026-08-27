"""Fail-closed boundary for whole-DAG authoring publication and recovery."""

from __future__ import annotations

from pathlib import Path

from lockstep.authoring_bundle import ProjectCompilationBundle

__all__ = ["AuthoringPublisher"]


class AuthoringPublisher:
    """Own future authoring publication without ambient state-directory lookup."""

    __slots__ = ("_state_dir",)

    def __init__(self, state_dir: Path) -> None:
        if not isinstance(state_dir, Path):
            raise TypeError("authoring state directory must be a Path")
        if not state_dir.is_absolute() or any(
            part in {".", ".."} for part in state_dir.parts
        ):
            raise ValueError(
                "authoring state directory must be absolute and lexically canonical"
            )
        self._state_dir = state_dir

    def publish(self, bundle: ProjectCompilationBundle) -> None:
        del bundle
        raise NotImplementedError("authoring publication is not implemented")

    def recover(self, project: Path) -> None:
        del project
        raise NotImplementedError("authoring recovery is not implemented")
