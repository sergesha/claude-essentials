"""Fail-closed rollback of one journal-bound authoring transaction."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_project_tree import AuthoringProjectTree
from lockstep.authoring_recovery_model import (
    AuthoringRecoveryModel,
    RecoveryBeforeImage,
    RecoveryWriteEntry,
)
from lockstep.errors import AuthoringError
from lockstep.recipe.authority import RecipeLimits


_MAX_FILE_BYTES = RecipeLimits().max_file_bytes


@dataclass(frozen=True, slots=True)
class _ObservedFile:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class _DestinationState:
    entry: RecoveryWriteEntry
    state: Literal["before", "after"]
    observed: _ObservedFile | None


@dataclass(frozen=True, slots=True)
class _OwnedStage:
    entry: RecoveryWriteEntry
    path: Path
    state: Literal["absent", "complete", "incomplete"]
    observed: _ObservedFile | None


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
        if any(entry.before.absent for entry in self.model.write_set):
            raise AuthoringError(
                "authoring recovery supports only existing destination before-images"
            )
        destinations = tuple(
            self._classify_destination(entry) for entry in self.model.write_set
        )
        all_destinations_before = all(
            state.state == "before" for state in destinations
        )
        publication_stages = tuple(
            self._inspect_publication_stage(
                state, incomplete_is_reclaimable=all_destinations_before
            )
            for state in destinations
        )
        restoration_stages = tuple(
            self._inspect_restoration_stage(state) for state in destinations
        )
        for state in reversed(destinations):
            if state.state == "after":
                self._restore(state, restoration_stages[state.entry.index])
        self._require_all_before_images()
        for stage in publication_stages:
            self._remove_publication_stage(stage)
        for stage in restoration_stages:
            self._reconcile_restoration_stage(stage)
        self._durably_confirm_all_before_images()
        self.journal.finish()

    def _classify_destination(
        self, entry: RecoveryWriteEntry
    ) -> _DestinationState:
        parent_descriptor, leaf = self.tree.open_parent(entry.path)
        try:
            observed = _observe_file(parent_descriptor, leaf, entry.path)
        finally:
            os.close(parent_descriptor)
        if _matches_captured_before(
            observed, entry.before
        ) or _matches_desired_before(observed, entry.before):
            return _DestinationState(entry, "before", observed)
        if _matches_planned_after(observed, entry):
            return _DestinationState(entry, "after", observed)
        raise AuthoringError(
            f"authoring recovery destination is foreign: {entry.path}"
        )

    def _inspect_publication_stage(
        self,
        destination: _DestinationState,
        *,
        incomplete_is_reclaimable: bool,
    ) -> _OwnedStage:
        entry = destination.entry
        path = self.model.reservation.stages[entry.index].publication
        parent_descriptor, leaf = self.tree.open_parent(path)
        try:
            observed = _observe_file(parent_descriptor, leaf, path)
        finally:
            os.close(parent_descriptor)
        if observed is None:
            # Exact terminal destination classification makes a missing stage
            # harmless: it was consumed, or recovery can abandon it all-old.
            return _OwnedStage(entry, path, "absent", None)
        if _matches_after_file(observed, entry):
            return _OwnedStage(entry, path, "complete", observed)
        if incomplete_is_reclaimable:
            return _OwnedStage(entry, path, "incomplete", observed)
        raise AuthoringError(
            f"authoring recovery will not remove a foreign stage: {path}"
        )

    def _inspect_restoration_stage(
        self, destination: _DestinationState
    ) -> _OwnedStage:
        entry = destination.entry
        path = self.model.reservation.stages[entry.index].restoration
        parent_descriptor, leaf = self.tree.open_parent(path)
        try:
            observed = _observe_file(parent_descriptor, leaf, path)
        finally:
            os.close(parent_descriptor)
        if observed is None:
            return _OwnedStage(entry, path, "absent", None)
        if _matches_desired_before(observed, entry.before):
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
            _require_same_file(parent_descriptor, leaf, entry.path, observed)
            _require_same_file(
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
            _fsync_regular_at(parent_descriptor, leaf)
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)

    def _prepare_restoration_stage(
        self, parent_descriptor: int, stage: _OwnedStage
    ) -> tuple[str, _ObservedFile]:
        entry = stage.entry
        before = entry.before
        if before.content is None or before.mode is None:
            raise AuthoringError("authoring recovery before-image is incomplete")
        path = stage.path
        leaf = path.name
        if stage.state == "complete":
            if stage.observed is None:
                raise RuntimeError("complete restoration stage has no observation")
            observed = _require_same_file(
                parent_descriptor, leaf, path, stage.observed
            )
            return leaf, observed
        if stage.state == "incomplete":
            if stage.observed is None:
                raise RuntimeError("incomplete restoration stage has no observation")
            _require_same_file(parent_descriptor, leaf, path, stage.observed)
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
        observed = _observe_file(parent_descriptor, leaf, path)
        if observed is None or not _matches_desired_before(observed, before):
            raise AuthoringError("authoring recovery stage could not be proven")
        return leaf, observed

    def _remove_publication_stage(self, stage: _OwnedStage) -> None:
        if stage.state == "absent":
            return
        if stage.observed is None:
            raise RuntimeError("observed publication stage has no observation")
        parent_descriptor, leaf = self.tree.open_parent(stage.path)
        try:
            current = _require_same_file(
                parent_descriptor, leaf, stage.path, stage.observed
            )
            if stage.state == "complete" and not _matches_after_file(
                current, stage.entry
            ):
                raise AuthoringError(
                    f"authoring recovery stage changed before cleanup: {stage.path}"
                )
            os.unlink(leaf, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)

    def _reconcile_restoration_stage(self, stage: _OwnedStage) -> None:
        parent_descriptor, leaf = self.tree.open_parent(stage.path)
        try:
            observed = _observe_file(parent_descriptor, leaf, stage.path)
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
            if not _matches_desired_before(observed, stage.entry.before):
                raise AuthoringError(
                    f"authoring recovery restoration stage is foreign: {stage.path}"
                )
            os.unlink(leaf, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)

    def _require_all_before_images(self) -> None:
        for entry in self.model.write_set:
            parent_descriptor, leaf = self.tree.open_parent(entry.path)
            try:
                observed = _observe_file(parent_descriptor, leaf, entry.path)
            finally:
                os.close(parent_descriptor)
            if not _matches_desired_before(observed, entry.before):
                raise AuthoringError(
                    f"authoring recovery did not restore before-image: {entry.path}"
                )

    def _durably_confirm_all_before_images(self) -> None:
        for entry in self.model.write_set:
            parent_descriptor, leaf = self.tree.open_parent(entry.path)
            try:
                observed = _observe_file(parent_descriptor, leaf, entry.path)
                if observed is None or not _matches_desired_before(
                    observed, entry.before
                ):
                    raise AuthoringError(
                        f"authoring recovery did not restore before-image: {entry.path}"
                    )
                _fsync_regular_at(parent_descriptor, leaf)
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)


def _observe_file(
    parent_descriptor: int, leaf: str, path: Path
) -> _ObservedFile | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(leaf, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AuthoringError(f"authoring recovery path is unavailable: {path}") from exc
    try:
        first = os.fstat(descriptor)
        if not stat.S_ISREG(first.st_mode):
            raise AuthoringError(f"authoring recovery path is not regular: {path}")
        if first.st_size > _MAX_FILE_BYTES:
            raise AuthoringError(
                f"authoring recovery path exceeds its byte limit: {path}"
            )
        chunks: list[bytes] = []
        remaining = first.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        last = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _file_facts(first) != _file_facts(last):
        raise AuthoringError(f"authoring recovery path changed while reading: {path}")
    content = b"".join(chunks)
    if len(content) != first.st_size:
        raise AuthoringError(f"authoring recovery path changed size: {path}")
    return _ObservedFile(
        first.st_dev,
        first.st_ino,
        first.st_mode,
        first.st_size,
        first.st_mtime_ns,
        first.st_ctime_ns,
        hashlib.sha256(content).hexdigest(),
        content,
    )


def _matches_captured_before(
    observed: _ObservedFile | None, before: RecoveryBeforeImage
) -> bool:
    if before.absent:
        return observed is None
    leaf = before.leaf
    return bool(
        observed is not None
        and leaf is not None
        and before.content is not None
        and (
            observed.device,
            observed.inode,
            observed.mode,
            observed.size,
            observed.mtime_ns,
            observed.ctime_ns,
        )
        == (
            leaf.device,
            leaf.inode,
            leaf.mode,
            leaf.size,
            leaf.mtime_ns,
            leaf.ctime_ns,
        )
        and observed.content == before.content
        and observed.sha256 == before.sha256
    )


def _matches_planned_after(
    observed: _ObservedFile | None, entry: RecoveryWriteEntry
) -> bool:
    return observed is not None and _matches_after_file(observed, entry)


def _matches_after_file(observed: _ObservedFile, entry: RecoveryWriteEntry) -> bool:
    after = entry.after
    return (
        observed.size == after.size
        and observed.sha256 == after.sha256
        and stat.S_IMODE(observed.mode) == after.mode
    )


def _matches_desired_before(
    observed: _ObservedFile | None, before: RecoveryBeforeImage
) -> bool:
    if before.absent:
        return observed is None
    return bool(
        observed is not None
        and before.content is not None
        and observed.content == before.content
        and observed.sha256 == before.sha256
        and stat.S_IMODE(observed.mode) == before.mode
    )


def _require_same_file(
    parent_descriptor: int,
    leaf: str,
    path: Path,
    expected: _ObservedFile,
) -> _ObservedFile:
    observed = _observe_file(parent_descriptor, leaf, path)
    if observed is None or observed != expected:
        raise AuthoringError(f"authoring recovery path changed before mutation: {path}")
    return observed


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write while staging authoring recovery")
        remaining = remaining[written:]


def _fsync_regular_at(parent_descriptor: int, leaf: str) -> None:
    descriptor = os.open(
        leaf,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AuthoringError("authoring recovery destination is not regular")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_facts(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
