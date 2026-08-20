from __future__ import annotations

import json

import pytest


@pytest.fixture
def stores(tmp_path):
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.project_snapshots import ProjectSnapshotStore

    blob_store = BlobStore(tmp_path / "owner-state")
    return blob_store, ProjectSnapshotStore(tmp_path / "owner-state", blob_store)


def test_snapshot_records_declared_paths_blob_refs_and_provenance(stores):
    blob_store, snapshot_store = stores
    app = blob_store.put(b"print('hello')\n")
    test = blob_store.put(b"def test_ok(): pass\n")

    ref = snapshot_store.capture(
        {"src/app.py": app, "tests/test_app.py": test},
        declared_paths=["src/", "tests/test_app.py"],
        provenance={"provider": "directory", "revision": "r1"},
    )
    snapshot = snapshot_store.read(ref)

    assert [entry.path for entry in snapshot.files] == ["src/app.py", "tests/test_app.py"]
    assert snapshot.files[0].blob == app
    assert snapshot.declared_paths == ("src/", "tests/test_app.py")
    assert snapshot.provenance == {"provider": "directory", "revision": "r1"}


def test_snapshot_rejects_undeclared_or_unsafe_paths(stores):
    from lockstep.runtime.project_snapshots import UndeclaredSnapshotPath, UnsafeSnapshotPath

    blob_store, snapshot_store = stores
    blob = blob_store.put(b"content")

    with pytest.raises(UndeclaredSnapshotPath):
        snapshot_store.capture(
            {"docs/readme.md": blob},
            declared_paths=["src/"],
            provenance={"provider": "directory"},
        )
    with pytest.raises(UnsafeSnapshotPath):
        snapshot_store.capture(
            {"../escape": blob},
            declared_paths=["../"],
            provenance={"provider": "directory"},
        )


def test_snapshot_rejects_duplicate_normalized_paths_and_declarations(stores):
    from lockstep.runtime.project_snapshots import DuplicateSnapshotPath

    blob_store, snapshot_store = stores
    blob = blob_store.put(b"content")
    with pytest.raises(DuplicateSnapshotPath):
        snapshot_store.capture(
            [("app.py", blob), ("./app.py", blob)],
            declared_paths=["app.py"],
            provenance={"provider": "memory"},
        )
    with pytest.raises(DuplicateSnapshotPath):
        snapshot_store.capture(
            [("app.py", blob)],
            declared_paths=["app.py", "./app.py"],
            provenance={"provider": "memory"},
        )


def test_snapshot_is_deterministic_and_reuses_identical_seal(stores):
    blob_store, snapshot_store = stores
    a = blob_store.put(b"a")
    b = blob_store.put(b"b")

    first = snapshot_store.capture(
        [("b.txt", b), ("a.txt", a)],
        declared_paths=["b.txt", "a.txt"],
        provenance={"revision": "r1", "provider": "memory"},
    )
    manifest_path = snapshot_store.manifest_path(first)
    before = manifest_path.stat().st_mtime_ns
    second = snapshot_store.capture(
        [("a.txt", a), ("b.txt", b)],
        declared_paths=["a.txt", "b.txt"],
        provenance={"provider": "memory", "revision": "r1"},
    )

    assert second == first
    assert manifest_path.stat().st_mtime_ns == before


def test_snapshot_rollover_binds_previous_sealed_snapshot(stores):
    blob_store, snapshot_store = stores
    first = snapshot_store.capture(
        {"app.py": blob_store.put(b"v1")},
        declared_paths=["app.py"],
        provenance={"revision": "1"},
    )
    second = snapshot_store.capture(
        {"app.py": blob_store.put(b"v2")},
        declared_paths=["app.py"],
        provenance={"revision": "2"},
        previous=first,
    )

    assert snapshot_store.read(second).previous == first


def test_snapshot_read_rejects_manifest_digest_mismatch(stores):
    from lockstep.runtime.project_snapshots import DigestMismatch

    blob_store, snapshot_store = stores
    ref = snapshot_store.capture(
        {"app.py": blob_store.put(b"v1")},
        declared_paths=["app.py"],
        provenance={"revision": "1"},
    )
    path = snapshot_store.manifest_path(ref)
    data = json.loads(path.read_text())
    data["provenance"]["revision"] = "tampered"
    path.chmod(0o600)
    path.write_text(json.dumps(data))

    with pytest.raises(DigestMismatch):
        snapshot_store.read(ref)


def test_snapshot_ref_cannot_escape_owner_state(stores):
    from lockstep.runtime.project_snapshots import ProjectSnapshotRef

    _blob_store, snapshot_store = stores
    with pytest.raises(ValueError):
        snapshot_store.manifest_path(ProjectSnapshotRef("../escape"))
