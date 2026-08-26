from __future__ import annotations
import hashlib, json, os, shutil, stat, tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from lockstep.runtime.locking import file_lock
from lockstep.runtime.manifests import PathContractError, ProjectWritePath, capture_project, compare_effect, snapshot_from_data, snapshot_to_data
from lockstep.runtime.manifests import ProjectSnapshot as FilesystemSnapshot
from lockstep.runtime.owner_state import InsecureStatePath, seal_owner_file, verify_owner_file
from lockstep.runtime.project_paths import portable_collision_key, validate_portable_project_paths
from lockstep.runtime.project_snapshots import ProjectSnapshotRef
from lockstep.runtime.providers._workspace_core import NoPublishProof, WorkspaceError, WorkspaceLease, WorkspacePurpose, _canonical, _hex, _read_regular_nofollow, _snapshot_ref, _stat_identity, _text, _workspace_digest

class WorkspaceMaterializationTransaction:
    def __init__(self, owner: Any) -> None: self._owner = owner
    def __getattr__(self, name):
        if name == "_capture":
            return self._owner._capture
        for target in (self._owner._record_repository, self._owner._attestor, self._owner):
            try: return getattr(target, name)
            except AttributeError: pass
        raise AttributeError(name)

    def execute(self, **request):
        return self.materialize(**request)
    def _materialization_request(
        self,
        *,
        effect_id: str,
        request_digest: str,
        workspace_ref: str,
        input_snapshot_ref: str,
        declared_writes: tuple[str, ...],
        purpose: WorkspacePurpose,
    ) -> tuple[str, str, str, ProjectSnapshotRef, tuple[str, ...]]:
        effect_id = _text(effect_id, "effect_id")
        request_digest = _hex(request_digest, "request digest")
        key = _workspace_digest(workspace_ref)
        snapshot_ref = _snapshot_ref(input_snapshot_ref)
        if purpose not in {"managed_output", "no_publish_operation"}:
            raise WorkspaceError("unknown workspace purpose")
        if not isinstance(declared_writes, tuple):
            declared_writes = tuple(declared_writes)
        normalized_writes = tuple(
            item.value
            for item in validate_portable_project_paths(
                (
                    (
                        path,
                        "prefix"
                        if isinstance(path, str) and path.endswith("/")
                        else "file",
                    )
                    for path in declared_writes
                ),
                limits=self._limits,
                label="workspace declared write entries",
            )
        )
        return effect_id, request_digest, key, snapshot_ref, normalized_writes

    def _reusable_workspace(
        self,
        key: str,
        *,
        expected: tuple[str, str, str, tuple[str, ...], WorkspacePurpose],
    ) -> WorkspaceLease | None:
        record_path = self._record_path(key)
        if not (record_path.exists() or record_path.is_symlink()):
            return None
        lease = self._read_record(key)
        observed = (
            lease.effect_id,
            lease.request_digest,
            lease.input_snapshot_ref,
            lease.declared_writes,
            lease.purpose,
        )
        if observed != expected:
            raise WorkspaceError("workspace reference is bound to another request")
        if lease.phase != "materialized":
            raise WorkspaceError("quarantined or released workspace is not reusable")
        self._preflight_tree_limits(lease.workspace_path)
        if self._capture(lease.workspace_path) != lease.baseline:
            raise WorkspaceError(
                "materialized workspace drifted from its launch baseline"
            )
        if self._vcs_tree_digest(lease.workspace_path) != lease.vcs_baseline_digest:
            raise WorkspaceError("materialized workspace Git control state drifted")
        return lease

    def _prepare_materialization_paths(self, key: str) -> tuple[Path, Path]:
        checkout = self._checkout_path(key)
        if checkout.exists() or checkout.is_symlink():
            self._discard_recovery_tree(checkout, self._checkouts)
        temporary = self._staging_path(key)
        if temporary.exists() or temporary.is_symlink():
            self._discard_recovery_tree(temporary, self._staging)
        temporary.mkdir(mode=0o700)
        return checkout, temporary

    def _populate_materialization(self, temporary: Path, snapshot) -> None:
        for entry in snapshot.files:
            destination = temporary.joinpath(*PurePosixPath(entry.path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._write_private_file(destination, self._blobs.read(entry.blob))

    def _publish_materialization(
        self,
        *,
        temporary: Path,
        checkout: Path,
        snapshot,
        declared_writes: tuple[str, ...],
    ) -> tuple[FilesystemSnapshot, str]:
        self._initialize_git_control(temporary)
        self._fsync_materialized_tree(temporary)
        published = False
        try:
            os.rename(temporary, checkout)
            published = True
            self._fsync_directory(self._checkouts)
            baseline = self._capture(checkout)
            self._verify_exact_input(baseline, snapshot)
            vcs_baseline_digest = self._vcs_tree_digest(checkout)
            for path in declared_writes:
                ProjectWritePath.parse(path, checkout)
            return baseline, vcs_baseline_digest
        except Exception:
            if published and checkout.exists() and not checkout.is_symlink():
                shutil.rmtree(checkout)
            raise

    @staticmethod
    def _cleanup_materialization(
        temporary: Path,
    ) -> None:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)

    def _read_snapshot(self, ref: ProjectSnapshotRef):
        try:
            return self._snapshots.read(ref)
        except Exception as exc:
            raise WorkspaceError(
                f"input snapshot cannot be verified: {ref.digest}"
            ) from exc

    def materialize(
        self,
        *,
        effect_id: str,
        request_digest: str,
        workspace_ref: str,
        input_snapshot_ref: str,
        declared_writes: tuple[str, ...],
        purpose: WorkspacePurpose = "managed_output",
    ) -> WorkspaceLease:
        (
            effect_id,
            request_digest,
            key,
            snapshot_ref,
            declared_writes,
        ) = self._materialization_request(
            effect_id=effect_id,
            request_digest=request_digest,
            workspace_ref=workspace_ref,
            input_snapshot_ref=input_snapshot_ref,
            declared_writes=declared_writes,
            purpose=purpose,
        )
        record_path = self._record_path(key)
        with file_lock(record_path, timeout=30.0, stale_after=300.0):
            reusable = self._reusable_workspace(
                key,
                expected=(
                    effect_id,
                    request_digest,
                    input_snapshot_ref,
                    declared_writes,
                    purpose,
                ),
            )
            if reusable is not None:
                return reusable

            snapshot_before = self._read_snapshot(snapshot_ref)
            paths = tuple(entry.path for entry in snapshot_before.files)
            validate_portable_project_paths(
                ((path, "file") for path in paths),
                limits=self._limits,
                label="workspace snapshot entries",
            )
            checkout, temporary = self._prepare_materialization_paths(key)
            try:
                self._populate_materialization(temporary, snapshot_before)
                snapshot_after = self._read_snapshot(snapshot_ref)
                if snapshot_after != snapshot_before:
                    raise WorkspaceError("input snapshot changed while materializing")
                baseline, vcs_baseline_digest = self._publish_materialization(
                    temporary=temporary,
                    checkout=checkout,
                    snapshot=snapshot_before,
                    declared_writes=declared_writes,
                )
            except Exception as exc:
                self._cleanup_materialization(temporary)
                if isinstance(exc, WorkspaceError):
                    raise
                raise WorkspaceError(
                    f"workspace materialization failed: {exc}"
                ) from exc

            lease = WorkspaceLease(
                workspace_ref=workspace_ref,
                effect_id=effect_id,
                request_digest=request_digest,
                input_snapshot_ref=input_snapshot_ref,
                revision=1,
                workspace_path=checkout,
                declared_writes=tuple(declared_writes),
                purpose=purpose,
                baseline=baseline,
                vcs_baseline_digest=vcs_baseline_digest,
                phase="materialized",
            )
            try:
                self._write_record(lease, snapshot_ref_out=None)
            except Exception:
                if checkout.exists() and not checkout.is_symlink():
                    shutil.rmtree(checkout)
                raise
            return lease
