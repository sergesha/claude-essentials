"""Fail-closed locking and per-file publication for one authoring plan."""

from __future__ import annotations

import hashlib, json, os, secrets, stat
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TypeVar

from lockstep.authoring_bundle import AuthoringPlan, DirectoryIdentity, PlannedTarget
from lockstep.authoring_capture import (
    _DescriptorObservationError, _RegularFileObservation, _observe_regular_descriptor,
    _validate_authoring_contents, validate_sources, validate_target, validate_target_at,
)
from lockstep.authoring_project_tree import AuthoringProjectTree
from lockstep.errors import AuthoringError
from lockstep.runtime.advisory_lock import advisory_file_lock
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.owner_state import (ensure_owner_directory, fsync_owner_directory,
    initialize_owner_state, verify_owner_directory, verify_owner_file)

__all__ = ["AuthoringPublisher", "observe_authoring_project"]
Observation = TypeVar("Observation")
_MAX_LEGACY_TRANSACTION_BYTES = 16 * 1024 * 1024
_READ_FLAGS = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
               | getattr(os, "O_NOFOLLOW", 0))


class LegacyAuthoringEvidence(AuthoringError): pass
def _preflight_plan(plan: AuthoringPlan) -> AuthoringProjectTree:
    if not isinstance(plan, AuthoringPlan):
        raise TypeError("authoring publication requires an AuthoringPlan")
    for label, contents in (
        ("authoring read set", (item.content for item in plan.sources)),
        ("authoring before images", (item.before for item in plan.targets)),
        ("authoring after images", (item.after for item in plan.targets)),
    ):
        _validate_authoring_contents(label, contents)
    tree = AuthoringProjectTree(plan)
    tree.preflight()
    validate_sources(plan.sources)
    for target in plan.targets:
        validate_target(target)
    return tree
def _publish_per_file(plan: AuthoringPlan) -> None:
    tree = _preflight_plan(plan)
    for target in sorted(plan.targets,
            key=lambda item: (len(item.path.parent.parts), str(item.path.parent))):
        descriptor, _leaf = tree.ensure_parent(target)
        os.close(descriptor)
    for target in plan.targets:
        validate_sources(plan.sources)
        _publish_target(tree, target)
    validate_sources(plan.sources)
    for target in plan.targets:
        descriptor, _leaf = tree.open_parent(target)
        try: capture_after_identity_at(descriptor, target)
        finally: os.close(descriptor)
def _publish_target(tree: AuthoringProjectTree, target: PlannedTarget) -> None:
    parent, leaf = tree.open_parent(target)
    temporary = f".lockstep-authoring-{secrets.token_hex(16)}.tmp"
    descriptor: int | None = None
    owned: tuple[int, int] | None = None
    try:
        descriptor, owned = _create_temporary(parent, temporary)
        _write_temporary(descriptor, target)
        _prove_owned_temporary(parent, temporary, owned, target)
        validate_target_at(parent, target)
        _publish_owned_temporary(parent, temporary, leaf, target)
        _fsync_regular_at(parent, leaf)
        os.fsync(parent)
        capture_after_identity_at(parent, target)
    finally:
        try:
            if owned is not None:
                _cleanup_owned_temporary(parent, temporary, owned)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)
def _create_temporary(parent: int, leaf: str) -> tuple[int, tuple[int, int]]:
    try:
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(leaf, flags, 0o600, dir_fd=parent)
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
            if owned is not None:
                _cleanup_owned_temporary(parent, leaf, owned)
        finally:
            os.close(descriptor)
        raise
def _write_temporary(descriptor: int, target: PlannedTarget) -> None:
    _write_all(descriptor, target.after)
    os.fchmod(descriptor, target.mode)
    os.fsync(descriptor)
def _validate_after(observed: _RegularFileObservation, target: PlannedTarget, message: str) -> None:
    if (observed.content != target.after
            or hashlib.sha256(observed.content).hexdigest() != target.after_sha256
            or stat.S_IMODE(observed.info.st_mode) != target.mode):
        raise AuthoringError(message)
def _validate_temporary_descriptor(descriptor: int, target: PlannedTarget) -> None:
    try:
        observed = _observe_regular_descriptor(os.dup(descriptor),
            max_bytes=len(target.after), expected_size=len(target.after))
    except _DescriptorObservationError as exc:
        raise AuthoringError("authoring temporary does not match its after-image") from exc
    _validate_after(observed, target, "authoring temporary does not match its after-image")
