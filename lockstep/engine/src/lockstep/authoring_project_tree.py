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

    def ensure_target_parents(self) -> None:
        """Create every planned parent in stable shallow-first order."""

        for parent in self._target_parents:
            self.ensure_directory(parent)

    def ensure_directory(
        self,
        directory: Path,
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
    ) -> int:
        # Record ambiguity before mkdir; enroll an inode only after proving the
        # open and named directories are the same empty directory.
        self.created_directories[child] = None
        os.mkdir(leaf, mode=0o755, dir_fd=parent_descriptor)
        descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        try:
            info = self._verify_directory_descriptor(descriptor, expected=None)
            identity = PathIdentity(child, info.st_dev, info.st_ino)
            proof = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
            try:
                try:
                    self._verify_directory_descriptor(proof, expected=identity)
                except AuthoringError as exc:
                    raise AuthoringError(
                        "created authoring directory ownership changed"
                    ) from exc
                if os.listdir(descriptor) or os.listdir(proof):
                    raise AuthoringError(
                        "created authoring directory contains foreign entries"
                    )
            finally:
                os.close(proof)
            self.created_directories[child] = identity
            os.fsync(parent_descriptor)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

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
