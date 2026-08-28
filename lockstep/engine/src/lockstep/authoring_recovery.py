"""Fail-closed rollback of one journal-bound authoring transaction."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lockstep.authoring_committed_recovery import CommittedAuthoringRecovery
from lockstep.authoring_directory_recovery import DirectoryRecoveryPlan
from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_project_tree import AuthoringProjectTree
from lockstep.authoring_recovery_observation import (
    ObservedRecoveryFile,
    fsync_recovery_regular,
    matches_after_file,
    matches_captured_before,
    matches_desired_before,
    matches_planned_after,
    observe_recovery_file,
    require_same_recovery_file,
    same_recovery_file_identity,
)
from lockstep.authoring_recovery_model import (
    AuthoringRecoveryModel,
    RecoveryWriteEntry,
)
from lockstep.errors import AuthoringError


@dataclass(frozen=True, slots=True)
class _DestinationState:
    entry: RecoveryWriteEntry
    state: Literal["before", "after"]
    observed: ObservedRecoveryFile | None


@dataclass(frozen=True, slots=True)
class _OwnedStage:
    entry: RecoveryWriteEntry
    path: Path
    state: Literal["absent", "complete", "incomplete"]
    observed: ObservedRecoveryFile | None


def recover_authoring_project(state_dir: Path, project: Path) -> None:
    journal, project_identity = AuthoringJournal.locate_for_project(
        state_dir, project
    )
    if journal is None:
        return
    with journal.locked():
        if not journal.has_active_transaction():
            journal.sync_namespace()
            return
        model = journal.read_recovery_model(expected_project=project_identity)
        if model.committed:
            CommittedAuthoringRecovery(journal, model).recover()
        else:
            AuthoringRecovery(journal, model).recover()


class AuthoringRecovery:
    """Restore one strictly parsed journal to its exact before-image set."""

    __slots__ = ("journal", "model", "tree")

    def __init__(
        self, journal: AuthoringJournal, model: AuthoringRecoveryModel
    ) -> None:
        self.journal = journal
        self.model = model
        chains = tuple(entry.ancestors for entry in model.read_set) + tuple(
            entry.ancestors for entry in model.write_set
        )
        self.tree = AuthoringProjectTree.from_identities(model.project, chains)

    def recover(self) -> None:
        directories = DirectoryRecoveryPlan.preflight(self.tree, self.model)
        destinations = tuple(
            self._classify_destination(entry, directories)
            for entry in self.model.write_set
        )
        all_destinations_before = all(
            state.state == "before" for state in destinations
        )
        publication_stages = tuple(
            self._inspect_publication_stage(
                state,
                directories,
                incomplete_is_reclaimable=all_destinations_before,
            )
            for state in destinations
        )
        restoration_stages = tuple(
            self._inspect_restoration_stage(state, directories)
            for state in destinations
        )
        removed_absent_destinations: dict[int, ObservedRecoveryFile] = {}
        for state in reversed(destinations):
            if state.state == "after":
                if state.entry.before.absent:
                    removed_absent_destinations[state.entry.index] = (
                        self._remove_absent_destination(state)
                    )
                else:
                    self._restore(state, restoration_stages[state.entry.index])
        self._require_all_before_images(directories)
        for stage in publication_stages:
            removed_destination = removed_absent_destinations.get(
                stage.entry.index
            )
            if (
                removed_destination is not None
                and stage.observed is not None
                and same_recovery_file_identity(stage.observed, removed_destination)
            ):
                self._remove_aliased_publication_stage(
                    stage, removed_destination
                )
            else:
                self._remove_publication_stage(stage)
        for stage in restoration_stages:
            self._reconcile_restoration_stage(stage)
        self._durably_confirm_all_before_images(directories)
        directories.remove_directories()
        self.journal.finish()

    def _classify_destination(
        self,
        entry: RecoveryWriteEntry,
        directories: DirectoryRecoveryPlan,
    ) -> _DestinationState:
        if directories.parent_is_missing(entry.path):
            if not entry.before.absent:
                raise AuthoringError(
                    f"authoring recovery lost a present before-image: {entry.path}"
                )
            return _DestinationState(entry, "before", None)
        parent_descriptor, leaf = self.tree.open_parent(entry.path)
        try:
            observed = observe_recovery_file(parent_descriptor, leaf, entry.path)
        finally:
            os.close(parent_descriptor)
        if matches_captured_before(
            observed, entry.before
        ) or matches_desired_before(observed, entry.before):
            return _DestinationState(entry, "before", observed)
        if matches_planned_after(observed, entry):
            return _DestinationState(entry, "after", observed)
        raise AuthoringError(
            f"authoring recovery destination is foreign: {entry.path}"
        )

    def _inspect_publication_stage(
        self,
        destination: _DestinationState,
        directories: DirectoryRecoveryPlan,
        *,
        incomplete_is_reclaimable: bool,
    ) -> _OwnedStage:
        entry = destination.entry
        path = self.model.reservation.stages[entry.index].publication
        if directories.parent_is_missing(path):
            return _OwnedStage(entry, path, "absent", None)
        parent_descriptor, leaf = self.tree.open_parent(path)
        try:
            observed = observe_recovery_file(parent_descriptor, leaf, path)
        finally:
            os.close(parent_descriptor)
        if observed is None:
            # Exact terminal destination classification makes a missing stage
            # harmless: it was consumed, or recovery can abandon it all-old.
            return _OwnedStage(entry, path, "absent", None)
        if matches_after_file(observed, entry):
            return _OwnedStage(entry, path, "complete", observed)
        if incomplete_is_reclaimable:
            return _OwnedStage(entry, path, "incomplete", observed)
        raise AuthoringError(
            f"authoring recovery will not remove a foreign stage: {path}"
        )

    def _inspect_restoration_stage(
        self,
        destination: _DestinationState,
        directories: DirectoryRecoveryPlan,
    ) -> _OwnedStage:
        entry = destination.entry
        path = self.model.reservation.stages[entry.index].restoration
        if directories.parent_is_missing(path):
            return _OwnedStage(entry, path, "absent", None)
        parent_descriptor, leaf = self.tree.open_parent(path)
        try:
            observed = observe_recovery_file(parent_descriptor, leaf, path)
        finally:
            os.close(parent_descriptor)
        if observed is None:
            return _OwnedStage(entry, path, "absent", None)
        if matches_desired_before(observed, entry.before):
            return _OwnedStage(entry, path, "complete", observed)
        if destination.state == "after":
            return _OwnedStage(entry, path, "incomplete", observed)
        raise AuthoringError(
            f"authoring recovery restoration stage is foreign: {path}"
        )

    def _restore(
        self, state: _DestinationState, restoration_stage: _OwnedStage
    ) -> None:
        entry = state.entry
        observed = state.observed
        if observed is None:
            raise AuthoringError("authoring recovery lost its after-image observation")
        parent_descriptor, leaf = self.tree.open_parent(entry.path)
        try:
            stage_leaf, stage_observed = self._prepare_restoration_stage(
                parent_descriptor, restoration_stage
            )
            require_same_recovery_file(parent_descriptor, leaf, entry.path, observed)
            require_same_recovery_file(
                parent_descriptor,
                stage_leaf,
                restoration_stage.path,
                stage_observed,
            )
            os.replace(
                stage_leaf,
                leaf,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            fsync_recovery_regular(parent_descriptor, leaf)
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)

    def _remove_absent_destination(
        self, state: _DestinationState
    ) -> ObservedRecoveryFile:
        if not state.entry.before.absent or state.observed is None:
            raise RuntimeError("absent destination removal has invalid state")
        parent_descriptor, leaf = self.tree.open_parent(state.entry.path)
        try:
            observed = require_same_recovery_file(
                parent_descriptor, leaf, state.entry.path, state.observed
            )
            os.unlink(leaf, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
            return observed
        finally:
            os.close(parent_descriptor)

    def _prepare_restoration_stage(
        self, parent_descriptor: int, stage: _OwnedStage
    ) -> tuple[str, ObservedRecoveryFile]:
        entry = stage.entry
        before = entry.before
        if before.content is None or before.mode is None:
            raise AuthoringError("authoring recovery before-image is incomplete")
        path = stage.path
        leaf = path.name
        if stage.state == "complete":
            if stage.observed is None:
                raise RuntimeError("complete restoration stage has no observation")
            observed = require_same_recovery_file(
                parent_descriptor, leaf, path, stage.observed
            )
            return leaf, observed
        if stage.state == "incomplete":
            if stage.observed is None:
                raise RuntimeError("incomplete restoration stage has no observation")
            require_same_recovery_file(parent_descriptor, leaf, path, stage.observed)
            os.unlink(leaf, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(leaf, flags, 0o600, dir_fd=parent_descriptor)
        try:
            created = os.fstat(descriptor)
            if not stat.S_ISREG(created.st_mode):
                raise AuthoringError("authoring recovery stage is not regular")
            os.fchmod(descriptor, before.mode)
            _write_all(descriptor, before.content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent_descriptor)
        observed = observe_recovery_file(parent_descriptor, leaf, path)
        if observed is None or not matches_desired_before(observed, before):
            raise AuthoringError("authoring recovery stage could not be proven")
        return leaf, observed

    def _remove_publication_stage(self, stage: _OwnedStage) -> None:
        if stage.state == "absent":
            return
        if stage.observed is None:
            raise RuntimeError("observed publication stage has no observation")
        parent_descriptor, leaf = self.tree.open_parent(stage.path)
        try:
            current = require_same_recovery_file(
                parent_descriptor, leaf, stage.path, stage.observed
            )
            if stage.state == "complete" and not matches_after_file(
                current, stage.entry
            ):
                raise AuthoringError(
                    f"authoring recovery stage changed before cleanup: {stage.path}"
                )
            os.unlink(leaf, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)

    def _remove_aliased_publication_stage(
        self, stage: _OwnedStage, removed_destination: ObservedRecoveryFile
    ) -> None:
        if stage.state != "complete" or stage.observed is None:
            raise RuntimeError("aliased publication stage has invalid state")
        if not same_recovery_file_identity(stage.observed, removed_destination):
            raise RuntimeError("publication stage is not the destination alias")
        parent_descriptor, leaf = self.tree.open_parent(stage.path)
        try:
            observed = observe_recovery_file(parent_descriptor, leaf, stage.path)
            if (
                observed is None
                or not same_recovery_file_identity(observed, stage.observed)
                or not matches_after_file(observed, stage.entry)
            ):
                raise AuthoringError(
                    f"authoring recovery stage changed before cleanup: {stage.path}"
                )
            os.unlink(leaf, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)

    def _reconcile_restoration_stage(self, stage: _OwnedStage) -> None:
        if stage.state == "absent":
            return
        parent_descriptor, leaf = self.tree.open_parent(stage.path)
        try:
            observed = observe_recovery_file(parent_descriptor, leaf, stage.path)
            if observed is None:
                return
            if (
                stage.state != "complete"
                or stage.observed is None
                or observed != stage.observed
            ):
                raise AuthoringError(
                    f"authoring recovery restoration stage changed: {stage.path}"
                )
            if not matches_desired_before(observed, stage.entry.before):
                raise AuthoringError(
                    f"authoring recovery restoration stage is foreign: {stage.path}"
                )
            os.unlink(leaf, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)

    def _require_all_before_images(
        self, directories: DirectoryRecoveryPlan
    ) -> None:
        for entry in self.model.write_set:
            if directories.parent_is_missing(entry.path):
                if not entry.before.absent:
                    raise AuthoringError(
                        "authoring recovery lost a present before-image: "
                        f"{entry.path}"
                    )
                continue
            parent_descriptor, leaf = self.tree.open_parent(entry.path)
            try:
                observed = observe_recovery_file(
                    parent_descriptor, leaf, entry.path
                )
            finally:
                os.close(parent_descriptor)
            if not matches_desired_before(observed, entry.before):
                raise AuthoringError(
                    f"authoring recovery did not restore before-image: {entry.path}"
                )

    def _durably_confirm_all_before_images(
        self, directories: DirectoryRecoveryPlan
    ) -> None:
        for entry in self.model.write_set:
            if directories.parent_is_missing(entry.path):
                if not entry.before.absent:
                    raise AuthoringError(
                        "authoring recovery lost a present before-image: "
                        f"{entry.path}"
                    )
                continue
            parent_descriptor, leaf = self.tree.open_parent(entry.path)
            try:
                observed = observe_recovery_file(parent_descriptor, leaf, entry.path)
                if not matches_desired_before(observed, entry.before):
                    raise AuthoringError(
                        f"authoring recovery did not restore before-image: {entry.path}"
                    )
                if observed is not None:
                    fsync_recovery_regular(parent_descriptor, leaf)
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write while staging authoring recovery")
        remaining = remaining[written:]
