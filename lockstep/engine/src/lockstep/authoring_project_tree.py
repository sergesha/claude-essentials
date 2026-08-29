"""Descriptor-relative project namespace access for authoring publication."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable

from lockstep.authoring_bundle import (
    PathIdentity,
    ProjectCompilationBundle,
)
from lockstep.authoring_stage_paths import (
    ReservedStageEvidence,
    ReservedStagePaths,
)
from lockstep.errors import AuthoringError


_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)


class AuthoringProjectTree:
    """Open and mutate only identity-bound directories below one project root."""

    __slots__ = (
        "created_directories",
        "_project",
        "_project_identity",
        "_recorded",
        "_target_parents",
    )

    def __init__(self, bundle: ProjectCompilationBundle) -> None:
        self._project = bundle.resolved_project
        self._project_identity = bundle.project_identity
        self.created_directories: dict[Path, PathIdentity | None] = {}
        recorded = {bundle.resolved_project: bundle.project_identity}
        for source in bundle.sources:
            for identity in source.ancestors:
                self._record_identity(recorded, identity)
        for image in bundle.before_images:
            for identity in image.ancestors:
                self._record_identity(recorded, identity)
        self._recorded = recorded
        self._target_parents = tuple(
            sorted(
                {image.resolved_path.parent for image in bundle.after_images},
                key=lambda path: (len(path.parts), str(path)),
            )
        )

    @classmethod
    def from_identities(
        cls,
        project_identity: PathIdentity,
        ancestor_chains: tuple[tuple[PathIdentity, ...], ...],
    ) -> AuthoringProjectTree:
        tree = cls.__new__(cls)
        tree._project = project_identity.resolved_path
        tree._project_identity = project_identity
        tree.created_directories = {}
        recorded = {tree._project: project_identity}
        for ancestors in ancestor_chains:
            for identity in ancestors:
                tree._record_identity(recorded, identity)
        tree._recorded = recorded
        tree._target_parents = ()
        return tree

    def ensure_target_parents(self) -> None:
        """Create every planned parent in stable shallow-first order."""

        for parent in self._target_parents:
            self.ensure_directory(parent, _ignore_created_directory)

    def ensure_directory(
        self,
        directory: Path,
        persist_created_directory_identity: Callable[[PathIdentity], None],
    ) -> None:
        relative = self._relative_directory(directory)
        descriptor = self._open_root()
        current = self._project
        try:
            for part in relative.parts:
                child = current / part
                next_descriptor = self._ensure_child_directory(
                    descriptor,
                    child,
                    part,
                    persist_created_directory_identity,
                )
                os.close(descriptor)
                descriptor = next_descriptor
                current = child
        finally:
            os.close(descriptor)

    def _ensure_child_directory(
        self,
        parent_descriptor: int,
        child: Path,
        leaf: str,
        persist_created_directory_identity: Callable[[PathIdentity], None],
    ) -> int:
        expected = self._expected(child)
        try:
            descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        except FileNotFoundError:
            if expected is not None:
                raise AuthoringError("recorded destination ancestor disappeared")
            return self._create_child_directory(
                parent_descriptor,
                child,
                leaf,
                persist_created_directory_identity,
            )
        if expected is None:
            os.close(descriptor)
            raise AuthoringError("destination ancestor was created after planning")
        try:
            self._verify_directory_descriptor(descriptor, expected=expected)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _create_child_directory(
        self,
        parent_descriptor: int,
        child: Path,
        leaf: str,
        persist_created_directory_identity: Callable[[PathIdentity], None],
    ) -> int:
        # Enrollment precedes mkdir so any ambiguous failure retains the journal.
        self.created_directories[child] = None
        os.mkdir(leaf, mode=0o755, dir_fd=parent_descriptor)
        descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        try:
            info = self._verify_directory_descriptor(descriptor, expected=None)
            identity = PathIdentity(child, info.st_dev, info.st_ino)
            self.created_directories[child] = identity
            persist_created_directory_identity(identity)
            os.fsync(parent_descriptor)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def open_parent(self, destination: Path) -> tuple[int, str]:
        parent = self._contained_parent(destination)
        return self.open_directory(parent), destination.name

    def prove_reserved_stage_absence(
        self,
        operation_id: str,
        stages: tuple[ReservedStagePaths, ...],
    ) -> ReservedStageEvidence:
        """Prove a complete planned stage set absent without creating parents."""

        for stage in stages:
            self._require_reserved_path_absent(stage.publication)
            self._require_reserved_path_absent(stage.restoration)
        return ReservedStageEvidence(operation_id, stages)

    def open_directory(self, directory: Path) -> int:
        try:
            relative = directory.relative_to(self._project)
        except ValueError as exc:
            raise AuthoringError("authoring directory is outside the project") from exc
        descriptor = self._open_root()
        current = self._project
        try:
            for part in relative.parts:
                child = current / part
                expected = self._expected(child)
                if expected is None:
                    raise AuthoringError("authoring directory identity is unowned")
                next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                try:
                    self._verify_directory_descriptor(
                        next_descriptor, expected=expected
                    )
                except BaseException:
                    os.close(next_descriptor)
                    raise
                os.close(descriptor)
                descriptor = next_descriptor
                current = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def inspect_created_directory(
        self, directory: Path, expected: PathIdentity | None
    ) -> frozenset[str] | None:
        """Inspect one candidate and enroll only an exact journal-owned inode."""

        if expected is not None and expected.resolved_path != directory:
            raise AuthoringError("created directory identity names another path")
        parent_descriptor = self.open_directory(directory.parent)
        try:
            try:
                child_descriptor = os.open(
                    directory.name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise AuthoringError(
                    "transaction-created directory is foreign"
                ) from exc
            try:
                if expected is None:
                    raise AuthoringError(
                        "transaction-created directory ownership is ambiguous"
                    )
                self._verify_directory_descriptor(
                    child_descriptor, expected=expected
                )
                children = frozenset(os.listdir(child_descriptor))
            finally:
                os.close(child_descriptor)
        finally:
            os.close(parent_descriptor)
        self.created_directories[directory] = expected
        return children

    def durably_confirm_created_directory_absent(self, directory: Path) -> None:
        parent_descriptor = self.open_directory(directory.parent)
        try:
            try:
                os.stat(
                    directory.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.fsync(parent_descriptor)
                return
            raise AuthoringError("transaction-created directory is not absent")
        finally:
            os.close(parent_descriptor)

    def remove_created_directories(self) -> None:
        for directory in sorted(
            self.created_directories, key=lambda path: len(path.parts), reverse=True
        ):
            expected = self.created_directories[directory]
            if expected is None:
                raise AuthoringError(
                    "transaction-created directory ownership is ambiguous"
                )
            parent_descriptor = self.open_directory(directory.parent)
            try:
                child_descriptor = os.open(
                    directory.name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor
                )
                try:
                    self._verify_directory_descriptor(
                        child_descriptor, expected=expected
                    )
                finally:
                    os.close(child_descriptor)
                os.rmdir(directory.name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            except OSError as exc:
                raise AuthoringError(
                    "transaction-created directory could not be removed durably"
                ) from exc
            finally:
                os.close(parent_descriptor)

    def _require_reserved_path_absent(self, path: Path) -> None:
        parent = self._contained_parent(path)
        descriptor = self._open_reserved_parent(parent)
        if descriptor is None:
            return
        try:
            try:
                os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise AuthoringError("authoring reserved stage path is occupied")
        finally:
            os.close(descriptor)

    def _open_reserved_parent(self, parent: Path) -> int | None:
        relative = parent.relative_to(self._project)
        descriptor = self._open_root()
        current = self._project
        try:
            for part in relative.parts:
                child = current / part
                next_descriptor = self._open_reserved_child(
                    descriptor, child, part
                )
                if next_descriptor is None:
                    return None
                os.close(descriptor)
                descriptor = next_descriptor
                current = child
            result = descriptor
            descriptor = -1
            return result
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _open_reserved_child(
        self, parent_descriptor: int, child: Path, leaf: str
    ) -> int | None:
        expected = self._expected(child)
        try:
            descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        except FileNotFoundError:
            if expected is not None:
                raise AuthoringError("recorded destination ancestor disappeared")
            return None
        except OSError as exc:
            raise AuthoringError(
                "authoring reserved stage parent is unavailable"
            ) from exc
        if expected is None:
            os.close(descriptor)
            raise AuthoringError("destination ancestor was created after planning")
        try:
            self._verify_directory_descriptor(descriptor, expected=expected)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _relative_directory(self, directory: Path) -> Path:
        try:
            return directory.relative_to(self._project)
        except ValueError as exc:
            raise AuthoringError("authoring directory is outside the project") from exc

    def _open_root(self) -> int:
        descriptor = os.open(self._project, _DIRECTORY_FLAGS)
        try:
            self._verify_directory_descriptor(
                descriptor, expected=self._project_identity
            )
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _expected(self, path: Path) -> PathIdentity | None:
        if path in self.created_directories:
            return self.created_directories[path]
        return self._recorded.get(path)

    def _contained_parent(self, destination: Path) -> Path:
        try:
            destination.relative_to(self._project)
        except ValueError as exc:
            raise AuthoringError("authoring destination is outside the project") from exc
        if destination == self._project or not destination.name:
            raise AuthoringError("authoring destination is invalid")
        return destination.parent

    @staticmethod
    def _verify_directory_descriptor(
        descriptor: int, *, expected: PathIdentity | None
    ) -> os.stat_result:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise AuthoringError("authoring ancestor is not a directory")
        if expected is not None and (info.st_dev, info.st_ino) != (
            expected.device,
            expected.inode,
        ):
            raise AuthoringError("authoring ancestor identity changed")
        return info

    @staticmethod
    def _record_identity(
        recorded: dict[Path, PathIdentity], identity: PathIdentity
    ) -> None:
        existing = recorded.setdefault(identity.resolved_path, identity)
        if existing != identity:
            raise AuthoringError("authoring bundle contains conflicting identities")


def _ignore_created_directory(_identity: PathIdentity) -> None:
    """The per-file writer retains live ownership in ``created_directories``."""
