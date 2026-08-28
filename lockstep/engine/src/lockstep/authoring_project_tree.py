"""Descriptor-relative project namespace access for authoring publication."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from lockstep.authoring_bundle import (
    PathIdentity,
    ProjectCompilationBundle,
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
        return tree

    def ensure_directory(self, directory: Path) -> None:
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
                try:
                    next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except FileNotFoundError:
                    if expected is not None:
                        raise AuthoringError("recorded destination ancestor disappeared")
                    # Register the ambiguous namespace edge before mkdir itself;
                    # any failure from here retains the active journal.
                    self.created_directories[child] = None
                    os.mkdir(part, mode=0o755, dir_fd=descriptor)
                    next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    try:
                        info = self._verify_directory_descriptor(
                            next_descriptor, expected=None
                        )
                        self.created_directories[child] = PathIdentity(
                            child, info.st_dev, info.st_ino
                        )
                        os.fsync(descriptor)
                    except Exception:
                        os.close(next_descriptor)
                        raise
                else:
                    if expected is None:
                        os.close(next_descriptor)
                        raise AuthoringError(
                            "destination ancestor was created after planning"
                        )
                    try:
                        self._verify_directory_descriptor(
                            next_descriptor, expected=expected
                        )
                    except Exception:
                        os.close(next_descriptor)
                        raise
                os.close(descriptor)
                descriptor = next_descriptor
                current = child
        finally:
            os.close(descriptor)

    def open_parent(self, destination: Path) -> tuple[int, str]:
        parent = self._contained_parent(destination)
        return self.open_directory(parent), destination.name

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
                except Exception:
                    os.close(next_descriptor)
                    raise
                os.close(descriptor)
                descriptor = next_descriptor
                current = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

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
