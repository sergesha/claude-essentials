from __future__ import annotations

import json

import pytest


class PoisonAfter:
    def __init__(self, values):
        self._values = values

    def __iter__(self):
        yield from self._values
        raise AssertionError("consumer read past max+1")


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


def test_new_snapshot_fsyncs_containing_directory_after_publish(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.runtime.blobs import BlobStore
    import lockstep.runtime.project_snapshots as snapshots

    owner = tmp_path / "owner-state"
    blobs = BlobStore(owner)
    observed = []
    monkeypatch.setattr(snapshots, "fsync_owner_directory", observed.append)
    store = snapshots.ProjectSnapshotStore(owner, blobs)
    ref = store.capture(
        {"file": blobs.put(b"durable")},
        declared_paths=("file",),
        provenance={"source": "test"},
    )

    assert observed == [store.manifest_path(ref).parent]


def test_snapshot_provenance_rejects_top_level_mutation(stores):
    blob_store, snapshot_store = stores
    ref = snapshot_store.capture(
        {"app.py": blob_store.put(b"v1")},
        declared_paths=["app.py"],
        provenance={"provider": "memory", "revision": "1"},
    )
    provenance = snapshot_store.read(ref).provenance

    with pytest.raises(TypeError):
        provenance["revision"] = "2"


def test_snapshot_provenance_is_not_targetable_by_dict_setitem(stores):
    blob_store, snapshot_store = stores
    ref = snapshot_store.capture(
        {"app.py": blob_store.put(b"v1")},
        declared_paths=["app.py"],
        provenance={"revision": "1"},
    )
    provenance = snapshot_store.read(ref).provenance

    assert not isinstance(provenance, dict)
    with pytest.raises(TypeError):
        dict.__setitem__(provenance, "revision", "2")
    assert provenance == {"revision": "1"}


def test_snapshot_provenance_is_not_targetable_by_dict_update(stores):
    blob_store, snapshot_store = stores
    ref = snapshot_store.capture(
        {"app.py": blob_store.put(b"v1")},
        declared_paths=["app.py"],
        provenance={"revision": "1"},
    )
    provenance = snapshot_store.read(ref).provenance

    assert not isinstance(provenance, dict)
    with pytest.raises(TypeError):
        dict.update(provenance, {"revision": "2"})
    assert provenance == {"revision": "1"}


def test_snapshot_provenance_exposes_no_mutable_instance_backing(stores):
    blob_store, snapshot_store = stores
    ref = snapshot_store.capture(
        {"app.py": blob_store.put(b"v1")},
        declared_paths=["app.py"],
        provenance={"revision": "1"},
    )
    provenance = snapshot_store.read(ref).provenance

    with pytest.raises(TypeError):
        vars(provenance)
    with pytest.raises(AttributeError):
        provenance._sealed = False
    assert provenance == {"revision": "1"}


def test_snapshot_provenance_recursively_freezes_nested_json(stores):
    blob_store, snapshot_store = stores
    original = {
        "provider": "memory",
        "source": {"revision": "1", "labels": ["reviewed", "sealed"]},
    }
    ref = snapshot_store.capture(
        {"app.py": blob_store.put(b"v1")},
        declared_paths=["app.py"],
        provenance=original,
    )
    provenance = snapshot_store.read(ref).provenance

    assert provenance == original
    with pytest.raises(TypeError):
        provenance["source"]["revision"] = "2"
    assert not isinstance(provenance["source"], dict)
    with pytest.raises(TypeError):
        dict.__setitem__(provenance["source"], "revision", "2")
    with pytest.raises((AttributeError, TypeError)):
        provenance["source"]["labels"].append("mutated")
    assert provenance == original


def test_snapshot_frozen_provenance_round_trips_deterministically(stores):
    blob_store, snapshot_store = stores
    original = {"source": {"revision": "1", "labels": ["sealed"]}}
    files = {"app.py": blob_store.put(b"v1")}
    ref = snapshot_store.capture(
        files,
        declared_paths=["app.py"],
        provenance=original,
    )
    snapshot = snapshot_store.read(ref)

    reused = snapshot_store.capture(
        files,
        declared_paths=["app.py"],
        provenance=snapshot.provenance,
    )

    assert reused == ref
    assert snapshot_store.read(reused).provenance == original


def test_snapshot_rejects_undeclared_or_unsafe_paths(stores):
    from lockstep.runtime.project_snapshots import (
        UndeclaredSnapshotPath,
        UnsafeSnapshotPath,
    )

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
    blob_store, snapshot_store = stores
    blob = blob_store.put(b"content")
    with pytest.raises(ValueError):
        snapshot_store.capture(
            [("app.py", blob), ("./app.py", blob)],
            declared_paths=["app.py"],
            provenance={"provider": "memory"},
        )
    with pytest.raises(ValueError):
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


def _replace_manifest_with_symlink(path, outside):
    path.rename(outside)
    try:
        path.symlink_to(outside)
    except OSError:
        outside.rename(path)
        pytest.skip("symlinks unavailable")


def test_snapshot_read_rejects_symlink_backed_manifest(stores, tmp_path):
    blob_store, snapshot_store = stores
    ref = snapshot_store.capture(
        {"app.py": blob_store.put(b"v1")},
        declared_paths=["app.py"],
        provenance={"revision": "1"},
    )
    _replace_manifest_with_symlink(
        snapshot_store.manifest_path(ref), tmp_path / "outside-snapshot-manifest.json"
    )

    with pytest.raises(RuntimeError, match="symlink"):
        snapshot_store.read(ref)


def test_snapshot_reuse_rejects_symlink_backed_manifest(stores, tmp_path):
    blob_store, snapshot_store = stores
    files = {"app.py": blob_store.put(b"v1")}
    ref = snapshot_store.capture(
        files,
        declared_paths=["app.py"],
        provenance={"revision": "1"},
    )
    _replace_manifest_with_symlink(
        snapshot_store.manifest_path(ref), tmp_path / "outside-snapshot-manifest.json"
    )

    with pytest.raises(RuntimeError, match="symlink"):
        snapshot_store.capture(
            files,
            declared_paths=["app.py"],
            provenance={"revision": "1"},
        )


def test_snapshot_limits_fail_before_manifest_publication(tmp_path):
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.project_snapshots import (
        ProjectSnapshotStore,
        SnapshotLimits,
        StorageLimitExceeded,
    )

    owner = tmp_path / "limited-state"
    blobs = BlobStore(owner)
    blob = blobs.put(b"content")
    store = ProjectSnapshotStore(
        owner,
        blobs,
        limits=SnapshotLimits(
            max_files=1,
            max_total_bytes=1024,
            max_manifest_bytes=1024,
            max_provenance_bytes=12,
            max_provenance_depth=2,
        ),
    )

    with pytest.raises(StorageLimitExceeded, match="provenance"):
        store.capture(
            {"app.py": blob},
            declared_paths=["app.py"],
            provenance={"provider": {"nested": "too large"}},
        )
    assert not list((owner / "project-snapshots").glob("*.json"))


def test_snapshot_read_enforces_manifest_limit(tmp_path):
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.project_snapshots import (
        ProjectSnapshotStore,
        SnapshotLimits,
        StorageLimitExceeded,
    )

    owner = tmp_path / "limited-read-state"
    blobs = BlobStore(owner)
    permissive = ProjectSnapshotStore(owner, blobs)
    ref = permissive.capture(
        {"app.py": blobs.put(b"content")},
        declared_paths=["app.py"],
        provenance={"provider": "memory"},
    )
    strict = ProjectSnapshotStore(
        owner,
        blobs,
        limits=SnapshotLimits(max_manifest_bytes=8),
    )

    with pytest.raises(StorageLimitExceeded, match="manifest"):
        strict.read(ref)


def test_snapshot_rejects_deep_provenance_before_recursive_freezing(tmp_path):
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.project_snapshots import (
        ProjectSnapshotStore,
        SnapshotLimits,
        StorageLimitExceeded,
    )

    owner = tmp_path / "deep-provenance-state"
    blobs = BlobStore(owner)
    store = ProjectSnapshotStore(
        owner,
        blobs,
        limits=SnapshotLimits(max_provenance_depth=8),
    )
    provenance = {}
    current = provenance
    for _ in range(2_000):
        child = {}
        current["child"] = child
        current = child

    with pytest.raises(StorageLimitExceeded, match="depth"):
        store.capture(
            {"app.py": blobs.put(b"content")},
            declared_paths=["app.py"],
            provenance=provenance,
        )


def test_snapshot_file_limit_consumes_at_most_max_plus_one(tmp_path):
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.project_snapshots import (
        ProjectSnapshotStore,
        SnapshotLimits,
        StorageLimitExceeded,
    )

    owner = tmp_path / "bounded-files-state"
    blobs = BlobStore(owner)
    blob = blobs.put(b"content")
    store = ProjectSnapshotStore(owner, blobs, limits=SnapshotLimits(max_files=2))
    files = PoisonAfter([("a", blob), ("b", blob), ("c", blob)])

    with pytest.raises(StorageLimitExceeded, match="files"):
        store.capture(files, declared_paths=["a", "b"], provenance={})


def test_snapshot_declaration_limit_consumes_at_most_max_plus_one(tmp_path):
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.project_snapshots import (
        ProjectSnapshotStore,
        SnapshotLimits,
        StorageLimitExceeded,
    )

    owner = tmp_path / "bounded-declarations-state"
    blobs = BlobStore(owner)
    blob = blobs.put(b"content")
    store = ProjectSnapshotStore(
        owner,
        blobs,
        limits=SnapshotLimits(max_declared_paths=2),
    )
    declarations = PoisonAfter(["a", "b", "c"])

    with pytest.raises(StorageLimitExceeded, match="declared paths"):
        store.capture({"a": blob}, declared_paths=declarations, provenance={})


def test_snapshot_wide_provenance_rejects_before_freeze_or_encode(
    tmp_path, monkeypatch
):
    from lockstep.runtime import project_snapshots
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.project_snapshots import (
        ProjectSnapshotStore,
        SnapshotLimits,
        StorageLimitExceeded,
    )

    owner = tmp_path / "wide-provenance-state"
    blobs = BlobStore(owner)
    store = ProjectSnapshotStore(
        owner,
        blobs,
        limits=SnapshotLimits(max_provenance_items=2),
    )

    def fail_freeze(_value):
        raise AssertionError("freeze reached")

    monkeypatch.setattr(project_snapshots, "_freeze_json", fail_freeze)

    with pytest.raises(StorageLimitExceeded, match="items"):
        store.capture(
            {"app.py": blobs.put(b"content")},
            declared_paths=["app.py"],
            provenance={"a": 1, "b": 2, "c": 3},
        )


def test_snapshot_provenance_node_and_scalar_budgets_reject_early(tmp_path):
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.project_snapshots import (
        ProjectSnapshotStore,
        SnapshotLimits,
        StorageLimitExceeded,
    )

    owner = tmp_path / "provenance-budget-state"
    blobs = BlobStore(owner)
    blob = blobs.put(b"content")
    node_limited = ProjectSnapshotStore(
        owner,
        blobs,
        limits=SnapshotLimits(max_provenance_nodes=2),
    )
    with pytest.raises(StorageLimitExceeded, match="nodes"):
        node_limited.capture(
            {"app.py": blob},
            declared_paths=["app.py"],
            provenance={"a": [1]},
        )

    scalar_limited = ProjectSnapshotStore(
        owner,
        blobs,
        limits=SnapshotLimits(max_provenance_scalar_bytes=4),
    )
    with pytest.raises(StorageLimitExceeded, match="scalar bytes"):
        scalar_limited.capture(
            {"app.py": blob},
            declared_paths=["app.py"],
            provenance={"source": "too-large"},
        )


@pytest.mark.parametrize("path", ["CON", "src/name:stream"])
def test_snapshot_rejects_paths_unusable_by_workspace_materialization(tmp_path, path):
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.project_snapshots import ProjectSnapshotStore

    owner = tmp_path / "portable-path-state"
    blobs = BlobStore(owner)
    store = ProjectSnapshotStore(owner, blobs)

    with pytest.raises(ValueError, match="path|alias|portable"):
        store.capture(
            {path: blobs.put(b"content")},
            declared_paths=[path],
            provenance={},
        )


@pytest.mark.parametrize(
    "paths",
    [
        ("src/Readme", "src/README"),
        ("src/\N{LATIN SMALL LETTER E WITH ACUTE}.txt", "src/e\N{COMBINING ACUTE ACCENT}.txt"),
        ("node", "node/child.txt"),
    ],
)
def test_snapshot_rejects_portable_collisions_and_file_descendants(tmp_path, paths):
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.project_snapshots import ProjectSnapshotStore

    owner = tmp_path / "portable-collision-state"
    blobs = BlobStore(owner)
    store = ProjectSnapshotStore(owner, blobs)
    blob = blobs.put(b"content")

    with pytest.raises(ValueError, match="collision|descendant|path"):
        store.capture(
            [(path, blob) for path in paths],
            declared_paths=paths,
            provenance={},
        )


def test_snapshot_entry_limit_counts_implicit_directories(tmp_path):
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.project_snapshots import (
        ProjectSnapshotStore,
        SnapshotLimits,
        StorageLimitExceeded,
    )

    owner = tmp_path / "tree-entry-limit-state"
    blobs = BlobStore(owner)
    store = ProjectSnapshotStore(owner, blobs, limits=SnapshotLimits(max_entries=2))

    with pytest.raises(StorageLimitExceeded, match="entries"):
        store.capture(
            {"one/two/file.txt": blobs.put(b"content")},
            declared_paths=["one/"],
            provenance={},
        )
