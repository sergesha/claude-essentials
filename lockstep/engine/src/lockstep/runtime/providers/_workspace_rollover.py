from __future__ import annotations
import os
import shutil
from pathlib import PurePosixPath

from lockstep.runtime.locking import file_lock
from lockstep.runtime.manifests import ProjectSnapshot as FilesystemSnapshot
from lockstep.runtime.project_snapshots import ProjectSnapshotRef
from lockstep.runtime.providers._workspace_attestation import WorkspaceAttestor
from lockstep.runtime.providers._workspace_core import (
    NoPublishProof,
    WorkspaceContext,
    WorkspaceError,
    WorkspaceLease,
    _read_regular_nofollow,
    _snapshot_ref,
    _workspace_digest,
)
from lockstep.runtime.providers._workspace_records import WorkspaceRecordRepository

class WorkspaceRolloverTransaction:
    def __init__(
        self,
        context: WorkspaceContext,
        records: WorkspaceRecordRepository,
        attestor: WorkspaceAttestor,
    ) -> None:
        self._context = context
        self._records = records
        self._attestor = attestor

    def _quarantine_for_rollover(
        self,
        key: str,
        current: WorkspaceLease,
    ) -> WorkspaceLease:
        if current.phase != "materialized":
            return current
        quarantined_path = self._records._quarantine_path(key)
        moved = quarantined_path.exists() and not current.workspace_path.exists()
        if quarantined_path.is_symlink() or (
            quarantined_path.exists() and not moved
        ):
            raise WorkspaceError("workspace quarantine destination already exists")
        if not moved:
            try:
                os.rename(current.workspace_path, quarantined_path)
                self._records._fsync_directory(self._context.checkouts)
                self._records._fsync_directory(self._context.quarantine)
            except OSError as exc:
                raise WorkspaceError(
                    "workspace could not be durably quarantined"
                ) from exc
        quarantined = WorkspaceLease(
            **{
                **current.__dict__,
                "revision": current.revision + 1,
                "workspace_path": quarantined_path,
                "phase": "quarantined",
                "rollover_snapshot_ref": None,
            }
        )
        self._records._write_record(quarantined, snapshot_ref_out=None)
        return quarantined

    def _rebind_relocated_workspace(
        self,
        current: WorkspaceLease,
    ) -> WorkspaceLease:
        self._attestor._preflight_tree_limits(current.workspace_path)
        relocated = self._attestor._capture(current.workspace_path)
        baseline = self._attestor._relocated_baseline(current.baseline, relocated)
        if baseline == current.baseline:
            return current
        rebound = WorkspaceLease(
            **{
                **current.__dict__,
                "revision": current.revision + 1,
                "baseline": baseline,
            }
        )
        self._records._write_record(rebound, snapshot_ref_out=None)
        return rebound

    def _copy_rollover_files(
        self,
        current: WorkspaceLease,
        before_copy: FilesystemSnapshot,
    ) -> dict[str, object]:
        files = {}
        copied_bytes = 0
        for entry in before_copy.entries:
            if entry.kind != "file":
                continue
            if len(files) >= self._context.limits.max_entries:
                raise WorkspaceError(
                    "workspace files exceed rollover admission limit"
                )
            assert entry.sha256 is not None
            data = _read_regular_nofollow(
                current.workspace_path.joinpath(
                    *PurePosixPath(entry.path).parts
                ),
                entry.sha256,
                max_bytes=self._context.limits.max_file_bytes,
            )
            copied_bytes += len(data)
            if copied_bytes > self._context.limits.max_total_bytes:
                raise WorkspaceError(
                    "workspace bytes exceed rollover admission limit"
                )
            files[entry.path] = self._context.blobs.put(
                data, expected_sha256=entry.sha256
            )
        return files

    def _capture_rollover_snapshot(self, current: WorkspaceLease):
        self._attestor._preflight_tree_limits(current.workspace_path)
        before_copy = self._attestor._capture(current.workspace_path)
        self._attestor._validate_output(current, before_copy)
        self._attestor._validate_snapshot_fidelity(before_copy)
        files = self._copy_rollover_files(current, before_copy)
        after_copy = self._attestor._capture(current.workspace_path)
        if after_copy != before_copy:
            raise WorkspaceError(
                "workspace changed during consistent before/copy/after capture"
            )
        if (
            self._attestor._vcs_tree_digest(current.workspace_path)
            != current.vcs_baseline_digest
        ):
            raise WorkspaceError("Git control state changed during rollover capture")
        previous = _snapshot_ref(current.input_snapshot_ref)
        input_snapshot = self._read_snapshot(previous)
        return self._context.snapshots.capture(
            files,
            declared_paths=tuple(
                sorted(
                    set(input_snapshot.declared_paths)
                    | set(current.declared_writes)
                )
            ),
            provenance={
                "source": "managed-workspace-rollover",
                "workspace_ref": current.workspace_ref,
                "request_digest": current.request_digest,
            },
            previous=previous,
        )

    def _commit_rollover(self, current: WorkspaceLease, rolled) -> str:
        result_ref = f"snapshot:{rolled.digest}"
        completed = WorkspaceLease(
            **{
                **current.__dict__,
                "revision": current.revision + 1,
                "rollover_snapshot_ref": result_ref,
            }
        )
        self._records._write_record(completed, snapshot_ref_out=result_ref)
        return result_ref

    def quarantine_and_rollover(
        self,
        lease: WorkspaceLease,
    ) -> str:
        key = _workspace_digest(lease.workspace_ref)
        record_path = self._records._record_path(key)
        with file_lock(record_path, timeout=30.0, stale_after=300.0):
            current = self._records._read_record(key)
            self._records._validate_current_lease(lease, current)
            if current.purpose != "managed_output":
                raise WorkspaceError("no-publish workspace cannot be rolled over")
            stored_snapshot_ref = self._records._stored_rollover_ref(key)
            if current.phase == "quarantined" and stored_snapshot_ref is not None:
                return stored_snapshot_ref
            if current.phase == "released":
                raise WorkspaceError("released workspace cannot be rolled over")

            current = self._quarantine_for_rollover(key, current)
            current = self._rebind_relocated_workspace(current)

            try:
                rolled = self._capture_rollover_snapshot(current)
            except Exception as exc:
                if isinstance(exc, WorkspaceError):
                    raise
                raise WorkspaceError(
                    f"workspace manifest integrity failure: {exc}"
                ) from exc
            return self._commit_rollover(current, rolled)

    def quarantine_no_publish(self, lease: WorkspaceLease) -> NoPublishProof:
        """Fence an operation workspace against publication and reuse.

        The process adapter proves quiescence separately.  This transition only
        makes the already committed no-publish purpose durable at the filesystem
        boundary; it intentionally does not inspect or snapshot operation output.
        """

        key = _workspace_digest(lease.workspace_ref)
        record_path = self._records._record_path(key)
        with file_lock(record_path, timeout=30.0, stale_after=300.0):
            current = self._records._read_record(key)
            self._records._validate_current_lease(lease, current)
            if current.purpose != "no_publish_operation":
                raise WorkspaceError("managed-output workspace requires rollover")
            if current.phase == "released":
                raise WorkspaceError("released workspace cannot be quarantined")
            if current.phase == "materialized":
                quarantined_path = self._records._quarantine_path(key)
                moved = (
                    quarantined_path.exists() and not current.workspace_path.exists()
                )
                if quarantined_path.is_symlink() or (
                    quarantined_path.exists() and not moved
                ):
                    raise WorkspaceError(
                        "workspace quarantine destination already exists"
                    )
                if not moved:
                    try:
                        os.rename(current.workspace_path, quarantined_path)
                        self._records._fsync_directory(self._context.checkouts)
                        self._records._fsync_directory(self._context.quarantine)
                    except OSError as exc:
                        raise WorkspaceError(
                            "workspace could not be durably quarantined"
                        ) from exc
                current = WorkspaceLease(
                    **{
                        **current.__dict__,
                        "revision": current.revision + 1,
                        "workspace_path": quarantined_path,
                        "phase": "quarantined",
                        "rollover_snapshot_ref": None,
                    }
                )
                self._records._write_record(current, snapshot_ref_out=None)
            return NoPublishProof(
                workspace_ref=current.workspace_ref,
                purpose="no_publish_operation",
                workspace_quarantined=True,
            )

    def release(
        self,
        lease: WorkspaceLease,
    ) -> None:
        key = _workspace_digest(lease.workspace_ref)
        record_path = self._records._record_path(key)
        with file_lock(record_path, timeout=30.0, stale_after=300.0):
            current = self._records._read_record(key)
            self._records._validate_current_lease(lease, current)
            if current.phase == "released":
                return
            if current.phase != "quarantined":
                raise WorkspaceError("only a quarantined workspace can be released")
            path = current.workspace_path
            expected = self._records._quarantine_path(key)
            if path != expected or path.is_symlink():
                raise WorkspaceError(
                    "workspace cleanup target failed containment check"
                )
            if path.exists():
                shutil.rmtree(path)
                self._records._fsync_directory(path.parent)
            released = WorkspaceLease(
                **{
                    **current.__dict__,
                    "revision": current.revision + 1,
                    "phase": "released",
                }
            )
            self._records._write_record(
                released, snapshot_ref_out=self._records._stored_rollover_ref(key)
            )

    def _read_snapshot(self, ref: ProjectSnapshotRef):
        try:
            return self._context.snapshots.read(ref)
        except Exception as exc:
            raise WorkspaceError(
                f"input snapshot cannot be verified: {ref.digest}"
            ) from exc
