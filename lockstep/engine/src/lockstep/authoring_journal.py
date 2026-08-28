"""Trusted owner-state journal for one project-bound authoring transaction."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from lockstep.authoring_bundle import (
    DestinationImage,
    LeafIdentity,
    PathIdentity,
    ProjectCompilationBundle,
)
from lockstep.authoring_recovery_model import (
    MAX_RECOVERY_JOURNAL_BYTES,
    AuthoringRecoveryModel,
    derive_created_directory_candidates,
    parse_recovery_journal,
)
from lockstep.authoring_stage_paths import (
    ReservedStageEvidence,
    reserved_stage_set,
)
from lockstep.errors import AuthoringError
from lockstep.runtime.advisory_lock import advisory_file_lock
from lockstep.runtime.owner_state import (
    ensure_owner_directory,
    fsync_owner_directory,
    initialize_owner_state,
    seal_owner_file,
    verify_owner_directory,
    verify_owner_file,
)


class AuthoringRecoveryRequired(AuthoringError):
    """An active trusted journal needs a frozen recovery protocol."""


class AuthoringJournal:
    __slots__ = (
        "directory",
        "journal_path",
        "_created_directory_candidates",
        "_document",
    )

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.journal_path = directory / "transaction.json"
        self._created_directory_candidates: tuple[Path, ...] = ()
        self._document: dict[str, object] | None = None

    @classmethod
    def create_for_bundle(
        cls, state_dir: Path, bundle: ProjectCompilationBundle
    ) -> AuthoringJournal:
        return cls._create_for_identity(state_dir, bundle.project_identity)

    @classmethod
    def create_for_project(
        cls, state_dir: Path, project: Path
    ) -> tuple[AuthoringJournal, PathIdentity]:
        identity = _current_project_identity(project)
        return cls._create_for_identity(state_dir, identity), identity

    @classmethod
    def _create_for_identity(
        cls, state_dir: Path, identity: PathIdentity
    ) -> AuthoringJournal:
        _validate_owner_state_location(state_dir, identity.resolved_path)
        root = initialize_owner_state(state_dir)
        authoring = ensure_owner_directory(root, "authoring")
        directory = ensure_owner_directory(
            authoring, _project_namespace_for_identity(identity)
        )
        return cls(directory)

    @classmethod
    def locate_for_project(
        cls, state_dir: Path, project: Path
    ) -> tuple[AuthoringJournal | None, PathIdentity]:
        identity = _current_project_identity(project)
        _validate_owner_state_location(state_dir, identity.resolved_path)
        if not state_dir.exists() and not state_dir.is_symlink():
            return None, identity
        verify_owner_directory(state_dir)
        authoring = state_dir / "authoring"
        if not authoring.exists() and not authoring.is_symlink():
            return None, identity
        verify_owner_directory(authoring)
        directory = authoring / _project_namespace_for_identity(identity)
        if not directory.exists() and not directory.is_symlink():
            return None, identity
        verify_owner_directory(directory)
        return cls(directory), identity

    @contextmanager
    def locked(self) -> Iterator[None]:
        lock_path = self.directory / "transaction.lock"
        existed = lock_path.exists() or lock_path.is_symlink()
        with advisory_file_lock(lock_path):
            verify_owner_file(lock_path)
            if not existed:
                fsync_owner_directory(self.directory)
            yield

    def require_inactive(self) -> None:
        if self.has_active_transaction():
            raise AuthoringRecoveryRequired(
                "active authoring transaction requires recovery"
            )

    def has_active_transaction(self) -> bool:
        if not self.journal_path.exists() and not self.journal_path.is_symlink():
            return False
        verify_owner_file(self.journal_path)
        return True

    def sync_namespace(self) -> None:
        """Complete durability after a prior journal unlink cut."""

        fsync_owner_directory(self.directory)

    def read_recovery_model(
        self, *, expected_project: PathIdentity
    ) -> AuthoringRecoveryModel:
        if not self.has_active_transaction():
            raise AuthoringRecoveryRequired("authoring recovery journal disappeared")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.journal_path, flags)
        try:
            first = os.fstat(descriptor)
            if (
                not stat.S_ISREG(first.st_mode)
                or first.st_uid != os.getuid()
                or first.st_mode & 0o077
                or first.st_size > MAX_RECOVERY_JOURNAL_BYTES
            ):
                raise AuthoringError("authoring recovery journal is insecure")
            chunks: list[bytes] = []
            remaining = MAX_RECOVERY_JOURNAL_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            last = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            (
                first.st_dev,
                first.st_ino,
                first.st_mode,
                first.st_size,
                first.st_mtime_ns,
                first.st_ctime_ns,
            )
            != (
                last.st_dev,
                last.st_ino,
                last.st_mode,
                last.st_size,
                last.st_mtime_ns,
                last.st_ctime_ns,
            )
        ):
            raise AuthoringError("authoring recovery journal changed while reading")
        return parse_recovery_journal(
            b"".join(chunks), expected_project=expected_project
        )

    def begin(
        self,
        bundle: ProjectCompilationBundle,
        reservation: ReservedStageEvidence,
    ) -> None:
        self.require_inactive()
        self._created_directory_candidates = derive_created_directory_candidates(
            tuple(
                (image.resolved_path, image.ancestors)
                for image in bundle.before_images
            )
        )
        self._document = _journal_document(bundle, reservation)
        self._replace(self._document)

    def record_created_directory(self, identity: PathIdentity) -> None:
        if self._document is None:
            raise RuntimeError("authoring journal has not begun")
        progress = self._document["created_directory_progress"]
        if not isinstance(progress, list):
            raise RuntimeError("authoring journal directory progress is invalid")
        index = len(progress)
        if (
            index >= len(self._created_directory_candidates)
            or identity.resolved_path != self._created_directory_candidates[index]
        ):
            raise AuthoringError(
                "authoring created directory progress is inconsistent"
            )
        progress.append(_identity_document(identity))
        self._replace(self._document)

    def record_replacement(self, index: int) -> None:
        if self._document is None:
            raise RuntimeError("authoring journal has not begun")
        progress = self._document["replacement_progress"]
        if not isinstance(progress, list):
            raise RuntimeError("authoring journal progress is invalid")
        progress.append(index)
        self._replace(self._document)

    def record_committed(self) -> None:
        if self._document is None:
            raise RuntimeError("authoring journal has not begun")
        progress = self._document["replacement_progress"]
        write_set = self._document["write_set"]
        committed = self._document["committed"]
        if not isinstance(progress, list) or not isinstance(write_set, list):
            raise RuntimeError("authoring journal progress is invalid")
        if progress != list(range(len(write_set))):
            raise AuthoringError("authoring journal commit progress is incomplete")
        if committed is not False:
            raise RuntimeError("authoring journal commit state is invalid")
        self._document["committed"] = True
        self._replace(self._document)

    def finish(self) -> None:
        verify_owner_file(self.journal_path)
        self.journal_path.unlink()
        fsync_owner_directory(self.directory)
        self._created_directory_candidates = ()
        self._document = None

    def _replace(self, document: dict[str, object]) -> None:
        encoded = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        descriptor, name = tempfile.mkstemp(
            prefix=".transaction-", suffix=".tmp", dir=self.directory
        )
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            seal_owner_file(temporary, writable=True)
            os.replace(temporary, self.journal_path)
            fsync_owner_directory(self.directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


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


def _project_namespace_for_identity(identity: PathIdentity) -> str:
    document = {
        "path": str(identity.resolved_path),
        "device": identity.device,
        "inode": identity.inode,
    }
    return hashlib.sha256(_canonical(document)).hexdigest()


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


def _journal_document(
    bundle: ProjectCompilationBundle, reservation: ReservedStageEvidence
) -> dict[str, object]:
    expected_stages = reserved_stage_set(
        tuple(image.resolved_path for image in bundle.after_images),
        reservation.operation_id,
    )
    if reservation.stages != expected_stages:
        raise AuthoringError("authoring reserved stage evidence is inconsistent")
    return {
        "schema": "lockstep.authoring-transaction/v4",
        "operation_id": reservation.operation_id,
        "project": {
            "path": str(bundle.resolved_project),
            "device": bundle.project_identity.device,
            "inode": bundle.project_identity.inode,
        },
        "read_set": [
            {
                "role": item.role,
                "path": str(item.resolved_path),
                "sha256": item.sha256,
                "size": len(item.content),
                "leaf": _leaf_document(item.leaf),
                "ancestors": [_identity_document(value) for value in item.ancestors],
            }
            for item in bundle.sources
        ],
        "write_set": [
            {
                "role": after.role,
                "path": str(after.resolved_path),
                "before": _before_document(before),
                "after": {
                    "sha256": after.sha256,
                    "size": len(after.content or b""),
                    "mode": after.mode,
                },
                "ancestors": [_identity_document(value) for value in before.ancestors],
            }
            for before, after in zip(
                bundle.before_images, bundle.after_images, strict=True
            )
        ],
        "reservation": {
            "kind": "complete-reserved-stage-absence/v1",
            "stages": [
                {
                    "index": stage.index,
                    "publication": str(stage.publication),
                    "restoration": str(stage.restoration),
                }
                for stage in reservation.stages
            ],
        },
        "replacement_progress": [],
        "created_directory_progress": [],
        "committed": False,
    }


def _before_document(image: DestinationImage) -> dict[str, object]:
    if image.content is None:
        return {"absent": True}
    return {
        "absent": False,
        "bytes": base64.b64encode(image.content).decode("ascii"),
        "sha256": image.sha256,
        "mode": image.mode,
        "leaf": _leaf_document(image.leaf),
    }


def _identity_document(identity: PathIdentity | LeafIdentity) -> dict[str, object]:
    return {
        "path": str(identity.resolved_path),
        "device": identity.device,
        "inode": identity.inode,
    }


def _leaf_document(identity: LeafIdentity | None) -> dict[str, object] | None:
    if identity is None:
        return None
    return {
        **_identity_document(identity),
        "mode": identity.mode,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
        "ctime_ns": identity.ctime_ns,
    }


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
