"""Durable same-directory filesystem transaction for authored outputs."""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from lockstep.authoring_bundle import DestinationImage, ProjectCompilationBundle
from lockstep.authoring_identity import (
    PublishedIdentity,
    capture_after_identity_at,
    classify_destination_ownership_at,
    validate_after_identity_at,
    validate_bundle_preconditions,
    validate_destination_before_at,
    validate_sources,
)
from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_project_tree import AuthoringProjectTree
from lockstep.authoring_stage_paths import ReservedStagePaths, reserved_stage_set
from lockstep.errors import AuthoringError


@dataclass(frozen=True, slots=True)
class _StageOwnership:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _StagedFile:
    path: Path
    identity: PublishedIdentity


@dataclass(frozen=True, slots=True)
class _ReplacementOwnership:
    before: DestinationImage
    after: DestinationImage
    identity: PublishedIdentity
    reservation: ReservedStagePaths


class AuthoringTransaction:
    __slots__ = ("bundle", "journal", "operation_id", "reservations", "tree")

    def __init__(
        self, bundle: ProjectCompilationBundle, journal: AuthoringJournal
    ) -> None:
        self.bundle = bundle
        self.journal = journal
        self.operation_id = secrets.token_hex(16)
        self.reservations = reserved_stage_set(
            tuple(image.resolved_path for image in bundle.after_images),
            self.operation_id,
        )
        self.tree = AuthoringProjectTree(bundle)

    def publish(self) -> None:
        validate_bundle_preconditions(self.bundle)
        reservation = self.tree.prove_reserved_stage_absence(
            self.operation_id, self.reservations
        )
        self.journal.begin(self.bundle, reservation)
        staged: dict[Path, _StagedFile] = {}
        owned_stages: dict[Path, _StageOwnership | None] = {}
        consumed_stages: set[Path] = set()
        stage_consumption_attempts: set[Path] = set()
        owned_replacements: list[_ReplacementOwnership] = []
        try:
            self._create_destination_directories()
            self._stage_after_images(staged, owned_stages)
            for index, (before, after) in enumerate(
                zip(self.bundle.before_images, self.bundle.after_images, strict=True)
            ):
                validate_sources(self.bundle.sources)
                self._publish_one(
                    before,
                    after,
                    staged[after.resolved_path],
                    self.reservations[index],
                    owned_replacements,
                    consumed_stages,
                    stage_consumption_attempts,
                )
                self.journal.record_replacement(index)
                validate_sources(self.bundle.sources)
            self._cleanup_stages(owned_stages, consumed_stages)
            self._validate_all_after_images()
            validate_sources(self.bundle.sources)
        except Exception as publish_error:
            try:
                self._rollback(
                    owned_replacements,
                    owned_stages=owned_stages,
                    consumed_stages=consumed_stages,
                    stage_consumption_attempts=stage_consumption_attempts,
                )
                self._cleanup_stages(owned_stages, consumed_stages)
                self.tree.remove_created_directories()
            except Exception as rollback_error:
                raise AuthoringError(
                    "authoring publication failed and rollback could not be proven"
                ) from rollback_error
            self.journal.finish()
            raise publish_error
        self.journal.finish()

    def _create_destination_directories(self) -> None:
        parents = sorted(
            {image.resolved_path.parent for image in self.bundle.after_images},
            key=lambda path: (len(path.parts), str(path)),
        )
        for parent in parents:
            self.tree.ensure_directory(parent)

    def _stage_after_images(
        self,
        staged: dict[Path, _StagedFile],
        owned_stages: dict[Path, _StageOwnership | None],
    ) -> None:
        for index, image in enumerate(self.bundle.after_images):
            content = image.content
            mode = image.mode
            if content is None or mode is None:
                raise AuthoringError("authoring after-image is incomplete")
            path = self.reservations[index].publication
            staged[image.resolved_path] = self._stage_file(
                image,
                path,
                content,
                mode,
                owned_stages=owned_stages,
            )

    def _stage_file(
        self,
        image: DestinationImage,
        path: Path,
        content: bytes,
        mode: int,
        *,
        owned_stages: dict[Path, _StageOwnership | None],
    ) -> _StagedFile:
        parent_descriptor, leaf = self.tree.open_parent(path)
        try:
            descriptor = os.open(
                leaf,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
            owned_stages[path] = None
            try:
                initial = os.fstat(descriptor)
                if not stat.S_ISREG(initial.st_mode):
                    raise AuthoringError("authoring stage is not a regular file")
                owned_stages[path] = _StageOwnership(
                    path, initial.st_dev, initial.st_ino
                )
                os.fchmod(descriptor, mode)
                _write_all(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            stage_image = DestinationImage(
                image.role,
                path,
                content,
                image.sha256,
                mode,
                None,
                image.ancestors,
            )
            identity = capture_after_identity_at(parent_descriptor, stage_image)
            return _StagedFile(path, identity)
        finally:
            os.close(parent_descriptor)

    def _publish_one(
        self,
        before: DestinationImage,
        after: DestinationImage,
        staged: _StagedFile,
        reservation: ReservedStagePaths,
        owned_replacements: list[_ReplacementOwnership],
        consumed_stages: set[Path],
        stage_consumption_attempts: set[Path],
    ) -> None:
        destination = after.resolved_path
        parent_descriptor, destination_leaf = self.tree.open_parent(destination)
        try:
            validate_destination_before_at(parent_descriptor, before)
            published_identity = PublishedIdentity(
                destination,
                staged.identity.device,
                staged.identity.inode,
                staged.identity.mode,
                staged.identity.size,
                staged.identity.sha256,
            )
            owned_replacements.append(
                _ReplacementOwnership(
                    before,
                    after,
                    published_identity,
                    reservation,
                )
            )
            if before.content is None:
                try:
                    os.link(
                        staged.path.name,
                        destination_leaf,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise AuthoringError(
                        "authoring destination was created before publication"
                    ) from exc
            else:
                stage_consumption_attempts.add(staged.path)
                os.replace(
                    staged.path.name,
                    destination_leaf,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                consumed_stages.add(staged.path)
            if before.content is None:
                stage_consumption_attempts.add(staged.path)
                os.unlink(staged.path.name, dir_fd=parent_descriptor)
                consumed_stages.add(staged.path)
            _fsync_regular_at(parent_descriptor, destination_leaf)
            os.fsync(parent_descriptor)
            observed = capture_after_identity_at(parent_descriptor, after)
            if (
                observed.device != staged.identity.device
                or observed.inode != staged.identity.inode
            ):
                raise AuthoringError("published destination identity is unexpected")
        finally:
            os.close(parent_descriptor)

    def _rollback(
        self,
        owned_replacements: list[_ReplacementOwnership],
        *,
        owned_stages: dict[Path, _StageOwnership | None],
        consumed_stages: set[Path],
        stage_consumption_attempts: set[Path],
    ) -> None:
        ownership_error: AuthoringError | None = None
        for replacement in reversed(owned_replacements):
            destination = replacement.after.resolved_path
            parent_descriptor, destination_leaf = self.tree.open_parent(destination)
            try:
                try:
                    state = classify_destination_ownership_at(
                        parent_descriptor,
                        replacement.before,
                        replacement.identity,
                    )
                except AuthoringError as exc:
                    if ownership_error is None:
                        ownership_error = exc
                    continue
                if state == "before":
                    continue
                if replacement.reservation.publication in stage_consumption_attempts:
                    consumed_stages.add(replacement.reservation.publication)
                if replacement.before.content is None:
                    validate_after_identity_at(parent_descriptor, replacement.identity)
                    os.unlink(destination_leaf, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
                    continue
                before = replacement.before
                if before.mode is None or before.content is None:
                    raise AuthoringError("authoring before-image is incomplete")
                restoration = self._stage_file(
                    before,
                    replacement.reservation.restoration,
                    before.content,
                    before.mode,
                    owned_stages=owned_stages,
                )
                validate_after_identity_at(parent_descriptor, replacement.identity)
                os.replace(
                    restoration.path.name,
                    destination_leaf,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                consumed_stages.add(restoration.path)
                _fsync_regular_at(parent_descriptor, destination_leaf)
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        if ownership_error is not None:
            raise AuthoringError(
                "authoring rollback found an ambiguous destination"
            ) from ownership_error

    def _cleanup_stages(
        self,
        owned_stages: dict[Path, _StageOwnership | None],
        consumed_stages: set[Path],
    ) -> None:
        for path, ownership in owned_stages.items():
            parent_descriptor, leaf = self.tree.open_parent(path)
            try:
                try:
                    info = os.stat(
                        leaf, dir_fd=parent_descriptor, follow_symlinks=False
                    )
                except FileNotFoundError:
                    if path not in consumed_stages:
                        raise AuthoringError(
                            "transaction stage disappeared before cleanup"
                        )
                    continue
                if (
                    ownership is None
                    or not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino)
                    != (ownership.device, ownership.inode)
                ):
                    raise AuthoringError("transaction stage ownership changed")
                os.unlink(leaf, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
                consumed_stages.add(path)
            finally:
                os.close(parent_descriptor)

    def _validate_all_after_images(self) -> None:
        for image in self.bundle.after_images:
            parent_descriptor, _leaf = self.tree.open_parent(image.resolved_path)
            try:
                capture_after_identity_at(parent_descriptor, image)
            finally:
                os.close(parent_descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while staging authoring output")
        view = view[written:]


def _fsync_regular_at(directory_descriptor: int, leaf: str) -> None:
    descriptor = os.open(
        leaf,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_descriptor,
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise AuthoringError("authoring destination is not a regular file")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
