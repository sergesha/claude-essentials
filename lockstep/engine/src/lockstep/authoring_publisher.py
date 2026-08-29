"""Fail-closed boundary for whole-DAG authoring publication."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TypeVar

from lockstep.authoring_bundle import (
    DestinationImage,
    PathIdentity,
    ProjectCompilationBundle,
)
from lockstep.authoring_capture import (
    _DescriptorObservationError,
    _observe_regular_descriptor,
)
from lockstep.authoring_identity import (
    validate_bundle_preconditions,
    validate_destination_before_at,
    validate_sources,
)
from lockstep.authoring_project_tree import AuthoringProjectTree
from lockstep.errors import AuthoringError
from lockstep.runtime.advisory_lock import advisory_file_lock
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.owner_state import (
    ensure_owner_directory,
    fsync_owner_directory,
    initialize_owner_state,
    take_bounded,
    verify_owner_directory,
    verify_owner_file,
)

__all__ = ["AuthoringPublisher", "observe_authoring_project"]


Observation = TypeVar("Observation")
_MAX_LEGACY_TRANSACTION_BYTES = 16 * 1024 * 1024
_MAX_AUTHORING_NAMESPACES = 256


class LegacyAuthoringEvidence(AuthoringError):
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


def _validate_temporary_descriptor(
    descriptor: int, after: DestinationImage
) -> None:
    """Prove the still-open temporary has the exact bounded after-image."""

    content, mode = after.content, after.mode
    if content is None or mode is None:
        raise AuthoringError("authoring after-image is incomplete")
    try:
        observed = _observe_regular_descriptor(
            os.dup(descriptor),
            max_bytes=len(content),
            expected_size=len(content),
        )
    except _DescriptorObservationError as exc:
        raise AuthoringError(
            "authoring temporary does not match its after-image"
        ) from exc
    if (
        observed.content != content
        or hashlib.sha256(observed.content).hexdigest() != after.sha256
        or stat.S_IMODE(observed.info.st_mode) != mode
    ):
        raise AuthoringError("authoring temporary does not match its after-image")


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


def capture_after_identity_at(
    directory_descriptor: int, image: DestinationImage
) -> None:
    """Validate one published leaf through its verified parent descriptor."""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            image.resolved_path.name,
            flags,
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise AuthoringError(
            f"authoring file is unavailable: {image.resolved_path}"
        ) from exc
    expected_size = len(image.content or b"")
    try:
        observed = _observe_regular_descriptor(
            descriptor,
            max_bytes=expected_size,
            expected_size=expected_size,
        )
    except _DescriptorObservationError as exc:
        raise AuthoringError(
            "published destination does not match its after-image"
        ) from exc
    if (
        observed.content != image.content
        or hashlib.sha256(observed.content).hexdigest() != image.sha256
        or stat.S_IMODE(observed.info.st_mode) != image.mode
    ):
        raise AuthoringError("published destination does not match its after-image")


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


def _validate_owner_state_location(state_dir: Path, project: Path) -> None:
    if not state_dir.is_absolute() or any(part in {".", ".."} for part in state_dir.parts):
        raise ValueError("authoring state directory must be absolute and canonical")
    lexical = Path(os.path.abspath(state_dir))
    resolved = state_dir.resolve(strict=False)
    if (
        lexical == project
        or project in lexical.parents
        or lexical in project.parents
        or resolved == project
        or project in resolved.parents
        or resolved in project.parents
    ):
        raise ValueError("authoring state directory must be outside the project")


def _current_project_identity(project: Path) -> PathIdentity:
    try:
        supplied = project.lstat()
        if stat.S_ISLNK(supplied.st_mode) or not stat.S_ISDIR(supplied.st_mode):
            raise AuthoringError("authoring recovery project must be a real directory")
        resolved = project.resolve(strict=True)
        info = resolved.lstat()
    except (OSError, RuntimeError) as exc:
        raise AuthoringError("authoring recovery project is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or _project_stability_facts(
        supplied
    ) != _project_stability_facts(info):
        raise AuthoringError("authoring recovery project identity changed")
    return PathIdentity(resolved, info.st_dev, info.st_ino)


def _project_stability_facts(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_ctime_ns


def _project_namespace_for_identity(identity: PathIdentity) -> str:
    encoded = json.dumps(
        {
            "path": str(identity.resolved_path),
            "device": identity.device,
            "inode": identity.inode,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _create_authoring_namespace_for_identity(
    state_dir: Path, identity: PathIdentity
) -> Path:
    _validate_owner_state_location(state_dir, identity.resolved_path)
    root = initialize_owner_state(state_dir)
    authoring = ensure_owner_directory(root, "authoring")
    return ensure_owner_directory(
        authoring, _project_namespace_for_identity(identity)
    )


def _create_authoring_namespace_for_project(
    state_dir: Path, project: Path
) -> tuple[Path, PathIdentity]:
    identity = _current_project_identity(project)
    return _create_authoring_namespace_for_identity(state_dir, identity), identity


def _locate_authoring_namespace(
    state_dir: Path, project: Path
) -> tuple[Path | None, PathIdentity]:
    identity = _current_project_identity(project)
    _validate_owner_state_location(state_dir, identity.resolved_path)
    if not state_dir.exists() and not state_dir.is_symlink():
        return None, identity
    verify_owner_directory(state_dir)
    authoring = state_dir / "authoring"
    if not authoring.exists() and not authoring.is_symlink():
        return None, identity
    verify_owner_directory(authoring)
    namespace = authoring / _project_namespace_for_identity(identity)
    if not namespace.exists() and not namespace.is_symlink():
        return None, identity
    verify_owner_directory(namespace)
    return namespace, identity


def _locate_ready_authoring_namespace(
    state_dir: Path, project: Path
) -> tuple[Path | None, PathIdentity]:
    namespace, identity = _locate_authoring_namespace(state_dir, project)
    if namespace is None:
        return None, identity
    lock_path = namespace / "transaction.lock"
    if not lock_path.exists() and not lock_path.is_symlink():
        raise AuthoringError("authoring boundary initialization is incomplete")
    verify_owner_file(lock_path)
    return namespace, identity


@contextmanager
def _locked_authoring_namespace(
    namespace: Path, *, create: bool
) -> Iterator[None]:
    lock_path = namespace / "transaction.lock"
    existed = lock_path.exists() or lock_path.is_symlink()
    with advisory_file_lock(lock_path, create=create):
        verify_owner_file(lock_path)
        if create and not existed:
            fsync_owner_directory(namespace)
        yield


class _ExistingAuthoringBoundary:
    """The already-created authoring namespace that readers may lock."""

    __slots__ = ("_namespace", "_project_identity")

    def __init__(
        self, namespace: Path, project_identity: PathIdentity
    ) -> None:
        self._namespace = namespace
        self._project_identity = project_identity

    def observe(self, operation: Callable[[], Observation]) -> Observation:
        with _locked_authoring_namespace(self._namespace, create=False):
            _require_no_legacy_transaction(
                self._namespace,
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
) -> LegacyAuthoringEvidence:
    return LegacyAuthoringEvidence(
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

    namespace, project_identity = _locate_ready_authoring_namespace(
        state_dir, project
    )
    if namespace is None:
        return None
    return _ExistingAuthoringBoundary(namespace, project_identity)


def _refuse_retained_legacy_transactions(
    state_dir: Path,
    project: Path,
) -> None:
    """Refuse evidence retained under an older inode binding for this state root."""

    _locate_authoring_namespace(state_dir, project)
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
        try:
            with _locked_authoring_namespace(namespace, create=False):
                _require_no_legacy_transaction(namespace, project)
        except FileNotFoundError as exc:
            raise AuthoringError(
                "authoring boundary initialization is incomplete"
            ) from exc


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
        namespace = _create_authoring_namespace_for_identity(
            self._state_dir, bundle.project_identity
        )
        with _locked_authoring_namespace(namespace, create=True):
            _require_no_legacy_transaction(
                namespace,
                bundle.resolved_project,
            )
            _publish_per_file(bundle)

    def require_ready(self, project: Path) -> None:
        if not isinstance(project, Path):
            raise TypeError("authoring project must be a Path")
        _refuse_retained_legacy_transactions(self._state_dir, project)
        namespace, identity = _create_authoring_namespace_for_project(
            self._state_dir,
            project,
        )
        with _locked_authoring_namespace(namespace, create=True):
            _require_no_legacy_transaction(
                namespace,
                identity.resolved_path,
            )

    def observe(
        self, project: Path, operation: Callable[[], Observation]
    ) -> Observation:
        if not isinstance(project, Path):
            raise TypeError("authoring project must be a Path")
        return observe_authoring_project(self._state_dir, project, operation)
