"""Parallel branch inputs contain accepted branch changes, never live siblings."""

from pathlib import Path

import pytest

from lockstep.runtime import _snapshot_lineage as lineage
from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.project_snapshots import ProjectSnapshotStore


def _environment(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "base.txt").write_text("base")
    blobs = BlobStore(tmp_path / "owner")
    snapshots = ProjectSnapshotStore(tmp_path / "owner", blobs)
    binding = RunBinding("run", "thread", "a" * 64, "bundle:" + "b" * 64, str(project))
    start = lineage.capture_authoritative_snapshot(
        project, snapshots, blobs, binding, previous=None, purpose="run-start"
    )
    return project, blobs, snapshots, binding, start


def _contents(snapshots, blobs, ref):
    return {item.path: blobs.read(item.blob) for item in snapshots.read(ref).files}


def test_parallel_successor_excludes_unaccepted_sibling_files(tmp_path: Path):
    project, blobs, snapshots, binding, start = _environment(tmp_path)
    (project / "left.txt").write_text("left")
    (project / "right.txt").write_text("right")
    left = lineage.capture_authoritative_snapshot(
        project, snapshots, blobs, binding, previous=start, purpose="manual",
        writes=("left.txt",),
    )
    assert _contents(snapshots, blobs, left) == {
        "base.txt": b"base", "left.txt": b"left",
    }
    (project / "left.txt").unlink()
    deleted = lineage.capture_authoritative_snapshot(
        project, snapshots, blobs, binding, previous=left, purpose="manual",
        writes=("left.txt",),
    )
    assert _contents(snapshots, blobs, deleted) == {"base.txt": b"base"}


def test_join_merges_immutable_branch_changes_and_retains_parents(tmp_path: Path):
    project, blobs, snapshots, binding, start = _environment(tmp_path)
    (project / "left.txt").write_text("left")
    left = lineage.capture_authoritative_snapshot(
        project, snapshots, blobs, binding, previous=start, purpose="manual", writes=("left.txt",)
    )
    (project / "left.txt").unlink()
    (project / "right.txt").write_text("right")
    right = lineage.capture_authoritative_snapshot(
        project, snapshots, blobs, binding, previous=start, purpose="manual", writes=("right.txt",)
    )
    (project / "right.txt").write_text("unaccepted live edit")
    joined = lineage.merge_lineage_snapshots((left, right), snapshots, binding)
    assert _contents(snapshots, blobs, joined) == {
        "base.txt": b"base", "left.txt": b"left", "right.txt": b"right",
    }
    assert lineage.resolve_lineage_snapshot((joined, left), snapshots) == left
    assert lineage.resolve_lineage_snapshot((joined, right), snapshots) == right
    assert lineage.merge_lineage_snapshots((right, left), snapshots, binding) == joined
    successor = snapshots.capture(
        {"base.txt": blobs.put(b"later")}, declared_paths=("base.txt",),
        provenance=snapshots.read(joined).provenance, previous=joined,
    )
    assert lineage.merge_lineage_snapshots((left, right, successor), snapshots, binding) == successor


def test_join_rejects_conflicting_branch_results(tmp_path: Path):
    project, blobs, snapshots, binding, start = _environment(tmp_path)
    tips = []
    for content in ("left", "right"):
        (project / "base.txt").write_text(content)
        tips.append(lineage.capture_authoritative_snapshot(
            project, snapshots, blobs, binding, previous=start, purpose="manual", writes=("base.txt",)
        ))
    with pytest.raises(lineage.RuntimeSnapshotConflict, match="conflicting"):
        lineage.merge_lineage_snapshots(tips, snapshots, binding)


def test_single_manual_tip_does_not_change_later_unrelated_fanin_policy(tmp_path: Path):
    project, blobs, snapshots, binding, start = _environment(tmp_path)
    (project / "base.txt").write_text("accepted parallel manual")
    manual = lineage.capture_authoritative_snapshot(
        project, snapshots, blobs, binding, previous=start, purpose="manual", writes=("base.txt",)
    )
    assert lineage.merge_lineage_snapshots((manual,), snapshots, binding) == manual
    tips = []
    for content in ("left managed change", "right managed change"):
        (project / "base.txt").write_text(content)
        tips.append(lineage.capture_authoritative_snapshot(
            project, snapshots, blobs, binding, previous=manual, purpose="effect"
        ))
    assert lineage.merge_lineage_snapshots(tips, snapshots, binding) == manual
