"""Serialized recovery boundary for authoring filesystem observations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_recovery import recover_locked_authoring_project


Observation = TypeVar("Observation")


def observe_authoring_project(
    state_dir: Path,
    project: Path,
    operation: Callable[[], Observation],
) -> Observation:
    """Recover and derive one immutable result under the project writer lock."""

    journal, project_identity = AuthoringJournal.create_for_project(
        state_dir, project
    )
    with journal.locked():
        recover_locked_authoring_project(journal, project_identity)
        return operation()
