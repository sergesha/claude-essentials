"""Read-only projections over durable effect-ledger facts."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import and_, func, or_, select

from lockstep.runtime.effects._ledger_records import (
    EffectRecord,
    RunDriveWatch,
    _dump,
    _load,
    _nonempty,
)
from lockstep.runtime.effects.descriptors import (
    parse_acceptance_result,
    parse_effect_result,
    parse_scope_result,
)
from lockstep.runtime.effects.models import (
    AcceptanceResult,
    EffectResult,
    ScopeResult,
)
from lockstep.runtime.native_models import NativeCoordinate


class _EffectLedgerQueries:
    """Read-only effect and run-drive projections for the ledger facade."""

    @staticmethod
    def _run_drive_watch(row) -> RunDriveWatch:
        admitted_at = _load(row.admitted_at)
        assert admitted_at is not None
        return RunDriveWatch(
            row.admission_seq,
            row.public_run_id,
            row.input_blob_sha256,
            row.input_blob_size,
            admitted_at,
        )

    def max_run_drive_admission_seq(self) -> int | None:
        table = self._store.tables.run_drive_watches
        with self._store.read_connection() as connection:
            return connection.execute(
                select(func.max(table.c.admission_seq))
            ).scalar_one()

    def list_run_drive_watches(
        self,
        *,
        after_admission_seq: int,
        high_water: int,
        limit: int,
    ) -> tuple[RunDriveWatch, ...]:
        if (
            type(after_admission_seq) is not int
            or type(high_water) is not int
            or not 0 <= after_admission_seq <= high_water
        ):
            raise ValueError(
                "run-drive watch bounds must be integers with "
                "0 <= after_admission_seq <= high_water"
            )
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError(
                "run-drive watch limit must be an integer from 1 to 128"
            )
        table = self._store.tables.run_drive_watches
        with self._store.read_connection() as connection:
            rows = connection.execute(
                select(table)
                .where(
                    table.c.admission_seq > after_admission_seq,
                    table.c.admission_seq <= high_water,
                )
                .order_by(table.c.admission_seq)
                .limit(limit)
            ).all()
        return tuple(self._run_drive_watch(row) for row in rows)

    def list_run_drive_watches_by_public_run_ids(
        self, public_run_ids: tuple[str, ...]
    ) -> tuple[RunDriveWatch, ...]:
        if (
            type(public_run_ids) is not tuple
            or not 1 <= len(public_run_ids) <= 128
            or any(type(value) is not str or not value for value in public_run_ids)
            or public_run_ids != tuple(sorted(set(public_run_ids)))
        ):
            raise ValueError(
                "run-drive watch IDs must be a sorted unique tuple of 1 to 128 "
                "non-empty strings"
            )
        table = self._store.tables.run_drive_watches
        with self._store.read_connection() as connection:
            rows = connection.execute(
                select(table)
                .where(table.c.public_run_id.in_(public_run_ids))
                .order_by(table.c.admission_seq)
            ).all()
        return tuple(self._run_drive_watch(row) for row in rows)

    def _result_for(
        self, connection, effect_id: str
    ) -> EffectResult | ScopeResult | AcceptanceResult | None:
        observations = self._store.tables.effect_observations
        row = connection.execute(
            select(observations.c.result_json)
            .where(
                and_(
                    observations.c.effect_id == effect_id,
                    observations.c.result_json.is_not(None),
                )
            )
            .order_by(observations.c.revision.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        value = json.loads(row.result_json)
        if value.get("schema") == "lockstep.scope-result/v1":
            return parse_scope_result(value)
        if value.get("schema") == "lockstep.acceptance-result/v1":
            return parse_acceptance_result(value)
        return parse_effect_result(value)

    def _from_row(self, connection, row) -> EffectRecord:
        values = row._mapping
        return EffectRecord(
            effect_id=values["effect_id"],
            coordinate=NativeCoordinate(
                thread_id=values["thread_id"],
                checkpoint_id=values["checkpoint_id"],
                checkpoint_ns=values["checkpoint_ns"],
                task_id=values["task_id"],
                interrupt_id=values["interrupt_id"],
            ),
            descriptor_digest=values["descriptor_digest"],
            effect_kind=values["effect_kind"],
            deadline_at=_load(values["deadline_at"]),
            phase=values["phase"],
            lease_epoch=int(values["lease_epoch"]),
            runner_binding_digest=values["runner_binding_digest"],
            workspace_ref=values["workspace_ref"],
            request_digest=values["request_digest"],
            grant_digest=values["grant_digest"],
            launch_commitment_digest=values["launch_commitment_digest"],
            result_ref=values["result_ref"],
            fixed_error_code=values["fixed_error_code"],
            created_at=_load(values["created_at"]),
            updated_at=_load(values["updated_at"]),
            revision=int(values["revision"]),
            result=self._result_for(connection, values["effect_id"]),
        )

    def get(self, effect_id: str) -> EffectRecord:
        table = self._store.tables.effects
        with self._store.read_connection() as connection:
            row = connection.execute(
                select(table).where(table.c.effect_id == effect_id)
            ).first()
            if row is None:
                raise KeyError(effect_id)
            return self._from_row(connection, row)

    def list_for_thread(
        self, thread_id: str, *, limit: int = 10_000
    ) -> tuple[EffectRecord, ...]:
        """Bounded read-only observation of durable effect facts."""

        _nonempty(thread_id, "effect thread_id")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("effect observation limit must be from 1 to 10000")
        table = self._store.tables.effects
        with self._store.read_connection() as connection:
            rows = connection.execute(
                select(table)
                .where(table.c.thread_id == thread_id)
                .order_by(table.c.created_at, table.c.effect_id)
                .limit(limit + 1)
            ).all()
            if len(rows) > limit:
                raise ValueError("effect observations exceed public bound")
            return tuple(self._from_row(connection, row) for row in rows)

    def list_nonterminal(self, *, limit: int | None = None) -> list[EffectRecord]:
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("nonterminal-effect limit must be a positive integer")
        table = self._store.tables.effects
        statement = (
            select(table)
            .where(table.c.phase.not_in({"delivered"}))
            .order_by(table.c.deadline_at, table.c.effect_id)
        )
        if limit is not None:
            statement = statement.limit(limit)
        with self._store.read_connection() as connection:
            rows = connection.execute(statement).all()
            return [self._from_row(connection, row) for row in rows]

    def list_nonterminal_for_thread(
        self, thread_id: str, *, limit: int
    ) -> list[EffectRecord]:
        if not thread_id:
            raise ValueError("effect thread_id must not be empty")
        if type(limit) is not int or limit <= 0:
            raise ValueError("nonterminal-effect limit must be a positive integer")
        table = self._store.tables.effects
        with self._store.read_connection() as connection:
            rows = connection.execute(
                select(table)
                .where(
                    and_(
                        table.c.thread_id == thread_id,
                        table.c.phase.not_in({"delivered"}),
                    )
                )
                .order_by(table.c.deadline_at, table.c.effect_id)
                .limit(limit)
            ).all()
            return [self._from_row(connection, row) for row in rows]

    def list_recovery_threads(
        self, *, limit: int, after_thread_id: str | None = None
    ) -> tuple[str, ...]:
        """Return a hard-bounded owner recovery queue, excluding parked humans."""

        if type(limit) is not int or limit <= 0:
            raise ValueError("recovery-effect limit must be a positive integer")
        table = self._store.tables.effects
        condition = and_(
            table.c.phase.not_in({"delivered"}),
            or_(
                table.c.effect_kind != "manual",
                table.c.phase != "prepared",
            ),
        )
        if after_thread_id is not None:
            _nonempty(after_thread_id, "recovery cursor")
            condition = and_(condition, table.c.thread_id > after_thread_id)
        with self._store.read_connection() as connection:
            rows = connection.execute(
                select(table.c.thread_id)
                .where(condition)
                .distinct()
                .order_by(table.c.thread_id)
                .limit(limit)
            ).all()
        return tuple(row.thread_id for row in rows)

    def list_due(self, now: datetime, *, limit: int) -> list[EffectRecord]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("due-effect limit must be a positive integer")
        table = self._store.tables.effects
        with self._store.read_connection() as connection:
            rows = connection.execute(
                select(table)
                .where(
                    and_(
                        table.c.phase.in_(("prepared", "launching", "running")),
                        table.c.deadline_at.is_not(None),
                        table.c.deadline_at <= _dump(now),
                    )
                )
                .order_by(table.c.deadline_at, table.c.effect_id)
                .limit(limit)
            ).all()
            return [self._from_row(connection, row) for row in rows]

    def next_deadline(self) -> datetime | None:
        table = self._store.tables.effects
        with self._store.read_connection() as connection:
            row = connection.execute(
                select(table.c.deadline_at)
                .where(
                    and_(
                        table.c.phase.in_(("prepared", "launching", "running")),
                        table.c.deadline_at.is_not(None),
                    )
                )
                .order_by(table.c.deadline_at, table.c.effect_id)
                .limit(1)
            ).first()
        return None if row is None else _load(row.deadline_at)
