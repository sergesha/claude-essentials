from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def blob_store(tmp_path):
    from lockstep.runtime.blobs import BlobStore

    return BlobStore(tmp_path / "owner-state")


def test_blob_is_sha256_addressed_and_duplicate_put_is_immutable(blob_store):
    payload = b"immutable bytes\n"
    expected = hashlib.sha256(payload).hexdigest()

    first = blob_store.put(payload)
    second = blob_store.put(payload)

    assert first == second
    assert first.sha256 == expected
    assert first.size == len(payload)
    assert blob_store.read(first) == payload


def test_new_blob_fsyncs_containing_directory_after_publish(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lockstep.runtime.blobs as blobs

    observed = []
    monkeypatch.setattr(blobs, "fsync_owner_directory", observed.append)
    store = blobs.BlobStore(tmp_path / "owner-state")
    ref = store.put(b"durable")

    assert observed == [store.path_for(ref).parent]


def test_blob_put_rejects_expected_digest_mismatch(blob_store):
    from lockstep.runtime.blobs import DigestMismatch

    with pytest.raises(DigestMismatch):
        blob_store.put(b"actual", expected_sha256="0" * 64)


def test_blob_read_rejects_stored_digest_mismatch(blob_store):
    from lockstep.runtime.blobs import DigestMismatch

    ref = blob_store.put(b"original")
    blob_store.path_for(ref).chmod(0o600)
    blob_store.path_for(ref).write_bytes(b"tampered")

    with pytest.raises(DigestMismatch):
        blob_store.read(ref)


def test_blob_read_rejects_symlink_storage(blob_store, tmp_path):
    from lockstep.runtime.blobs import BlobStorageError

    payload = b"outside bytes"
    ref = blob_store.put(payload)
    path = blob_store.path_for(ref)
    outside = tmp_path / "outside"
    outside.write_bytes(payload)
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(BlobStorageError):
        blob_store.read(ref)


def test_concurrent_duplicate_blob_puts_publish_one_value(blob_store):
    payload = b"same content" * 100
    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(lambda _: blob_store.put(payload), range(24)))

    assert len(set(refs)) == 1
    assert blob_store.read(refs[0]) == payload


def test_blob_limit_rejects_before_publication(tmp_path):
    from lockstep.runtime.blobs import BlobLimits, BlobStore, StorageLimitExceeded

    store = BlobStore(tmp_path / "owner-state", limits=BlobLimits(max_bytes=4))

    with pytest.raises(StorageLimitExceeded, match="blob"):
        store.put(b"12345")
    assert not list((tmp_path / "owner-state" / "blobs" / "sha256").rglob("[0-9a-f]" * 64))


def test_blob_state_paths_are_owner_only(blob_store):
    ref = blob_store.put(b"private")

    path = blob_store.path_for(ref)
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0


def test_blob_store_rejects_insecure_existing_state_root(tmp_path):
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.owner_state import InsecureStatePath

    root = tmp_path / "owner-state"
    root.mkdir(mode=0o755)

    with pytest.raises(InsecureStatePath, match="owner-only"):
        BlobStore(root)
