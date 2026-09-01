"""Public facade for fenced, disposable local Git workspaces."""

from __future__ import annotations

from pathlib import Path

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.owner_state import ensure_owner_directory, initialize_owner_state
from lockstep.runtime.project_snapshots import ProjectSnapshotStore
from lockstep.runtime.providers._workspace_attestation import WorkspaceAttestor
from lockstep.runtime.providers._workspace_core import (
    NoPublishProof,
    WorkspaceContext,
    WorkspaceError,
    WorkspaceLease,
    WorkspaceLimits,
    WorkspacePhase,
    WorkspacePurpose,
    _digest,
    _hex,
    _text,
)
from lockstep.runtime.providers._workspace_materialization import (
    WorkspaceMaterializationTransaction,
)
from lockstep.runtime.providers._workspace_records import WorkspaceRecordRepository
from lockstep.runtime.providers._workspace_rollover import WorkspaceRolloverTransaction

# Keep the established public DTO/exception identity for introspection and pickle.
WorkspaceError.__module__ = __name__
WorkspaceLease.__module__ = __name__
NoPublishProof.__module__ = __name__


class LocalGitWorkspaceProvider:
    """Stable provider API composed from focused workspace collaborators."""

    def __init__(
        self,
        owner_state_dir: str | Path,
        snapshots: ProjectSnapshotStore,
        blobs: BlobStore,
        *,
        limits: WorkspaceLimits | None = None,
    ) -> None:
        owner_state = initialize_owner_state(owner_state_dir)
        root = ensure_owner_directory(owner_state, "managed-workspaces")
        context = WorkspaceContext(
            records=ensure_owner_directory(root, "records"),
            checkouts=ensure_owner_directory(root, "checkouts"),
            staging=ensure_owner_directory(root, "staging"),
            quarantine=ensure_owner_directory(root, "quarantine"),
            snapshots=snapshots,
            blobs=blobs,
            limits=limits or snapshots.limits,
        )
        self._context = context

        # Preserve the established facade's diagnostic/private aliases.
        self._owner_state = owner_state
        self._root = root
        self._records = context.records
        self._checkouts = context.checkouts
        self._staging = context.staging
        self._quarantine = context.quarantine
        self._snapshots = context.snapshots
        self._blobs = context.blobs
        self._limits = context.limits
        self._attestor = WorkspaceAttestor(context)
        self._record_repository = WorkspaceRecordRepository(context)
        self._materializer = WorkspaceMaterializationTransaction(
            context, self._record_repository, self._attestor
        )
        self._rollover = WorkspaceRolloverTransaction(
            context, self._record_repository, self._attestor
        )

    def workspace_ref_for(self, effect_id: str, intent_digest: str) -> str:
        commitment = {
            "schema": "lockstep.workspace-ref/v1",
            "effect_id": _text(effect_id, "effect_id"),
            "intent_digest": _hex(intent_digest, "intent digest"),
        }
        return f"workspace:{_digest(commitment)}"

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
        return self._materializer.execute(
            effect_id=effect_id,
            request_digest=request_digest,
            workspace_ref=workspace_ref,
            input_snapshot_ref=input_snapshot_ref,
            declared_writes=declared_writes,
            purpose=purpose,
        )

    def inspect(self, workspace_ref: str) -> WorkspaceLease:
        return self._record_repository.inspect(workspace_ref)

    def quarantine_and_rollover(self, lease: WorkspaceLease) -> str:
        return self._rollover.quarantine_and_rollover(lease)

    def quarantine_no_publish(self, lease: WorkspaceLease) -> NoPublishProof:
        return self._rollover.quarantine_no_publish(lease)

    def release(self, lease: WorkspaceLease) -> None:
        self._rollover.release(lease)

    def _capture(self, workspace: Path):
        return self._attestor._capture(workspace)