def _prove_owned_temporary(
    parent: int, leaf: str, owned: tuple[int, int], target: PlannedTarget
) -> None:
    try:
        descriptor = os.open(leaf, _READ_FLAGS, dir_fd=parent)
    except OSError as exc:
        raise AuthoringError("authoring temporary ownership changed") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != owned:
            raise AuthoringError("authoring temporary ownership changed")
        _validate_temporary_descriptor(descriptor, target)
    finally:
        os.close(descriptor)
def _publish_owned_temporary(
    parent: int, temporary: str, destination: str, target: PlannedTarget
) -> None:
    if target.before is None:
        try:
            os.link(temporary, destination, src_dir_fd=parent, dst_dir_fd=parent,
                    follow_symlinks=False)
        except FileExistsError as exc:
            raise AuthoringError("authoring destination was created before publication") from exc
        os.unlink(temporary, dir_fd=parent)
    else:
        os.replace(temporary, destination, src_dir_fd=parent, dst_dir_fd=parent)
def _cleanup_owned_temporary(parent: int, leaf: str, owned: tuple[int, int]) -> None:
    try:
        descriptor = os.open(leaf, _READ_FLAGS, dir_fd=parent)
    except OSError:
        return
    try:
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == owned:
        os.unlink(leaf, dir_fd=parent)
        os.fsync(parent)
def capture_after_identity_at(parent: int, target: PlannedTarget) -> None:
    try:
        descriptor = os.open(target.path.name, _READ_FLAGS, dir_fd=parent)
    except OSError as exc:
        raise AuthoringError(f"authoring file is unavailable: {target.path}") from exc
    try:
        observed = _observe_regular_descriptor(
            descriptor, max_bytes=len(target.after), expected_size=len(target.after))
    except _DescriptorObservationError as exc:
        raise AuthoringError("published destination does not match its after-image") from exc
    _validate_after(observed, target, "published destination does not match its after-image")
def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while writing authoring output")
        view = view[written:]
def _fsync_regular_at(parent: int, leaf: str) -> None:
    descriptor = os.open(leaf, _READ_FLAGS, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AuthoringError("authoring destination is not a regular file")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
def _validate_owner_state_location(state_dir: Path, project: Path) -> None:
    if not state_dir.is_absolute() or any(part in {".", ".."} for part in state_dir.parts):
        raise ValueError("authoring state directory must be absolute and canonical")
    lexical, resolved = Path(os.path.abspath(state_dir)), state_dir.resolve(strict=False)
    if any(left == right or left in right.parents or right in left.parents
           for left, right in ((lexical, project), (resolved, project))):
        raise ValueError("authoring state directory must be outside the project")
def _current_project_identity(project: Path) -> DirectoryIdentity:
    try:
        supplied = project.lstat()
        if stat.S_ISLNK(supplied.st_mode) or not stat.S_ISDIR(supplied.st_mode):
            raise AuthoringError("authoring recovery project must be a real directory")
        resolved = project.resolve(strict=True)
        info = resolved.lstat()
    except (OSError, RuntimeError) as exc:
        raise AuthoringError("authoring recovery project is unavailable") from exc
    facts = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_ctime_ns)
    if not stat.S_ISDIR(info.st_mode) or facts(supplied) != facts(info):
        raise AuthoringError("authoring recovery project identity changed")
    return DirectoryIdentity(resolved, info.st_dev, info.st_ino)
