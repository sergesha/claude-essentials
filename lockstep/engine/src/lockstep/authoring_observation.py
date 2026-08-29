"""Serialized read-only boundary with presence-only legacy refusal."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from lockstep.authoring_bundle import PathIdentity
from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_publisher import (
    _observe_existing_authoring_project,
    observe_authoring_project,
)


Observation = TypeVar("Observation")


def observe_existing_authoring_project(
    journal: AuthoringJournal,
    project_identity: PathIdentity,
    operation: Callable[[], Observation],
) -> Observation:
    """Observe under an existing persistent lock after legacy refusal."""

    return _observe_existing_authoring_project(journal, project_identity, operation)
