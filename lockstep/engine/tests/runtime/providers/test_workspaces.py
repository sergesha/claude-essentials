from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.project_paths import ProjectTreeLimits
from lockstep.runtime.project_snapshots import ProjectSnapshotRef, ProjectSnapshotStore
from lockstep.runtime.providers.workspaces import (
    LocalGitWorkspaceProvider,
    WorkspaceError,
)


def _materialized(tmp_path: Path):
    owner = tmp_path / "owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    seed = snapshots.capture(
        {"src/app.py": blobs.put(b"VALUE = 1\n")},
        declared_paths=("src/",),
        provenance={"source": "workspace-test"},
    )
    provider = LocalGitWorkspaceProvider(owner, snapshots, blobs)
    workspace_ref = provider.workspace_ref_for("effect", "a" * 64)
    lease = provider.materialize(
        effect_id="effect",
        request_digest="b" * 64,
        workspace_ref=workspace_ref,
        input_snapshot_ref=f"snapshot:{seed.digest}",
        declared_writes=("src/",),
    )
    return provider, lease


def test_workspace_lease_uses_revision_and_phase_without_fake_fences(tmp_path):
    _provider, lease = _materialized(tmp_path)

    assert lease.revision == 1
    assert lease.phase == "materialized"


def test_rollover_returns_snapshot_ref_and_rejects_stale_revision(tmp_path):
    provider, lease = _materialized(tmp_path)
    (lease.workspace_path / "src/app.py").write_text("VALUE = 2\n")

    snapshot_ref = provider.quarantine_and_rollover(lease)

    assert snapshot_ref.startswith("snapshot:")
    with pytest.raises(WorkspaceError, match="revision|stale"):
        provider.quarantine_and_rollover(lease)
    current = provider.inspect(lease.workspace_ref)
    assert provider.quarantine_and_rollover(current) == snapshot_ref


def test_release_requires_current_revision_and_is_idempotent_by_phase(tmp_path):
    provider, lease = _materialized(tmp_path)
    provider.quarantine_and_rollover(lease)
    quarantined = provider.inspect(lease.workspace_ref)

    with pytest.raises(WorkspaceError, match="revision|stale"):
        provider.release(lease)

    provider.release(quarantined)
    released = provider.inspect(lease.workspace_ref)
    assert released.phase == "released"
    provider.release(released)


@pytest.mark.parametrize("mutation", ["symlink", "vcs", "undeclared"])
def test_rollover_rejects_each_workspace_escape_surface(tmp_path, mutation):
    provider, lease = _materialized(tmp_path)
    if mutation == "symlink":
        outside = lease.workspace_path.parent / "outside"
        outside.write_text("outside")
        (lease.workspace_path / "src/link").symlink_to(outside)
    elif mutation == "vcs":
        (lease.workspace_path / ".git/hooks/hostile").write_text("payload")
    else:
        (lease.workspace_path / "outside-declaration.txt").write_text("payload")

    with pytest.raises(WorkspaceError, match="Git|symlink|manifest|declared|integrity"):
        provider.quarantine_and_rollover(lease)


@pytest.mark.parametrize(
    "declared_writes",
    [("Out", "out"), ("node", "node/child"), ("one/two/three/",)],
)
def test_materialize_validates_declared_writes_as_one_bounded_tree(
    tmp_path, declared_writes
):
    owner = tmp_path / "owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    seed = snapshots.capture(
        {"src/app.py": blobs.put(b"content")},
        declared_paths=("src/",),
        provenance={},
    )
    provider = LocalGitWorkspaceProvider(
        owner,
        snapshots,
        blobs,
        limits=ProjectTreeLimits(max_entries=2),
    )
    workspace_ref = provider.workspace_ref_for("effect", "a" * 64)

    with pytest.raises(ValueError, match="collision|descendant|entries"):
        provider.materialize(
            effect_id="effect",
            request_digest="b" * 64,
            workspace_ref=workspace_ref,
            input_snapshot_ref=f"snapshot:{seed.digest}",
            declared_writes=declared_writes,
        )


def test_rollover_checks_tree_limits_before_first_mutable_capture(tmp_path, monkeypatch):
    provider, lease = _materialized(tmp_path)
    (lease.workspace_path / "src/extra.py").write_text("extra")
    provider = LocalGitWorkspaceProvider(
        provider._owner_state,
        provider._snapshots,
        provider._blobs,
        limits=ProjectTreeLimits(max_entries=1),
    )

    def capture_must_not_run(_workspace):
        raise AssertionError("capture ran before tree-limit preflight")

    monkeypatch.setattr(provider._attestor, "_capture", capture_must_not_run)
    with pytest.raises(WorkspaceError, match="entries"):
        provider.quarantine_and_rollover(lease)


