from __future__ import annotations

from pathlib import Path

import pytest

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.project_snapshots import ProjectSnapshotStore
from lockstep.runtime.providers.workspaces import (
    LocalGitWorkspaceProvider,
    WorkspaceError,
)


def _workspace(tmp_path: Path, *, purpose: str):
    owner = tmp_path / "owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    seed = snapshots.capture(
        {"src/app.py": blobs.put(b"VALUE = 1\n")},
        declared_paths=("src/",),
        provenance={"source": "pinned-test"},
    )
    provider = LocalGitWorkspaceProvider(owner, snapshots, blobs)
    workspace_ref = provider.workspace_ref_for("effect", "a" * 64)
    lease = provider.materialize(
        effect_id="effect",
        request_digest="b" * 64,
        workspace_ref=workspace_ref,
        input_snapshot_ref=f"snapshot:{seed.digest}",
        declared_writes=(),
        purpose=purpose,
    )
    return provider, lease


def test_no_publish_workspace_purpose_is_immutable(tmp_path: Path) -> None:
    provider, lease = _workspace(tmp_path, purpose="no_publish_operation")

    assert lease.purpose == "no_publish_operation"
    with pytest.raises(WorkspaceError, match="purpose|another request"):
        provider.materialize(
            effect_id=lease.effect_id,
            request_digest=lease.request_digest,
            workspace_ref=lease.workspace_ref,
            input_snapshot_ref=lease.input_snapshot_ref,
            declared_writes=lease.declared_writes,
            purpose="managed_output",
        )


def test_no_publish_quarantine_never_returns_successor_snapshot(tmp_path: Path) -> None:
    provider, lease = _workspace(tmp_path, purpose="no_publish_operation")
    (lease.workspace_path / "src/app.py").write_text("VALUE = 2\n")

    proof = provider.quarantine_no_publish(lease)

    assert proof.workspace_ref == lease.workspace_ref
    assert proof.purpose == "no_publish_operation"
    assert proof.workspace_quarantined is True
    assert proof.rollover_snapshot_ref is None
    assert provider.inspect(lease.workspace_ref).phase == "quarantined"

