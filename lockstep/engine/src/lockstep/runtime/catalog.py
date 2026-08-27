"""Immutable run discovery bindings.

Workflow progress is intentionally absent.  Status, current tasks, interrupts,
and terminal outcome are projections of public LangGraph snapshots.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import select

from lockstep.runtime.storage import SQLiteStore


class ImmutableBindingConflict(RuntimeError):
    """An immutable public-run or thread identity is already bound differently."""


@dataclass(frozen=True)
class RunBinding:
    public_run_id: str
    thread_id: str
    recipe_digest: str
    recipe_snapshot_ref: str
    project_identity: str
    created_at: str | None = None


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _canonical_created_at(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("created_at must include a timezone")
    return _iso_utc(parsed)


class RunCatalog:
    """Create and discover immutable run bindings.  There is no update API."""

    def __init__(
        self, store: SQLiteStore, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _validate(binding: RunBinding) -> None:
        for field in (
            "public_run_id",
            "thread_id",
            "recipe_digest",
            "recipe_snapshot_ref",
            "project_identity",
        ):
            if not getattr(binding, field):
                raise ValueError(f"{field} must not be empty")
        digest = binding.recipe_digest
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("recipe_digest must be a lowercase SHA-256 digest")

    @staticmethod
    def _from_row(row) -> RunBinding:
        return RunBinding(**dict(row._mapping))

    @staticmethod
    def _same_requested(existing: RunBinding, requested: RunBinding) -> bool:
        expected = requested
        if requested.created_at is None:
            expected = replace(requested, created_at=existing.created_at)
        return existing == expected

    def create(self, binding: RunBinding) -> RunBinding:
        with self._store.write_transaction() as connection:
            return self.create_in_transaction(connection, binding)

    def create_in_transaction(self, connection, binding: RunBinding) -> RunBinding:
        """Create through an owner transaction shared with a start admission."""

        self._validate(binding)
        requested = (
            replace(binding, created_at=_canonical_created_at(binding.created_at))
            if binding.created_at is not None
            else binding
        )
        candidate = replace(
            requested,
            created_at=requested.created_at or _iso_utc(self._clock()),
        )
        table = self._store.tables.runs
        rows = connection.execute(
            select(table).where(
                (table.c.public_run_id == binding.public_run_id)
                | (table.c.thread_id == binding.thread_id)
            )
        ).all()
        if rows:
            matches = [self._from_row(row) for row in rows]
            if len(matches) == 1 and self._same_requested(matches[0], requested):
                return matches[0]
            raise ImmutableBindingConflict(
                "public_run_id or thread_id is already bound to different immutable data"
            )
        connection.execute(
            table.insert().values(
                public_run_id=candidate.public_run_id,
                thread_id=candidate.thread_id,
                recipe_digest=candidate.recipe_digest,
                recipe_snapshot_ref=candidate.recipe_snapshot_ref,
                project_identity=candidate.project_identity,
                created_at=candidate.created_at,
            )
        )
        return candidate

    def get(self, run_id: str) -> RunBinding:
        table = self._store.tables.runs
        with self._store.read_connection() as connection:
            row = connection.execute(
                select(table).where(table.c.public_run_id == run_id)
            ).first()
        if row is None:
            raise KeyError(run_id)
        return self._from_row(row)

    def find_by_thread(self, thread_id: str) -> RunBinding:
        table = self._store.tables.runs
        with self._store.read_connection() as connection:
            row = connection.execute(
                select(table).where(table.c.thread_id == thread_id)
            ).first()
        if row is None:
            raise KeyError(thread_id)
        return self._from_row(row)

    def list_after_public_run_id(
        self,
        after_public_run_id: str | None,
        *,
        limit: int,
    ) -> tuple[RunBinding, ...]:
        """Page immutable bindings for the one-time v2 schema backfill."""

        if after_public_run_id is not None and (
            type(after_public_run_id) is not str or not after_public_run_id
        ):
            raise ValueError("catalog page cursor must be a non-empty string")
        if type(limit) is not int or not 1 <= limit <= 129:
            raise ValueError("catalog migration page limit must be from 1 to 129")
        table = self._store.tables.runs
        statement = select(table).order_by(table.c.public_run_id).limit(limit)
        if after_public_run_id is not None:
            statement = statement.where(
                table.c.public_run_id > after_public_run_id
            )
        with self._store.read_connection() as connection:
            return tuple(
                self._from_row(row) for row in connection.execute(statement)
            )

    def list(
        self, project_identity: str, *, limit: int | None = None
    ) -> list[RunBinding]:
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10_000):
            raise ValueError("run catalog limit must be from 1 to 10000")
        table = self._store.tables.runs
        statement = (
            select(table)
            .where(table.c.project_identity == project_identity)
            .order_by(table.c.created_at, table.c.public_run_id)
        )
        if limit is not None:
            statement = statement.limit(limit)
        with self._store.read_connection() as connection:
            return [self._from_row(row) for row in connection.execute(statement)]
