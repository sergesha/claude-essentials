"""Fail-closed boundary for whole-DAG authoring publication."""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from lockstep.authoring_bundle import (
    DestinationImage,
    PathIdentity,
    ProjectCompilationBundle,
)
from lockstep.authoring_identity import (
    capture_after_identity_at,
    validate_bundle_preconditions,
    validate_destination_before_at,
    validate_sources,
    validate_temporary_descriptor as _validate_temporary_descriptor,
)
from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_project_tree import AuthoringProjectTree
from lockstep.errors import AuthoringError
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.owner_state import take_bounded, verify_owner_directory

__all__ = ["AuthoringPublisher", "observe_authoring_project"]


Observation = TypeVar("Observation")
_MAX_LEGACY_TRANSACTION_BYTES = 16 * 1024 * 1024
_MAX_AUTHORING_NAMESPACES = 256


class LegacyAuthoringTransaction(AuthoringError):
    """Retained transaction evidence needs the pre-simplification recovery path."""


def _publish_per_file(bundle: ProjectCompilationBundle) -> None:
    """Publish one preplanned bundle with per-target atomic namespace changes."""

    _preflight_bundle(bundle)
    tree = AuthoringProjectTree(bundle)
    tree.ensure_target_parents()
    for before, after in zip(
        bundle.before_images,
        bundle.after_images,
        strict=True,
    ):
        validate_sources(bundle.sources)
        _publish_target(tree, before, after)
    validate_sources(bundle.sources)
    _validate_all_after_images(tree, bundle.after_images)


def _preflight_bundle(bundle: ProjectCompilationBundle) -> None:
    """Complete every fallible plan/currentness check before first mutation."""

    validate_bundle_preconditions(bundle)


def _publish_target(
    tree: AuthoringProjectTree,
    before: DestinationImage,
    after: DestinationImage,
) -> None:
    parent_descriptor, destination_leaf = tree.open_parent(after.resolved_path)
    temporary_leaf = f".lockstep-authoring-{secrets.token_hex(16)}.tmp"
    owned: tuple[int, int] | None = None
    try:
        descriptor, owned = _create_temporary(parent_descriptor, temporary_leaf)
        try:
            _write_temporary(descriptor, after)
        finally:
            os.close(descriptor)
        _prove_owned_temporary(parent_descriptor, temporary_leaf, owned, after)
        validate_destination_before_at(parent_descriptor, before)
        _publish_owned_temporary(
            parent_descriptor,
            temporary_leaf,
            destination_leaf,
            before,
        )
        _fsync_regular_at(parent_descriptor, destination_leaf)
        os.fsync(parent_descriptor)
        capture_after_identity_at(parent_descriptor, after)
    finally:
        if owned is not None:
            _cleanup_owned_temporary(parent_descriptor, temporary_leaf, owned)
        os.close(parent_descriptor)


