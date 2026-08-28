"""Whole-set recovery policy for transaction-created project directories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lockstep.authoring_project_tree import AuthoringProjectTree
from lockstep.authoring_recovery_model import AuthoringRecoveryModel
from lockstep.errors import AuthoringError


_DirectoryState = Literal["missing", "owned"]


@dataclass(frozen=True, slots=True)
class _DirectoryObservation:
    path: Path
    state: _DirectoryState


@dataclass(frozen=True, slots=True)
class DirectoryRecoveryPlan:
    """A preclassified directory set that grants no path-derived ownership."""

    tree: AuthoringProjectTree
    observations: tuple[_DirectoryObservation, ...]

    @classmethod
    def preflight(
        cls, tree: AuthoringProjectTree, model: AuthoringRecoveryModel
    ) -> DirectoryRecoveryPlan:
        progress = {
            identity.resolved_path: identity
            for identity in model.created_directories
        }
        allowed_children = _allowed_candidate_children(model)
        observations: list[_DirectoryObservation] = []
        missing: set[Path] = set()
        for candidate in model.directory_candidates:
            if any(parent in missing for parent in candidate.parents):
                observations.append(_DirectoryObservation(candidate, "missing"))
                missing.add(candidate)
                continue
            children = tree.inspect_created_directory(
                candidate, progress.get(candidate)
            )
            if children is None:
                observations.append(_DirectoryObservation(candidate, "missing"))
                missing.add(candidate)
                continue
            unexpected = children - allowed_children[candidate]
            if unexpected:
                raise AuthoringError(
                    "transaction-created directory contains foreign entries"
                )
            observations.append(_DirectoryObservation(candidate, "owned"))
        return cls(tree, tuple(observations))

    def parent_is_missing(self, path: Path) -> bool:
        return any(
            observation.state == "missing" and observation.path in path.parents
            for observation in self.observations
        )

    def remove_directories(self) -> None:
        missing_paths = {
            observation.path
            for observation in self.observations
            if observation.state == "missing"
        }
        for observation in reversed(self.observations):
            if observation.state != "missing":
                continue
            if not any(
                parent in missing_paths for parent in observation.path.parents
            ):
                self.tree.durably_confirm_created_directory_absent(
                    observation.path
                )
        self.tree.remove_created_directories()


def _allowed_candidate_children(
    model: AuthoringRecoveryModel,
) -> dict[Path, frozenset[str]]:
    allowed: dict[Path, set[str]] = {
        candidate: set() for candidate in model.directory_candidates
    }
    for candidate in model.directory_candidates:
        if candidate.parent in allowed:
            allowed[candidate.parent].add(candidate.name)
    for entry, stages in zip(
        model.write_set, model.reservation.stages, strict=True
    ):
        for path in (entry.path, stages.publication, stages.restoration):
            if path.parent in allowed:
                allowed[path.parent].add(path.name)
    return {path: frozenset(children) for path, children in allowed.items()}
