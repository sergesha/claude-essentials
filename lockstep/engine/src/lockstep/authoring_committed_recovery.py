"""Fail-closed completion of a durably committed authoring transaction."""

from __future__ import annotations

import os

from lockstep.authoring_directory_recovery import DirectoryRecoveryPlan
from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_project_tree import AuthoringProjectTree
from lockstep.authoring_recovery_model import (
    AuthoringRecoveryModel,
    RecoveryWriteEntry,
)
from lockstep.authoring_recovery_observation import (
    ObservedRecoveryFile,
    fsync_recovery_regular,
    matches_planned_after,
    observe_recovery_file,
    require_same_recovery_file,
)
from lockstep.errors import AuthoringError


class CommittedAuthoringRecovery:
    """Confirm one committed after-image set without consulting mutable sources."""

    __slots__ = ("journal", "model", "tree")

    def __init__(
        self, journal: AuthoringJournal, model: AuthoringRecoveryModel
    ) -> None:
        if not model.committed:
            raise ValueError("committed authoring recovery requires committed evidence")
        self.journal = journal
        self.model = model
        chains = tuple(entry.ancestors for entry in model.read_set) + tuple(
            entry.ancestors for entry in model.write_set
        )
        self.tree = AuthoringProjectTree.from_identities(model.project, chains)

    def recover(self) -> None:
        directories = DirectoryRecoveryPlan.preflight(self.tree, self.model)
        destinations = tuple(
            self._observe_after_image(entry, directories)
            for entry in self.model.write_set
        )
        reservation = self.tree.prove_reserved_stage_absence(
            self.model.operation_id, self.model.reservation.stages
        )
        if reservation != self.model.reservation:
            raise AuthoringError(
                "authoring committed recovery stage reservation changed"
            )
        self._durably_confirm_after_images(destinations)
        self.journal.finish()

    def _observe_after_image(
        self,
        entry: RecoveryWriteEntry,
        directories: DirectoryRecoveryPlan,
    ) -> ObservedRecoveryFile:
        if directories.parent_is_missing(entry.path):
            raise AuthoringError(
                f"authoring committed destination is missing: {entry.path}"
            )
        parent_descriptor, leaf = self.tree.open_parent(entry.path)
        try:
            observed = observe_recovery_file(parent_descriptor, leaf, entry.path)
        finally:
            os.close(parent_descriptor)
        if observed is None or not matches_planned_after(observed, entry):
            raise AuthoringError(
                f"authoring committed destination is not its after-image: {entry.path}"
            )
        return observed

    def _durably_confirm_after_images(
        self, observations: tuple[ObservedRecoveryFile, ...]
    ) -> None:
        for entry, expected in zip(
            self.model.write_set, observations, strict=True
        ):
            parent_descriptor, leaf = self.tree.open_parent(entry.path)
            try:
                require_same_recovery_file(
                    parent_descriptor, leaf, entry.path, expected
                )
                fsync_recovery_regular(parent_descriptor, leaf)
                require_same_recovery_file(
                    parent_descriptor, leaf, entry.path, expected
                )
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
