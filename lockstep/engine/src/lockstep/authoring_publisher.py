"""Fail-closed boundary for whole-DAG authoring publication and recovery."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from lockstep.authoring_bundle import PathIdentity, ProjectCompilationBundle
from lockstep.authoring_identity import validate_bundle_preconditions
from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_recovery import (
    recover_authoring_project,
    recover_locked_authoring_project,
)
from lockstep.authoring_transaction import AuthoringTransaction
from lockstep.runtime.errors import LockstepError

__all__ = ["AuthoringPublisher", "observe_authoring_project"]


Observation = TypeVar("Observation")


class _ExistingAuthoringBoundary:
    """The already-created authoring namespace that readers may lock."""

    __slots__ = ("_journal", "_project_identity")

    def __init__(
        self, journal: AuthoringJournal, project_identity: PathIdentity
    ) -> None:
        self._journal = journal
        self._project_identity = project_identity

    def observe(self, operation: Callable[[], Observation]) -> Observation:
        with self._journal.locked_existing():
            recover_locked_authoring_project(self._journal, self._project_identity)
            return operation()


def _locate_existing_boundary(
    state_dir: Path, project: Path
) -> _ExistingAuthoringBoundary | None:
    """Find a reader-safe boundary without creating owner state."""

    journal, project_identity = AuthoringJournal.locate_ready_for_project(
        state_dir, project
    )
    if journal is None:
        return None
    return _ExistingAuthoringBoundary(journal, project_identity)


def _observe_existing_authoring_project(
    journal: AuthoringJournal,
    project_identity: PathIdentity,
    operation: Callable[[], Observation],
) -> Observation:
    return _ExistingAuthoringBoundary(journal, project_identity).observe(operation)


def observe_authoring_project(
    state_dir: Path,
    project: Path,
    operation: Callable[[], Observation],
) -> Observation:
    boundary = _locate_existing_boundary(state_dir, project)
    if boundary is not None:
        return boundary.observe(operation)
    try:
        optimistic = operation()
    except (LockstepError, OSError, ValueError):
        boundary = _locate_existing_boundary(state_dir, project)
        if boundary is None:
            raise
        return boundary.observe(operation)
    boundary = _locate_existing_boundary(state_dir, project)
    return optimistic if boundary is None else boundary.observe(operation)


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

    def observe(
        self, project: Path, operation: Callable[[], Observation]
    ) -> Observation:
        if not isinstance(project, Path):
            raise TypeError("authoring project must be a Path")
        return observe_authoring_project(self._state_dir, project, operation)