def test_materialize_reuse_checks_limits_before_recapture(tmp_path, monkeypatch):
    provider, lease = _materialized(tmp_path)
    (lease.workspace_path / "src/extra.py").write_text("extra")
    provider = LocalGitWorkspaceProvider(
        provider._owner_state,
        provider._snapshots,
        provider._blobs,
        limits=ProjectTreeLimits(max_entries=1),
    )

    def capture_must_not_run(_workspace):
        raise AssertionError("capture ran before tree-limit preflight")

    monkeypatch.setattr(provider._attestor, "_capture", capture_must_not_run)
    with pytest.raises(WorkspaceError, match="entries"):
        provider.materialize(
            effect_id=lease.effect_id,
            request_digest=lease.request_digest,
            workspace_ref=lease.workspace_ref,
            input_snapshot_ref=lease.input_snapshot_ref,
            declared_writes=lease.declared_writes,
        )


def test_materialize_recovers_deterministic_staging_and_unrecorded_checkout(tmp_path):
    owner = tmp_path / "owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    seed = snapshots.capture(
        {"src/app.py": blobs.put(b"expected")},
        declared_paths=("src/",),
        provenance={},
    )
    provider = LocalGitWorkspaceProvider(owner, snapshots, blobs)
    workspace_ref = provider.workspace_ref_for("effect", "a" * 64)
    key = workspace_ref.removeprefix("workspace:")
    staging = owner / "managed-workspaces" / "staging" / key
    checkout = owner / "managed-workspaces" / "checkouts" / key
    staging.mkdir(parents=True)
    checkout.mkdir(parents=True)
    (staging / "partial").write_text("partial")
    (checkout / "stale").write_text("stale")

    lease = provider.materialize(
        effect_id="effect",
        request_digest="b" * 64,
        workspace_ref=workspace_ref,
        input_snapshot_ref=f"snapshot:{seed.digest}",
        declared_writes=("src/",),
    )

    assert not staging.exists()
    assert not (checkout / "stale").exists()
    assert (lease.workspace_path / "src/app.py").read_bytes() == b"expected"


def test_materialize_constructs_git_metadata(tmp_path):
    provider, lease = _materialized(tmp_path)

    assert provider.inspect(lease.workspace_ref) == lease
    assert (lease.workspace_path / ".git/HEAD").read_bytes() == (
        b"ref: refs/heads/lockstep\n"
    )
    assert (lease.workspace_path / ".git/config").is_file()
    assert (lease.workspace_path / ".git/objects/info").is_dir()
    assert (lease.workspace_path / ".git/objects/pack").is_dir()
    assert (lease.workspace_path / ".git/refs/heads").is_dir()
    assert (lease.workspace_path / ".git/refs/tags").is_dir()


def test_structurally_built_unborn_git_tree_is_recognized_when_git_exists(tmp_path):
    git = Path("/usr/bin/git")
    if not git.is_file():
        pytest.skip("absolute system Git is unavailable")
    _provider, lease = _materialized(tmp_path)

    result = subprocess.run(
        (str(git), "-C", str(lease.workspace_path), "rev-parse", "--is-inside-work-tree"),
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "true"


@pytest.mark.parametrize("mutation", ["executable", "empty-directory"])
def test_rollover_rejects_state_content_only_snapshot_cannot_reproduce(
    tmp_path, mutation
):
    provider, lease = _materialized(tmp_path)
    if mutation == "executable":
        target = lease.workspace_path / "src/app.py"
        target.chmod(target.stat().st_mode | 0o100)
    else:
        (lease.workspace_path / "src/empty").mkdir()

    with pytest.raises(WorkspaceError, match="executable|empty director|fidelity"):
        provider.quarantine_and_rollover(lease)


def test_rollover_snapshot_materializes_the_same_admitted_project_tree(tmp_path):
    provider, lease = _materialized(tmp_path)
    (lease.workspace_path / "src/app.py").write_text("VALUE = 2\n")
    snapshot_ref = provider.quarantine_and_rollover(lease)
    provider.release(provider.inspect(lease.workspace_ref))

    successor_ref = provider.workspace_ref_for("successor", "c" * 64)
    successor = provider.materialize(
        effect_id="successor",
        request_digest="d" * 64,
        workspace_ref=successor_ref,
        input_snapshot_ref=snapshot_ref,
        declared_writes=("src/",),
    )
    sealed = provider._snapshots.read(
        ProjectSnapshotRef(snapshot_ref.removeprefix("snapshot:"))
    )

    assert tuple(entry.path for entry in sealed.files) == ("src/app.py",)
    assert (successor.workspace_path / "src/app.py").read_bytes() == b"VALUE = 2\n"
    assert not any(
        entry.kind == "symlink" or (entry.kind == "file" and entry.executable)
        for entry in provider._capture(successor.workspace_path).entries
    )