def _project_namespace_for_identity(identity: DirectoryIdentity) -> str:
    encoded = json.dumps({"path": str(identity.path), "device": identity.device,
        "inode": identity.inode}, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()
def _create_authoring_namespace_for_identity(state_dir: Path,
                                              identity: DirectoryIdentity) -> Path:
    _validate_owner_state_location(state_dir, identity.path)
    root = initialize_owner_state(state_dir)
    return ensure_owner_directory(ensure_owner_directory(root, "authoring"),
                                  _project_namespace_for_identity(identity))
def _create_authoring_namespace_for_project(state_dir: Path,
        project: Path) -> tuple[Path, DirectoryIdentity]:
    identity = _current_project_identity(project)
    return _create_authoring_namespace_for_identity(state_dir, identity), identity
def _locate_authoring_namespace(state_dir: Path,
        project: Path) -> tuple[Path | None, DirectoryIdentity]:
    identity = _current_project_identity(project)
    _validate_owner_state_location(state_dir, identity.path)
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
@contextmanager
def _locked_authoring_namespace(namespace: Path, *, create: bool) -> Iterator[None]:
    lock = namespace / "transaction.lock"
    existed = lock.exists() or lock.is_symlink()
    with advisory_file_lock(lock, create=create):
        verify_owner_file(lock)
        if create and not existed:
            fsync_owner_directory(namespace)
        yield
def _ready_boundary(state_dir: Path,
        project: Path) -> tuple[Path | None, DirectoryIdentity]:
    namespace, identity = _locate_authoring_namespace(state_dir, project)
    if namespace is not None:
        lock = namespace / "transaction.lock"
        if not lock.exists() and not lock.is_symlink():
            raise AuthoringError("authoring boundary initialization is incomplete")
        verify_owner_file(lock)
    return namespace, identity
class _ExistingAuthoringBoundary:
    __slots__ = ("_namespace", "_project_identity")
    def __init__(self, namespace: Path, identity: DirectoryIdentity) -> None:
        self._namespace, self._project_identity = namespace, identity
    def observe(self, operation: Callable[[], Observation]) -> Observation:
        with _locked_authoring_namespace(self._namespace, create=False):
            _require_no_legacy_transaction(self._namespace, self._project_identity.path)
            return operation()
def _legacy_error(transaction: Path, project: Path, namespace: Path,
                  note: str = "") -> LegacyAuthoringEvidence:
    return LegacyAuthoringEvidence(
        "legacy authoring transaction evidence is present at "
        f"{transaction}; it may be a v4 transaction. Use a pre-simplification "
        f"Lockstep build against project {project} and state directory "
        f"{namespace.parent.parent}, complete recovery there, and retry. "
        "Do not delete transaction.json manually." + note
    )
def _require_no_legacy_transaction(namespace: Path, project: Path) -> None:
    note = _legacy_evidence_note(namespace)
    if note is None:
        return
    raise _legacy_error(namespace / "transaction.json", project, namespace, note)
def _legacy_evidence_note(namespace: Path) -> str | None:
    transaction = namespace / "transaction.json"
    try:
        descriptor = os.open(transaction, _READ_FLAGS)
    except FileNotFoundError:
        return None
    except OSError:
        return ""
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return ""
        return (" The evidence also exceeds the legacy byte bound."
                if info.st_size > _MAX_LEGACY_TRANSACTION_BYTES else "")
    finally:
        os.close(descriptor)
def _locate_existing_boundary(state_dir: Path,
        project: Path) -> _ExistingAuthoringBoundary | None:
    namespace, identity = _ready_boundary(state_dir, project)
    return None if namespace is None else _ExistingAuthoringBoundary(namespace, identity)
def observe_authoring_project(state_dir: Path, project: Path,
                              operation: Callable[[], Observation]) -> Observation:
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
    __slots__ = ("_state_dir",)
    def __init__(self, state_dir: Path) -> None:
        if not isinstance(state_dir, Path):
            raise TypeError("authoring state directory must be a Path")
        if not state_dir.is_absolute() or any(part in {".", ".."} for part in state_dir.parts):
            raise ValueError("authoring state directory must be absolute and lexically canonical")
        self._state_dir = state_dir

    def publish(self, plan: AuthoringPlan) -> None:
        _preflight_plan(plan)
        namespace = _create_authoring_namespace_for_identity(self._state_dir, plan.project_identity)
        with _locked_authoring_namespace(namespace, create=True):
            _require_no_legacy_transaction(namespace, plan.project)
            _publish_per_file(plan)

    def require_ready(self, project: Path) -> None:
        if not isinstance(project, Path):
            raise TypeError("authoring project must be a Path")
        namespace, identity = _create_authoring_namespace_for_project(self._state_dir, project)
        with _locked_authoring_namespace(namespace, create=True):
            _require_no_legacy_transaction(namespace, identity.path)

    def observe(self, project: Path, operation: Callable[[], Observation]) -> Observation:
        if not isinstance(project, Path):
            raise TypeError("authoring project must be a Path")
        return observe_authoring_project(self._state_dir, project, operation)
