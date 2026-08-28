"""Trusted owner-state journal for one project-bound authoring transaction."""

from __future__ import annotations

import base64
import hashlib
import json
import os
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
    __slots__ = ("directory", "journal_path", "_document")

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.journal_path = directory / "transaction.json"
        self._document: dict[str, object] | None = None

    @classmethod
    def create_for_bundle(
        cls, state_dir: Path, bundle: ProjectCompilationBundle
    ) -> AuthoringJournal:
        _validate_owner_state_location(state_dir, bundle.resolved_project)
        root = initialize_owner_state(state_dir)
        authoring = ensure_owner_directory(root, "authoring")
        directory = ensure_owner_directory(
            authoring, _project_namespace(bundle)
        )
        return cls(directory)

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
        if self.journal_path.exists() or self.journal_path.is_symlink():
            verify_owner_file(self.journal_path)
            raise AuthoringRecoveryRequired(
                "active authoring transaction requires recovery"
            )

    def begin(self, bundle: ProjectCompilationBundle, operation_id: str) -> None:
        self.require_inactive()
        self._document = _journal_document(bundle, operation_id)
        self._replace(self._document)

    def record_replacement(self, index: int) -> None:
        if self._document is None:
            raise RuntimeError("authoring journal has not begun")
        progress = self._document["replacement_progress"]
        if not isinstance(progress, list):
            raise RuntimeError("authoring journal progress is invalid")
        progress.append(index)
        self._replace(self._document)

    def finish(self) -> None:
        self.journal_path.unlink()
        fsync_owner_directory(self.directory)
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


def assert_no_active_journal(state_dir: Path, project: Path) -> None:
    project = project.resolve(strict=True)
    _validate_owner_state_location(state_dir, project)
    if not state_dir.exists() and not state_dir.is_symlink():
        return
    verify_owner_directory(state_dir)
    authoring = state_dir / "authoring"
    if not authoring.exists() and not authoring.is_symlink():
        return
    verify_owner_directory(authoring)
    info = project.lstat()
    identity = {"path": str(project), "device": info.st_dev, "inode": info.st_ino}
    digest = hashlib.sha256(_canonical(identity)).hexdigest()
    directory = authoring / digest
    if not directory.exists() and not directory.is_symlink():
        return
    verify_owner_directory(directory)
    journal = AuthoringJournal(directory)
    with journal.locked():
        journal.require_inactive()


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


def _project_namespace(bundle: ProjectCompilationBundle) -> str:
    identity = {
        "path": str(bundle.resolved_project),
        "device": bundle.project_identity.device,
        "inode": bundle.project_identity.inode,
    }
    return hashlib.sha256(_canonical(identity)).hexdigest()


def _journal_document(
    bundle: ProjectCompilationBundle, operation_id: str
) -> dict[str, object]:
    return {
        "schema": "lockstep.authoring-transaction/v1",
        "operation_id": operation_id,
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
        "replacement_progress": [],
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
    }


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