def _create_temporary(
    parent_descriptor: int,
    temporary_leaf: str,
) -> tuple[int, tuple[int, int]]:
    try:
        descriptor = os.open(
            temporary_leaf,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
    except FileExistsError as exc:
        raise AuthoringError("authoring temporary already exists") from exc
    owned: tuple[int, int] | None = None
    try:
        info = os.fstat(descriptor)
        owned = info.st_dev, info.st_ino
        if not stat.S_ISREG(info.st_mode):
            raise AuthoringError("authoring temporary is not a regular file")
        return descriptor, owned
    except Exception:
        try:
            os.close(descriptor)
        finally:
            if owned is not None:
                _cleanup_owned_temporary(
                    parent_descriptor, temporary_leaf, owned
                )
        raise


def _write_temporary(descriptor: int, after: DestinationImage) -> None:
    content, mode = after.content, after.mode
    if content is None or mode is None:
        raise AuthoringError("authoring after-image is incomplete")
    _write_all(descriptor, content)
    os.fchmod(descriptor, mode)
    os.fsync(descriptor)


def _prove_owned_temporary(
    parent_descriptor: int,
    temporary_leaf: str,
    owned: tuple[int, int],
    after: DestinationImage,
) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        proof = os.open(temporary_leaf, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise AuthoringError("authoring temporary ownership changed") from exc
    try:
        info = os.fstat(proof)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != owned:
            raise AuthoringError("authoring temporary ownership changed")
        _validate_temporary_descriptor(proof, after)
    finally:
        os.close(proof)


def _publish_owned_temporary(
    parent_descriptor: int,
    temporary_leaf: str,
    destination_leaf: str,
    before: DestinationImage,
) -> None:
    if before.content is None:
        try:
            os.link(
                temporary_leaf,
                destination_leaf,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise AuthoringError(
                "authoring destination was created before publication"
            ) from exc
        os.unlink(temporary_leaf, dir_fd=parent_descriptor)
        return
    os.replace(
        temporary_leaf,
        destination_leaf,
        src_dir_fd=parent_descriptor,
        dst_dir_fd=parent_descriptor,
    )


def _cleanup_owned_temporary(
    parent_descriptor: int,
    temporary_leaf: str,
    owned: tuple[int, int],
) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(temporary_leaf, flags, dir_fd=parent_descriptor)
    except OSError:
        return
    try:
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == owned:
        os.unlink(temporary_leaf, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)


def _validate_all_after_images(
    tree: AuthoringProjectTree,
    after_images: tuple[DestinationImage, ...],
) -> None:
    for after in after_images:
        parent_descriptor, _leaf = tree.open_parent(after.resolved_path)
        try:
            capture_after_identity_at(parent_descriptor, after)
        finally:
            os.close(parent_descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while writing authoring output")
        view = view[written:]


def _fsync_regular_at(directory_descriptor: int, leaf: str) -> None:
    descriptor = os.open(
        leaf,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_descriptor,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AuthoringError("authoring destination is not a regular file")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _ExistingAuthoringBoundary:
    """The already-created authoring namespace that readers may lock."""

    __slots__ = ("_journal", "_project_identity")

    def __init__(
        self, journal: AuthoringJournal, project_identity: PathIdentity
    ) -> None:
        self._journal = journal
        self._project_identity = project_identity

    def observe(self, operation: Callable[[], Observation]) -> Observation:
        with self._journal.locked_existing():
            _require_no_legacy_transaction(
                self._journal.directory,
                self._project_identity.resolved_path,
            )
            return operation()


def _require_no_legacy_transaction(namespace: Path, project: Path) -> None:
    """Refuse retained evidence by presence without decoding or mutating it."""

    transaction = namespace / "transaction.json"
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(transaction, flags)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _legacy_transaction_error(transaction, project, namespace) from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise _legacy_transaction_error(transaction, project, namespace)
        size_note = (
            " The evidence also exceeds the legacy byte bound."
            if info.st_size > _MAX_LEGACY_TRANSACTION_BYTES
            else ""
        )
    finally:
        os.close(descriptor)
    raise _legacy_transaction_error(transaction, project, namespace, size_note)


def _legacy_transaction_error(
    transaction: Path,
    project: Path,
    namespace: Path,
    size_note: str = "",
) -> LegacyAuthoringTransaction:
    return LegacyAuthoringTransaction(
        "legacy authoring transaction evidence is present at "
        f"{transaction}; it may be a v4 transaction. Use a pre-simplification "
        f"Lockstep build against project {project} and state directory "
        f"{namespace.parent.parent}, complete recovery there, and retry. "
        "Do not delete transaction.json manually."
        + size_note
    )


def _locate_existing_boundary(
    state_dir: Path, project: Path
) -> _ExistingAuthoringBoundary | None:
    """Find a reader-safe boundary without creating owner state."""

    journal, project_identity = AuthoringJournal.locate_ready_for_project(
        state_dir, project
    )
    if journal is None:
        return None
    return _ExistingAuthoringBoundary(journal, project_identity)


def _refuse_retained_legacy_transactions(
    state_dir: Path,
    project: Path,
) -> None:
    """Refuse evidence retained under an older inode binding for this state root."""

    AuthoringJournal.locate_for_project(state_dir, project)
    authoring = state_dir / "authoring"
    if not authoring.exists() and not authoring.is_symlink():
        return
    verify_owner_directory(authoring)
    namespaces = tuple(
        sorted(
            take_bounded(
                authoring.iterdir(),
                _MAX_AUTHORING_NAMESPACES,
                "authoring namespaces",
            ),
            key=lambda path: path.name,
        )
    )
    for namespace in namespaces:
        if (
            len(namespace.name) != 64
            or any(character not in "0123456789abcdef" for character in namespace.name)
        ):
            raise AuthoringError("authoring namespace name is invalid")
        verify_owner_directory(namespace)
        journal = AuthoringJournal(namespace)
        try:
            with journal.locked_existing():
                _require_no_legacy_transaction(namespace, project)
        except FileNotFoundError as exc:
            raise AuthoringError(
                "authoring boundary initialization is incomplete"
            ) from exc


def _observe_existing_authoring_project(
    journal: AuthoringJournal,
    project_identity: PathIdentity,
    operation: Callable[[], Observation],
) -> Observation:
    return _ExistingAuthoringBoundary(journal, project_identity).observe(operation)


def observe_authoring_project(
    state_dir: Path,
    project: Path,
    operation: Callable[[], Observation],
) -> Observation:
    _refuse_retained_legacy_transactions(state_dir, project)
    boundary = _locate_existing_boundary(state_dir, project)
    if boundary is not None:
        return boundary.observe(operation)
    try:
        optimistic = operation()
    except (LockstepError, OSError, ValueError):
        boundary = _locate_existing_boundary(state_dir, project)
        if boundary is None:
            raise
        return boundary.observe(operation)
    boundary = _locate_existing_boundary(state_dir, project)
    return optimistic if boundary is None else boundary.observe(operation)


class AuthoringPublisher:
    """Own future authoring publication without ambient state-directory lookup."""

    __slots__ = ("_state_dir",)

    def __init__(self, state_dir: Path) -> None:
        if not isinstance(state_dir, Path):
            raise TypeError("authoring state directory must be a Path")
        if not state_dir.is_absolute() or any(
            part in {".", ".."} for part in state_dir.parts
        ):
            raise ValueError(
                "authoring state directory must be absolute and lexically canonical"
            )
        self._state_dir = state_dir

    def publish(self, bundle: ProjectCompilationBundle) -> None:
        validate_bundle_preconditions(bundle)
        _refuse_retained_legacy_transactions(
            self._state_dir,
            bundle.resolved_project,
        )
        journal = AuthoringJournal.create_for_bundle(self._state_dir, bundle)
        with journal.locked():
            _require_no_legacy_transaction(
                journal.directory,
                bundle.resolved_project,
            )
            _publish_per_file(bundle)

    def require_ready(self, project: Path) -> None:
        if not isinstance(project, Path):
            raise TypeError("authoring project must be a Path")
        _refuse_retained_legacy_transactions(self._state_dir, project)
        journal, identity = AuthoringJournal.create_for_project(
            self._state_dir,
            project,
        )
        with journal.locked():
            _require_no_legacy_transaction(
                journal.directory,
                identity.resolved_path,
            )

    def observe(
        self, project: Path, operation: Callable[[], Observation]
    ) -> Observation:
        if not isinstance(project, Path):
            raise TypeError("authoring project must be a Path")
        return observe_authoring_project(self._state_dir, project, operation)
