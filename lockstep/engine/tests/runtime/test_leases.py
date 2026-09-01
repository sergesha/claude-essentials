from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 20, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def lease_store(tmp_path):
    from lockstep.runtime.leases import LeaseStore
    from lockstep.runtime.storage import SQLiteStore

    clock = Clock()
    storage = SQLiteStore(tmp_path / "runtime.db")
    yield LeaseStore(storage, clock=clock), clock
    storage.close()


def test_lease_scopes_are_closed(lease_store):
    store, _clock = lease_store

    for scope in ("invoke", "effect", "session", "publication"):
        assert store.acquire(scope, "key", "owner", 30).scope == scope
    with pytest.raises(ValueError, match="scope"):
        store.acquire("workflow", "key", "owner", 30)


def test_live_lease_excludes_another_owner(lease_store):
    from lockstep.runtime.leases import LeaseUnavailable

    store, _clock = lease_store
    lease = store.acquire("effect", "effect-1", "worker-a", 30)

    with pytest.raises(LeaseUnavailable):
        store.acquire("effect", "effect-1", "worker-b", 30)
    assert store.is_current(lease)


def test_expiry_fences_stale_owner_with_a_higher_epoch(lease_store):
    store, clock = lease_store
    stale = store.acquire("invoke", "thread-1", "worker-a", timedelta(seconds=10))
    clock.now += timedelta(seconds=11)

    current = store.acquire("invoke", "thread-1", "worker-b", 10)

    assert current.epoch == stale.epoch + 1
    assert not store.is_current(stale)
    assert store.is_current(current)
    assert not store.release(stale)
    assert store.is_current(current)


def test_same_owner_renews_live_lease_without_changing_epoch(lease_store):
    store, clock = lease_store
    first = store.acquire("session", "interrupt-1", "worker-a", 10)
    clock.now += timedelta(seconds=5)
    renewed = store.acquire("session", "interrupt-1", "worker-a", 10)

    assert renewed.epoch == first.epoch
    assert renewed.expires_at > first.expires_at


def test_release_then_reacquire_preserves_monotonic_fence(lease_store):
    store, _clock = lease_store
    stale = store.acquire("invoke", "thread-aba", "worker-a", 30)

    assert store.release(stale)
    current = store.acquire("invoke", "thread-aba", "worker-a", 30)

    assert current.epoch == stale.epoch + 1
    assert not store.is_current(stale)
    assert not store.release(stale)
    assert store.is_current(current)


def test_lease_rejects_empty_identity_and_nonpositive_ttl(lease_store):
    store, _clock = lease_store

    with pytest.raises(ValueError):
        store.acquire("invoke", "", "owner", 10)
    with pytest.raises(ValueError):
        store.acquire("invoke", "key", "", 10)
    with pytest.raises(ValueError):
        store.acquire("invoke", "key", "owner", 0)
