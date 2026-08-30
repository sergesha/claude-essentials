"""Behavior freeze for snapshot lineage, capture, and append-only facts."""

from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import pytest

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.project_snapshots import ProjectSnapshotRef, ProjectSnapshotStore
from lockstep.runtime.snapshot_resolver import (
    RuntimeSnapshotConflict,
    RuntimeSnapshotFacts,
    _read_regular,
    capture_authoritative_snapshot,
    resolve_lineage_snapshot,
)
from lockstep.runtime.storage import SQLiteStore


class _LineageStore:
    def __init__(self, previous: dict[ProjectSnapshotRef, ProjectSnapshotRef | None]):
        self._previous = previous

    def read(self, ref: ProjectSnapshotRef) -> SimpleNamespace:
        return SimpleNamespace(previous=self._previous[ref])


def _binding(project, *, public_run_id: str = "run-1") -> RunBinding:
    return RunBinding(
        public_run_id,
        "thread-1",
        "a" * 64,
        "bundle:" + "b" * 64,
        str(project.resolve()),
    )


def test_lineage_rejects_cycles_and_the_public_depth_bound() -> None:
    first = ProjectSnapshotRef("1" * 64)
    second = ProjectSnapshotRef("2" * 64)
    with pytest.raises(RuntimeSnapshotConflict, match="contains a cycle"):
        resolve_lineage_snapshot(
            (first,), _LineageStore({first: second, second: first})
        )

    refs = tuple(ProjectSnapshotRef(f"{index:064x}") for index in range(10_001))
    previous = {ref: refs[index + 1] for index, ref in enumerate(refs[:-1])}
    previous[refs[-1]] = None
    assert resolve_lineage_snapshot((refs[1],), _LineageStore(previous)) == refs[1]
    with pytest.raises(RuntimeSnapshotConflict, match="exceeds public bound"):
        resolve_lineage_snapshot((refs[0],), _LineageStore(previous))


def test_regular_snapshot_read_rejects_size_digest_and_toctou_drift(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "input.txt"
    data = b"snapshot"
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()

    with pytest.raises(RuntimeSnapshotConflict, match="is not admissible"):
        _read_regular(path, digest, len(data) - 1)
    with pytest.raises(RuntimeSnapshotConflict, match="changed while reading"):
        _read_regular(path, "0" * 64, len(data))

    real_fstat = os.fstat
    calls = 0

    def drifting_fstat(descriptor):
        nonlocal calls
        calls += 1
        observed = real_fstat(descriptor)
        if calls == 1:
            return observed
        return SimpleNamespace(
            st_dev=observed.st_dev,
            st_ino=observed.st_ino,
            st_mode=observed.st_mode,
            st_size=observed.st_size,
            st_mtime_ns=observed.st_mtime_ns + 1,
        )

    monkeypatch.setattr(os, "fstat", drifting_fstat)
    with pytest.raises(RuntimeSnapshotConflict, match="changed while reading"):
        _read_regular(path, digest, len(data))


def test_authoritative_capture_rejects_project_symlinks(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "target.txt"
    target.write_text("content", encoding="utf-8")
    (project / "linked.txt").symlink_to(target.name)
    blobs = BlobStore(tmp_path / "owner")
    snapshots = ProjectSnapshotStore(tmp_path / "owner", blobs)

    with pytest.raises(RuntimeSnapshotConflict, match="reject symlinks"):
        capture_authoritative_snapshot(
            project,
            snapshots,
            blobs,
            _binding(project),
            previous=None,
            purpose="run-start",
        )


def test_snapshot_facts_are_idempotent_and_reject_rebinding(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "runtime.sqlite3")
    facts = RuntimeSnapshotFacts(store)
    binding = _binding(tmp_path / "project")
    coordinate = NativeCoordinate(
        thread_id=binding.thread_id,
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
        task_id="task-1",
        interrupt_id="interrupt-1",
    )
    first = ProjectSnapshotRef("1" * 64)
    second = ProjectSnapshotRef("2" * 64)
    try:
        with store.write_transaction() as connection:
            facts.bind_run_start_in_transaction(connection, binding, first)
            facts.bind_run_start_in_transaction(connection, binding, first)
            with pytest.raises(RuntimeSnapshotConflict, match="already bound differently"):
                facts.bind_run_start_in_transaction(connection, binding, second)
        assert facts.run_start(binding) == first

        assert facts.bind_effect(
            "effect-1", "current_project_snapshot", binding, coordinate, "3" * 64, first
        ) == first
        assert facts.bind_effect(
            "effect-1", "current_project_snapshot", binding, coordinate, "3" * 64, first
        ) == first
        with pytest.raises(RuntimeSnapshotConflict, match="already bound differently"):
            facts.bind_effect(
                "effect-1",
                "current_project_snapshot",
                binding,
                coordinate,
                "3" * 64,
                second,
            )
        assert facts.get_effect(
            "effect-1", "current_project_snapshot"
        ).snapshot_ref == first
    finally:
        store.close()
