"""Recoverable publication of immutable artifacts into an exact project tree.

Publication is deliberately a separate external-effect port.  ``prepare`` only
writes owner-state intent; ``apply_or_recover`` is the sole project mutation
boundary and every retry decides from the durable journal plus current bytes.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import tempfile
from pathlib import Path

from lockstep.runtime._publication_queries import _ProjectPublicationQueries
from lockstep.runtime._publication_values import (
    PublicationConflict,
    PublicationEntry as PublicationEntry,
    PublicationError as PublicationError,
    PublicationJournalError as PublicationJournalError,
    PublicationLimits,
    PublicationPhase,
    PublicationReceipt,
    PublicationRequest,
    PreparedPublication,
    _HEX as _HEX,
    _canonical,
    _coordinate_data as _coordinate_data,
    _counter as _counter,
    _digest as _digest,
    _entry_data,
    _image_data,
    _image_from_data as _image_from_data,
    _request_data,
    _same_image,
    _text as _text,
)
from lockstep.runtime.artifacts import ArtifactRegistry
from lockstep.runtime.blobs import BlobRef, BlobStore
from lockstep.runtime.locking import file_lock
from lockstep.runtime.owner_state import (
    StorageLimitExceeded,
    ensure_owner_directory,
    initialize_owner_state,
    seal_owner_file,
)

_MISSING = object()


def _after_replacement(_direction: str, _index: int) -> None:
    """Crash-injection seam used to prove recovery after every replacement."""


class ProjectPublisher(_ProjectPublicationQueries):
    required_authorities = ("publication",)

    def __init__(
        self,
        owner_state_dir: str | Path,
        project_root: str | Path,
        registry: ArtifactRegistry,
        blob_store: BlobStore,
        *,
        limits: PublicationLimits | None = None,
    ) -> None:
        self._owner_state = initialize_owner_state(owner_state_dir)
        self._limits = limits or PublicationLimits()
        self._registry = registry
        self._blobs = blob_store
        root = Path(project_root)
        try:
            root_info = root.lstat()
        except OSError as exc:
            raise PublicationConflict("project root is unavailable") from exc
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise PublicationConflict("project root must be a real directory")
        self._project = root
        self._root_device = root_info.st_dev
        self._root_inode = root_info.st_ino
        binding = {
            "schema": "lockstep.project-publisher-binding/v1",
            "root": str(root.resolve(strict=True)),
            "device": root_info.st_dev,
            "inode": root_info.st_ino,
        }
        self.binding_digest = hashlib.sha256(_canonical(binding)).hexdigest()
        self._directory = ensure_owner_directory(
            self._owner_state, f"publications/{self.binding_digest}"
        )
        self._journals = ensure_owner_directory(
            self._owner_state, f"publications/{self.binding_digest}/journals"
        )
        self._active = self._directory / "active.json"

    def prepare(self, request: PublicationRequest) -> PreparedPublication:
        if not isinstance(request, PublicationRequest):
            raise TypeError("publication request must be a closed value")
        if request.publisher_binding_digest != self.binding_digest:
            raise PublicationConflict("publication request names another publisher")
        journal_digest = hashlib.sha256(
            _canonical(
                {
                    "schema": "lockstep.publication-journal-key/v1",
                    "request_digest": request.request_digest,
                    "publisher_binding_digest": self.binding_digest,
                }
            )
        ).hexdigest()
        handle = PreparedPublication(
            journal_digest, request.request_digest, self.binding_digest
        )
        with file_lock(self._active, timeout=30.0, stale_after=300.0):
            active = self._read_active_optional()
            if active is not None and active != journal_digest:
                old = self._read_journal_digest(active)
                old_phase = PublicationPhase(str(old["phase"]))
                if old_phase not in {
                    PublicationPhase.APPLIED,
                    PublicationPhase.ROLLED_BACK,
                }:
                    raise PublicationConflict(
                        "another publication is active for this project"
                    )
            journal_path = self.journal_path(handle)
            if journal_path.exists() or journal_path.is_symlink():
                existing = self._read_journal(handle)
                if existing["request"] != _request_data(request):
                    raise PublicationConflict(
                        "publication request differs from its durable journal"
                    )
            else:
                journal = {
                    "schema": "lockstep.publication-journal/v1",
                    "phase": PublicationPhase.PREPARED.value,
                    "request_digest": request.request_digest,
                    "publisher_binding_digest": self.binding_digest,
                    "request": _request_data(request),
                    "plan": self._build_plan(request),
                    "cursor": 0,
                }
                encoded = _canonical(journal)
                if len(encoded) > self._limits.max_journal_bytes:
                    raise StorageLimitExceeded(
                        "publication journal exceeds admission limit"
                    )
                self._write_atomic(journal_path, encoded, mutable=True)
            self._write_atomic(
                self._active,
                _canonical(
                    {
                        "schema": "lockstep.active-publication/v1",
                        "journal_digest": journal_digest,
                    }
                ),
                mutable=True,
            )
        return handle

    def _build_plan(self, request: PublicationRequest) -> list[dict[str, object]]:
        plan: list[dict[str, object]] = []
        total_bytes = 0
        root_fd = self._open_root()
        try:
            for entry in request.entries:
                record = self._registry.read(entry.artifact_ref)
                if (
                    record.public_run_id != request.public_run_id
                    or record.project_identity != request.project_identity
                    or record.definition_digest != request.definition_digest
                ):
                    raise PublicationConflict(
                        "artifact provenance does not match the publication request"
                    )
                parent_fd, leaf, ancestors = self._open_parent(
                    root_fd, entry.destination
                )
                try:
                    before_data = self._current_image_data(parent_fd, leaf)
                finally:
                    os.close(parent_fd)
                total_bytes += record.blob.size
                if before_data is not None:
                    total_bytes += before_data[0].size
                if total_bytes > self._limits.max_total_bytes:
                    raise StorageLimitExceeded(
                        "publication aggregate bytes exceed admission limit"
                    )
                before = None
                if before_data is not None:
                    before_ref, before_mode, content = before_data
                    if self._blobs.put(content) != before_ref:
                        raise PublicationConflict(
                            "publication preimage blob admission changed"
                        )
                    before = (before_ref, before_mode)
                plan.append(
                    {
                        **_entry_data(entry),
                        "after": _image_data((record.blob, 0o600)),
                        "before": _image_data(before),
                        "ancestors": ancestors,
                    }
                )
        finally:
            os.close(root_fd)
        return plan

    def apply_or_recover(
        self, handle: PreparedPublication
    ) -> PublicationReceipt:
        path = self.journal_path(handle)
        with file_lock(path, timeout=30.0, stale_after=300.0):
            journal = self._read_journal(handle)
            phase = PublicationPhase(str(journal["phase"]))
            if phase is PublicationPhase.APPLIED:
                self._verify_complete(journal["plan"], direction="apply")
                return self._receipt(handle, PublicationPhase.APPLIED)
            if phase not in {PublicationPhase.PREPARED, PublicationPhase.APPLYING}:
                raise PublicationConflict(f"cannot apply publication in phase {phase}")
            if phase is PublicationPhase.PREPARED:
                journal["phase"] = PublicationPhase.APPLYING.value
                journal["cursor"] = 0
                self._store_journal(path, journal)
                # Admission into the applying phase and the first replacement
                # are one publication commitment action.  The coordinator
                # invokes this branch inside authority and native commitment
                # guards; recovery may advance later actions from that durable
                # commitment without re-resolving revocable policy.
                return self._advance_plan(
                    path, journal, handle, direction="apply"
                )
            return self._advance_plan(path, journal, handle, direction="apply")

    def rollback_or_recover(
        self, handle: PreparedPublication
    ) -> PublicationReceipt:
        path = self.journal_path(handle)
        with file_lock(path, timeout=30.0, stale_after=300.0):
            journal = self._read_journal(handle)
            phase = PublicationPhase(str(journal["phase"]))
            if phase is PublicationPhase.ROLLED_BACK:
                self._verify_complete(journal["plan"], direction="rollback")
                return self._receipt(handle, PublicationPhase.ROLLED_BACK)
            if phase not in {
                PublicationPhase.APPLYING,
                PublicationPhase.ROLLBACK_PENDING,
            }:
                raise PublicationConflict(
                    f"cannot roll back publication in phase {phase}"
                )
            if phase is PublicationPhase.APPLYING:
                journal["phase"] = PublicationPhase.ROLLBACK_PENDING.value
                journal["cursor"] = min(
                    int(journal["cursor"]),  # type: ignore[call-overload]
                    len(journal["plan"]) - 1,  # type: ignore[arg-type]
                )
                self._store_journal(path, journal)
                return self._receipt(handle, PublicationPhase.ROLLBACK_PENDING)
            return self._advance_plan(path, journal, handle, direction="rollback")

    def _advance_plan(
        self,
        path: Path,
        journal: dict[str, object],
        handle: PreparedPublication,
        *,
        direction: str,
    ) -> PublicationReceipt:
        plan = self._validate_plan(journal["plan"])
        cursor = int(journal["cursor"])  # type: ignore[call-overload]
        terminal = cursor >= len(plan) if direction == "apply" else cursor < 0
        if terminal:
            self._verify_complete(journal["plan"], direction=direction)
            phase = (
                PublicationPhase.APPLIED
                if direction == "apply"
                else PublicationPhase.ROLLED_BACK
            )
            journal["phase"] = phase.value
            self._store_journal(path, journal)
            return self._receipt(handle, phase)
        item = plan[cursor]
        root_fd = self._open_root()
        try:
            parent_fd, leaf, _ancestors = self._open_parent(
                root_fd, item["destination"], expected=item["ancestors"]
            )
            try:
                before = item["before"]
                after = item["after"]
                expected = before if direction == "apply" else after
                desired = after if direction == "apply" else before
                current = self._current_image(parent_fd, leaf)
                if _same_image(current, desired):
                    journal["cursor"] = cursor + (1 if direction == "apply" else -1)
                    self._store_journal(path, journal)
                    return self._receipt(
                        handle, PublicationPhase(str(journal["phase"]))
                    )
                if not _same_image(current, expected):
                    raise PublicationConflict(
                        f"publication destination changed: {item['destination']}"
                    )
                self._replace(parent_fd, leaf, expected, desired)
                _after_replacement(direction, cursor)
                return self._receipt(
                    handle, PublicationPhase(str(journal["phase"]))
                )
            finally:
                os.close(parent_fd)
        finally:
            os.close(root_fd)

    def _replace(
        self,
        parent_fd: int,
        leaf: str,
        expected: tuple[BlobRef, int] | None,
        desired: tuple[BlobRef, int] | None,
    ) -> None:
        if desired is None:
            if not _same_image(self._current_image(parent_fd, leaf), expected):
                raise PublicationConflict(
                    f"publication destination changed: {leaf}"
                )
            try:
                os.unlink(leaf, dir_fd=parent_fd)
            except FileNotFoundError:
                return
            os.fsync(parent_fd)
            return
        blob, mode = desired
        data = self._blobs.read(blob)
        temporary = f".lockstep-publish-{secrets.token_hex(16)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
            os.fchmod(fd, mode)
            os.fsync(fd)
            # Stage first, then recheck at the namespace mutation edge.  The
            # earlier plan check cannot authorize overwriting bytes installed
            # while the desired image was being materialized and synced.
            if not _same_image(self._current_image(parent_fd, leaf), expected):
                raise PublicationConflict(
                    f"publication destination changed: {leaf}"
                )
            os.replace(temporary, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass

    def _store_journal(self, path: Path, journal: dict[str, object]) -> None:
        encoded = _canonical(journal)
        if len(encoded) > self._limits.max_journal_bytes:
            raise StorageLimitExceeded("publication journal exceeds admission limit")
        self._write_atomic(path, encoded, mutable=True)

    def _write_atomic(self, path: Path, encoded: bytes, *, mutable: bool) -> None:
        fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            seal_owner_file(temporary, writable=mutable)
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()
