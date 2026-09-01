"""Short-lived ownership leases with monotonically increasing fence epochs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select, update

from lockstep.runtime.storage import SQLiteStore

LEASE_SCOPES = frozenset({"invoke", "effect", "session", "publication"})


class LeaseUnavailable(RuntimeError):
    """A different owner still holds an unexpired lease."""


@dataclass(frozen=True)
class Lease:
    scope: str
    key: str
    owner: str
    epoch: int
    expires_at: datetime


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dump(value: datetime) -> str:
    return _utc(value).isoformat()


def _load(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _seconds(ttl: float | timedelta) -> float:
    value = ttl.total_seconds() if isinstance(ttl, timedelta) else float(ttl)
    if value <= 0:
        raise ValueError("ttl must be positive")
    return value


class LeaseStore:
    """Coordinates inspectors; possession never conveys launch permission."""

    def __init__(self, store: SQLiteStore, *, clock: Callable[[], datetime] | None = None) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _validate_identity(scope: str, key: str, owner: str) -> None:
        if scope not in LEASE_SCOPES:
            raise ValueError(f"unknown lease scope {scope!r}; expected one of {sorted(LEASE_SCOPES)}")
        if not key:
            raise ValueError("lease key must not be empty")
        if not owner:
            raise ValueError("lease owner must not be empty")

    def acquire(self, scope: str, key: str, owner: str, ttl: float | timedelta) -> Lease:
        self._validate_identity(scope, key, owner)
        duration = _seconds(ttl)
        now = _utc(self._clock())
        expires_at = now + timedelta(seconds=duration)
        table = self._store.tables.leases
        with self._store._v2_write_transaction() as connection:
            row = connection.execute(
                select(table).where(and_(table.c.scope == scope, table.c.lease_key == key))
            ).first()
            if row is None:
                epoch = 1
                connection.execute(
                    table.insert().values(
                        scope=scope,
                        lease_key=key,
                        owner=owner,
                        epoch=epoch,
                        expires_at=_dump(expires_at),
                        acquired_at=_dump(now),
                    )
                )
            else:
                current = row._mapping
                current_expiry = _load(current["expires_at"])
                if current_expiry > now and current["owner"] != owner:
                    raise LeaseUnavailable(
                        f"{scope} lease {key!r} is held by another owner through {current_expiry.isoformat()}"
                    )
                epoch = int(current["epoch"])
                if current_expiry <= now:
                    epoch += 1
                connection.execute(
                    update(table)
                    .where(and_(table.c.scope == scope, table.c.lease_key == key))
                    .values(
                        owner=owner,
                        epoch=epoch,
                        expires_at=_dump(expires_at),
                        acquired_at=_dump(now),
                    )
                )
        return Lease(scope=scope, key=key, owner=owner, epoch=epoch, expires_at=expires_at)

    def is_current(self, lease: Lease) -> bool:
        now = _utc(self._clock())
        table = self._store.tables.leases
        with self._store.read_connection() as connection:
            row = connection.execute(
                select(table.c.owner, table.c.epoch, table.c.expires_at).where(
                    and_(table.c.scope == lease.scope, table.c.lease_key == lease.key)
                )
            ).first()
        return bool(
            row is not None
            and row.owner == lease.owner
            and row.epoch == lease.epoch
            and _load(row.expires_at) > now
        )

    def release(self, lease: Lease) -> bool:
        """Expire a live epoch without deleting its monotonic fence history."""

        now = _utc(self._clock())
        table = self._store.tables.leases
        with self._store._v2_write_transaction() as connection:
            result = connection.execute(
                update(table)
                .where(
                    and_(
                        table.c.scope == lease.scope,
                        table.c.lease_key == lease.key,
                        table.c.owner == lease.owner,
                        table.c.epoch == lease.epoch,
                        table.c.expires_at > _dump(now),
                    )
                )
                .values(expires_at=_dump(now))
            )
        return result.rowcount == 1
