"""Fail-closed boundary for whole-DAG authoring publication and recovery."""

from __future__ import annotations

from pathlib import Path

from lockstep.authoring_bundle import ProjectCompilationBundle
from lockstep.authoring_identity import validate_bundle_preconditions
from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_recovery import recover_authoring_project
from lockstep.authoring_transaction import AuthoringTransaction

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
        validate_bundle_preconditions(bundle)
        journal = AuthoringJournal.create_for_bundle(self._state_dir, bundle)
        with journal.locked():
            journal.require_inactive()
            AuthoringTransaction(bundle, journal).publish()

    def recover(self, project: Path) -> None:
        if not isinstance(project, Path):
            raise TypeError("authoring project must be a Path")
        recover_authoring_project(self._state_dir, project)
