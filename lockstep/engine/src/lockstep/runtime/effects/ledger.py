"""Durable external-attempt facts keyed by exact native coordinates."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from lockstep.runtime.blobs import BlobRef
from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_result,
    parse_acceptance_result,
    parse_scope_result,
)
from lockstep.runtime.effects.models import (
    AcceptDescriptor,
    AcceptanceResult,
    EffectDescriptor,
    EffectResult,
    ScopeDescriptor,
    ScopeResult,
    PublishDescriptor,
)
from lockstep.runtime.leases import Lease
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.storage import SQLiteStore

PRELAUNCH_ERROR_CODES = frozenset({"prelaunch_failed", "deadline_timeout"})


class EffectConflict(RuntimeError):
    """An immutable effect fact conflicts with an existing fact."""


class StaleEffectRevision(RuntimeError):
    """The caller lost the optimistic concurrency race."""


class IllegalEffectTransition(RuntimeError):
    """The requested phase edge is not part of the monotonic lifecycle."""


class StaleEffectLease(RuntimeError):
    """The supplied effect lease is not the current live fence."""


@dataclass(frozen=True)
class EffectDispatchWatch:
    """A process-neutral discovery outbox, never workflow status or authority."""

    public_run_id: str
    input_blob: BlobRef
    admitted_at: datetime


@dataclass(frozen=True, slots=True)
class RunDriveWatch:
    """Durable v2 discovery record without workflow or scheduling state."""

    admission_seq: int
    public_run_id: str
    input_blob_sha256: str | None
    input_blob_size: int | None
    admitted_at: datetime

    def __post_init__(self) -> None:
        if type(self.admission_seq) is not int or self.admission_seq <= 0:
            raise ValueError("admission_seq must be a positive integer")
        if type(self.public_run_id) is not str or not self.public_run_id:
            raise ValueError("public_run_id must be a non-empty string")
        if (self.input_blob_sha256 is None) != (self.input_blob_size is None):
            raise ValueError(
                "input blob digest and size must both be null or both be non-null"
            )
        if self.input_blob_sha256 is not None and (
            type(self.input_blob_sha256) is not str
            or len(self.input_blob_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.input_blob_sha256)
        ):
            raise ValueError(
                "input_blob_sha256 must be a lowercase SHA-256 digest"
            )
        if self.input_blob_size is not None and (
            type(self.input_blob_size) is not int or self.input_blob_size <= 0
        ):
            raise ValueError("input_blob_size must be a positive integer")
        if (
            not isinstance(self.admitted_at, datetime)
            or self.admitted_at.tzinfo is None
            or self.admitted_at.utcoffset() is None
        ):
            raise ValueError("admitted_at must be a timezone-aware datetime")
        object.__setattr__(self, "admitted_at", self.admitted_at.astimezone(UTC))


@dataclass(frozen=True)
class EffectRecord:
    effect_id: str
    coordinate: NativeCoordinate
    descriptor_digest: str
    effect_kind: str
    deadline_at: datetime | None
    phase: str
    lease_epoch: int
    runner_binding_digest: str | None
    workspace_ref: str | None
    request_digest: str | None
    grant_digest: str | None
    launch_commitment_digest: str | None
    result_ref: str | None
    fixed_error_code: str | None
    created_at: datetime
    updated_at: datetime
    revision: int
    result: EffectResult | ScopeResult | AcceptanceResult | None = None


@dataclass(frozen=True)
class _PreparedEffectFacts:
    effect_id: str
    coordinate: NativeCoordinate
    descriptor_digest: str
    effect_kind: str
    deadline_at: datetime | None
    runner_binding_digest: str | None
    workspace_ref: str | None
    request_digest: str | None
    grant_digest: str | None
    created_at: datetime

    def insert_values(self) -> dict[str, object]:
        timestamp = _dump(self.created_at)
        return {
            "effect_id": self.effect_id,
            "thread_id": self.coordinate.thread_id,
            "checkpoint_ns": self.coordinate.checkpoint_ns,
            "checkpoint_id": self.coordinate.checkpoint_id,
            "task_id": self.coordinate.task_id,
            "interrupt_id": self.coordinate.interrupt_id,
            "descriptor_digest": self.descriptor_digest,
            "effect_kind": self.effect_kind,
            "deadline_at": None if self.deadline_at is None else _dump(self.deadline_at),
            "phase": "prepared",
            "lease_epoch": 0,
            "runner_binding_digest": self.runner_binding_digest,
            "workspace_ref": self.workspace_ref,
            "request_digest": self.request_digest,
            "grant_digest": self.grant_digest,
            "launch_commitment_digest": None,
            "result_ref": None,
            "fixed_error_code": None,
            "created_at": timestamp,
            "updated_at": timestamp,
            "revision": 0,
        }

    def immutable_values(self) -> dict[str, object]:
        return {
            "deadline_at": self.deadline_at,
            "workspace_ref": self.workspace_ref,
            "request_digest": self.request_digest,
            "grant_digest": self.grant_digest,
            "effect_kind": self.effect_kind,
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _dump(value: datetime) -> str:
    return _utc(value).isoformat()


def _load(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value).astimezone(UTC)


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


def _binding_digest(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("runner binding must be a lowercase SHA-256 digest")
    return value


def _validate_prepare_coordinate(coordinate: NativeCoordinate) -> None:
    for name in ("thread_id", "checkpoint_id", "task_id", "interrupt_id"):
        _nonempty(getattr(coordinate, name), name)
    if not isinstance(coordinate.checkpoint_ns, str):
        raise TypeError("checkpoint_ns must be a string")


def _validate_effect_preparation(
    descriptor: EffectDescriptor,
    *,
    deadline: datetime | None,
    binding: str | None,
    request: str | None,
    now: datetime,
) -> None:
    if descriptor.kind == "manual" and deadline is not None:
        raise ValueError("unmanaged manual effect may not bind a deadline")
    if descriptor.kind != "manual" and binding is None:
        raise ValueError("managed effect requires a runner binding")
    if (
        descriptor.deadline_seconds is not None or descriptor.scope_state_keys
    ) and deadline is None:
        raise ValueError("bounded effect requires its resolved deadline")
    if (
        descriptor.runner is not None
        and request is None
        and (deadline is None or deadline > now)
    ):
        raise ValueError(
            "runnable effect requires exact request and grant commitments"
        )


def _validate_prepare_descriptor(
    descriptor: EffectDescriptor | ScopeDescriptor | AcceptDescriptor | PublishDescriptor,
    *,
    deadline: datetime | None,
    binding: str | None,
    request: str | None,
    grant: str | None,
    now: datetime,
) -> None:
    if isinstance(descriptor, EffectDescriptor):
        _validate_effect_preparation(
            descriptor,
            deadline=deadline,
            binding=binding,
            request=request,
            now=now,
        )
        return
    if isinstance(descriptor, ScopeDescriptor):
        if descriptor.scope_kind == "call" and binding is None:
            raise ValueError("call scope requires a runner binding")
        return
    if isinstance(descriptor, AcceptDescriptor):
        if binding is not None or request is not None or grant is not None:
            raise ValueError("acceptance has no external launch commitment")
        return
    if binding is None or request is None or grant is None:
        raise ValueError("publication requires exact authority commitments")


class EffectLedger:
    """Owns attempt lifecycle facts, never workflow routing or status."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        return _utc(self._clock())

    def admit_start(
        self,
        catalog: RunCatalog,
        binding: RunBinding,
        input_blob: BlobRef,
        *,
        on_admit: Callable[[object, RunBinding], None] | None = None,
    ) -> tuple[RunBinding, EffectDispatchWatch]:
        """Atomically bind a run and record its immutable initial command."""

        if catalog._store is not self._store:
            raise ValueError("catalog and effect ledger must share one owner store")
        if (
            not isinstance(input_blob, BlobRef)
            or input_blob.size < 0
            or input_blob.size > 64 * 1024 * 1024
        ):
            raise ValueError("start input blob reference is invalid")
        _binding_digest(input_blob.sha256)
        table = self._store.tables.run_drive_watches
        admitted_at = self._now()
        with self._store.write_transaction() as connection:
            admitted_binding = catalog.create_in_transaction(connection, binding)
            if on_admit is not None:
                on_admit(connection, admitted_binding)
            row = connection.execute(
                select(table).where(table.c.public_run_id == binding.public_run_id)
            ).first()
            if row is not None:
                observed_at = _load(row.admitted_at)
                assert observed_at is not None
                existing = EffectDispatchWatch(
                    row.public_run_id,
                    BlobRef(row.input_blob_sha256, int(row.input_blob_size)),
                    observed_at,
                )
                if existing.input_blob != input_blob:
                    raise EffectConflict(
                        "start admission is already bound to another input"
                    )
                return admitted_binding, existing
            connection.execute(
                table.insert().values(
                    public_run_id=admitted_binding.public_run_id,
                    input_blob_sha256=input_blob.sha256,
                    input_blob_size=input_blob.size,
                    admitted_at=_dump(admitted_at),
                )
            )
        return admitted_binding, EffectDispatchWatch(
            admitted_binding.public_run_id, input_blob, admitted_at
        )

    def list_dispatch_watches(self, *, limit: int) -> tuple[EffectDispatchWatch, ...]:
        if type(limit) is not int or limit <= 0 or limit > 1_000:
            raise ValueError("dispatch-watch limit must be an integer from 1 to 1000")
        table = self._store.tables.run_drive_watches
        with self._store.read_connection() as connection:
            rows = connection.execute(
                select(table)
                .order_by(table.c.admitted_at, table.c.public_run_id)
                .limit(limit)
            ).all()
        result = []
        for row in rows:
            admitted_at = _load(row.admitted_at)
            assert admitted_at is not None
            result.append(
                EffectDispatchWatch(
                    row.public_run_id,
                    BlobRef(row.input_blob_sha256, int(row.input_blob_size)),
                    admitted_at,
                )
            )
        return tuple(result)

    def acknowledge_dispatch_watch(self, public_run_id: str) -> bool:
        """Acknowledge only after the native snapshot is terminal."""

        _nonempty(public_run_id, "dispatch public_run_id")
        table = self._store.tables.run_drive_watches
        with self._store.write_transaction() as connection:
            result = connection.execute(
                delete(table).where(table.c.public_run_id == public_run_id)
            )
        return result.rowcount == 1

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
        raise NotImplementedError("run-drive-watch queries are staged in R2")

    def acknowledge_run_drive_watch(self, public_run_id: str) -> None:
        raise NotImplementedError(
            "run-drive-watch acknowledgement is staged in R2"
        )

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

    def prepare(
        self,
        coordinate: NativeCoordinate,
        descriptor: EffectDescriptor | ScopeDescriptor | AcceptDescriptor | PublishDescriptor,
        *,
        deadline_at: datetime | None,
        runner_binding_digest: str | None,
        workspace_ref: str | None,
        request_digest: str | None = None,
        grant_digest: str | None = None,
        lease: Lease | None = None,
    ) -> EffectRecord:
        _validate_prepare_coordinate(coordinate)
        binding = _binding_digest(runner_binding_digest)
        if workspace_ref is not None:
            workspace_ref = _nonempty(workspace_ref, "workspace_ref")
        request = _binding_digest(request_digest)
        grant = _binding_digest(grant_digest)
        if (request is None) != (grant is None):
            raise ValueError("effect request and grant digests must be bound together")
        deadline = None if deadline_at is None else _utc(deadline_at)
        now = self._now()
        _validate_prepare_descriptor(
            descriptor,
            deadline=deadline,
            binding=binding,
            request=request,
            grant=grant,
            now=now,
        )
        facts = _PreparedEffectFacts(
            effect_id=derive_effect_id(coordinate, descriptor.digest),
            coordinate=coordinate,
            descriptor_digest=descriptor.digest,
            effect_kind=descriptor.kind,
            deadline_at=deadline,
            runner_binding_digest=binding,
            workspace_ref=workspace_ref,
            request_digest=request,
            grant_digest=grant,
            created_at=now,
        )
        return self._insert_or_verify_prepared(facts, lease)

    def _insert_or_verify_prepared(
        self, facts: _PreparedEffectFacts, lease: Lease | None
    ) -> EffectRecord:
        table = self._store.tables.effects
        coordinate_clause = and_(
            table.c.thread_id == facts.coordinate.thread_id,
            table.c.checkpoint_ns == facts.coordinate.checkpoint_ns,
            table.c.checkpoint_id == facts.coordinate.checkpoint_id,
            table.c.task_id == facts.coordinate.task_id,
            table.c.interrupt_id == facts.coordinate.interrupt_id,
        )
        with self._store.write_transaction() as connection:
            if lease is not None:
                self._validate_live_lease(connection, facts.effect_id, lease)
            existing = connection.execute(
                select(table).where(coordinate_clause)
            ).first()
            if existing is not None:
                current = self._from_row(connection, existing)
                if current.descriptor_digest != facts.descriptor_digest:
                    raise EffectConflict(
                        "native coordinate already has a different descriptor"
                    )
                if current.runner_binding_digest != facts.runner_binding_digest:
                    raise EffectConflict(
                        "effect already has a different runner binding"
                    )
                if any(
                    getattr(current, key) != value
                    for key, value in facts.immutable_values().items()
                ):
                    raise EffectConflict(
                        "effect preparation conflicts with immutable facts"
                    )
                return current
            try:
                connection.execute(table.insert().values(**facts.insert_values()))
            except IntegrityError as exc:
                raise EffectConflict(
                    "native coordinate or effect identity conflicts"
                ) from exc
            row = connection.execute(
                select(table).where(table.c.effect_id == facts.effect_id)
            ).one()
            return self._from_row(connection, row)

    @staticmethod
    def _validate_result_kind(
        current: EffectRecord,
        effect_id: str,
        result: EffectResult | ScopeResult | AcceptanceResult | None,
    ) -> None:
        if result is None:
            return
        if result.effect_id != effect_id:
            raise EffectConflict("result effect_id does not match ledger identity")
        if current.effect_kind == "scope" and not isinstance(result, ScopeResult):
            raise EffectConflict("effect result kind does not match scope")
        if current.effect_kind == "accept" and not isinstance(
            result, AcceptanceResult
        ):
            raise EffectConflict("acceptance result kind does not match descriptor")
        if current.effect_kind not in {"scope", "accept"} and not isinstance(
            result, EffectResult
        ):
            raise EffectConflict("effect result kind does not match descriptor")

    @staticmethod
    def _validate_scope_seal(
        current: EffectRecord,
        result: EffectResult | ScopeResult | AcceptanceResult | None,
        scope_descriptor: ScopeDescriptor | None,
    ) -> None:
        if not isinstance(result, ScopeResult):
            return
        if scope_descriptor is None:
            raise EffectConflict("scope seal requires its validated descriptor")
        if scope_descriptor.digest != current.descriptor_digest:
            raise EffectConflict("scope descriptor does not match prepared digest")
        if result.scope_digest != current.descriptor_digest:
            raise EffectConflict("scope digest does not match descriptor")
        if result.scope_kind != scope_descriptor.scope_kind:
            raise EffectConflict("scope result kind does not match descriptor")
        if (
            result.outcome == "PASS"
            and result.runner_selector != scope_descriptor.runner_selector
        ):
            raise EffectConflict("scope runner selector does not match descriptor")
        if (
            result.outcome == "PASS"
            and result.runner_binding_digest != current.runner_binding_digest
        ):
            raise EffectConflict(
                "scope runner binding does not match prepared facts"
            )

    @staticmethod
    def _validate_prelaunch_seal(
        current: EffectRecord,
        target: str,
        result: EffectResult | ScopeResult | AcceptanceResult | None,
    ) -> None:
        if (
            target == "sealed"
            and current.phase == "prepared"
            and isinstance(result, EffectResult)
            and current.effect_kind != "manual"
            and (
                result.outcome != "ERROR"
                or result.fixed_error_code not in PRELAUNCH_ERROR_CODES
            )
        ):
            raise IllegalEffectTransition(
                "managed pre-launch seal requires a fixed pre-launch ERROR"
            )

    @staticmethod
    def _terminal_transition_replay(
        current: EffectRecord,
        target: str,
        result: EffectResult | ScopeResult | AcceptanceResult | None,
    ) -> EffectRecord | None:
        if current.phase in {"sealed", "indeterminate", "delivered"} and result is not None:
            if current.result == result:
                return current
            raise EffectConflict("effect is already sealed with a different result")
        if current.phase == "delivered" and target == "delivered":
            return current
        return None

    def _validate_transition_edge(
        self,
        connection,
        *,
        current: EffectRecord,
        effect_id: str,
        expected_revision: int,
        target: str,
        allowed_sources: set[str],
        lease: Lease | None,
    ) -> None:
        if current.effect_kind == "scope" and target in {"launching", "running"}:
            raise IllegalEffectTransition("scope effects have no launch lifecycle")
        if current.revision != expected_revision:
            raise StaleEffectRevision(
                f"expected revision {expected_revision}, found {current.revision}"
            )
        if current.phase not in allowed_sources:
            raise IllegalEffectTransition(
                f"illegal effect phase edge {current.phase} -> {target}"
            )
        lease_required = (
            target in {"launching", "running", "indeterminate"}
            or (target == "sealed" and current.phase in {"launching", "running"})
            or lease is not None
        )
        if lease_required:
            if lease is None:
                raise StaleEffectLease("a current effect lease is required")
            self._validate_live_lease(connection, effect_id, lease)

    @staticmethod
    def _validate_transition_facts(
        current: EffectRecord,
        *,
        target: str,
        runner_binding_digest: str | None,
        workspace_ref: str | None,
        launch_commitment_digest: str | None,
    ) -> tuple[str | None, str | None]:
        if target == "launching" and (
            current.request_digest is None
            or current.grant_digest is None
            or launch_commitment_digest is None
        ):
            raise EffectConflict(
                "runner launch requires request, grant, and launch commitments"
            )
        if (
            target == "sealed"
            and current.phase in {"launching", "running"}
            and runner_binding_digest is None
        ):
            raise EffectConflict("active effect seal requires its runner binding")
        if runner_binding_digest is not None:
            binding = _binding_digest(runner_binding_digest)
            if binding != current.runner_binding_digest:
                raise EffectConflict(
                    "effect runner binding does not match prepared facts"
                )
        normalized_workspace = workspace_ref
        if workspace_ref is not None:
            normalized_workspace = _nonempty(workspace_ref, "workspace_ref")
            if (
                target == "launching"
                and current.workspace_ref is not None
                and current.workspace_ref != normalized_workspace
            ):
                raise EffectConflict(
                    "effect already has a different prepared workspace"
                )
        return normalized_workspace, _binding_digest(launch_commitment_digest)

    def _transition_values(
        self,
        current: EffectRecord,
        *,
        target: str,
        lease: Lease | None,
        workspace_ref: str | None,
        launch_digest: str | None,
        result: EffectResult | ScopeResult | AcceptanceResult | None,
    ) -> tuple[dict[str, object], str | None, int, datetime]:
        revision = current.revision + 1
        now = self._now()
        changes: dict[str, object] = {
            "phase": target,
            "revision": revision,
            "updated_at": _dump(now),
        }
        if lease is not None:
            changes["lease_epoch"] = lease.epoch
        if target == "launching" and workspace_ref is not None:
            changes["workspace_ref"] = workspace_ref
        if target == "launching":
            changes["launch_commitment_digest"] = launch_digest
        result_json = None
        if result is not None:
            changes["result_ref"] = getattr(result, "result_ref", None)
            changes["fixed_error_code"] = getattr(
                result, "fixed_error_code", None
            )
            result_json = json.dumps(
                result.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        return changes, result_json, revision, now

    def _persist_transition(
        self,
        connection,
        *,
        effect_id: str,
        expected_revision: int,
        target: str,
        changes: dict[str, object],
        result_json: str | None,
        revision: int,
        now: datetime,
    ) -> EffectRecord:
        table = self._store.tables.effects
        observations = self._store.tables.effect_observations
        updated = connection.execute(
            update(table)
            .where(
                and_(
                    table.c.effect_id == effect_id,
                    table.c.revision == expected_revision,
                )
            )
            .values(**changes)
        )
        if updated.rowcount != 1:
            raise StaleEffectRevision("effect revision changed concurrently")
        connection.execute(
            observations.insert().values(
                effect_id=effect_id,
                revision=revision,
                phase=target,
                result_json=result_json,
                observed_at=_dump(now),
            )
        )
        row = connection.execute(
            select(table).where(table.c.effect_id == effect_id)
        ).one()
        return self._from_row(connection, row)

    def _transition(
        self,
        effect_id: str,
        *,
        expected_revision: int,
        target: str,
        allowed_sources: set[str],
        lease: Lease | None = None,
        runner_binding_digest: str | None = None,
        workspace_ref: str | None = None,
        launch_commitment_digest: str | None = None,
        result: EffectResult | ScopeResult | AcceptanceResult | None = None,
        scope_descriptor: ScopeDescriptor | None = None,
    ) -> EffectRecord:
        if type(expected_revision) is not int or expected_revision < 0:
            raise TypeError("expected revision must be a non-negative integer")
        table = self._store.tables.effects
        with self._store.write_transaction() as connection:
            row = connection.execute(
                select(table).where(table.c.effect_id == effect_id)
            ).first()
            if row is None:
                raise KeyError(effect_id)
            current = self._from_row(connection, row)
            self._validate_result_kind(current, effect_id, result)
            self._validate_scope_seal(current, result, scope_descriptor)
            self._validate_prelaunch_seal(current, target, result)
            replay = self._terminal_transition_replay(current, target, result)
            if replay is not None:
                return replay
            self._validate_transition_edge(
                connection,
                current=current,
                effect_id=effect_id,
                expected_revision=expected_revision,
                target=target,
                allowed_sources=allowed_sources,
                lease=lease,
            )
            workspace_ref, launch_digest = self._validate_transition_facts(
                current,
                target=target,
                runner_binding_digest=runner_binding_digest,
                workspace_ref=workspace_ref,
                launch_commitment_digest=launch_commitment_digest,
            )
            changes, result_json, revision, now = self._transition_values(
                current,
                target=target,
                lease=lease,
                workspace_ref=workspace_ref,
                launch_digest=launch_digest,
                result=result,
            )
            return self._persist_transition(
                connection,
                effect_id=effect_id,
                expected_revision=expected_revision,
                target=target,
                changes=changes,
                result_json=result_json,
                revision=revision,
                now=now,
            )

    def _validate_live_lease(self, connection, effect_id: str, lease: Lease) -> None:
        if lease.scope != "effect" or lease.key != effect_id:
            raise StaleEffectLease("lease is not bound to this effect")
        table = self._store.tables.leases
        row = connection.execute(
            select(table.c.owner, table.c.epoch, table.c.expires_at).where(
                and_(table.c.scope == "effect", table.c.lease_key == effect_id)
            )
        ).first()
        expires_at = None if row is None else _load(row.expires_at)
        if (
            row is None
            or row.owner != lease.owner
            or int(row.epoch) != lease.epoch
            or expires_at is None
            or expires_at <= self._now()
        ):
            raise StaleEffectLease("effect lease is stale, expired, or owned elsewhere")

    def mark_launching(
        self,
        effect_id: str,
        *,
        expected_revision: int,
        lease: Lease,
        runner_binding_digest: str,
        workspace_ref: str | None = None,
        launch_commitment_digest: str | None = None,
    ) -> EffectRecord:
        return self._transition(
            effect_id,
            expected_revision=expected_revision,
            target="launching",
            allowed_sources={"prepared"},
            lease=lease,
            runner_binding_digest=runner_binding_digest,
            workspace_ref=workspace_ref,
            launch_commitment_digest=launch_commitment_digest,
        )

    def mark_running(
        self,
        effect_id: str,
        *,
        expected_revision: int,
        lease: Lease,
        runner_binding_digest: str,
    ) -> EffectRecord:
        return self._transition(
            effect_id,
            expected_revision=expected_revision,
            target="running",
            allowed_sources={"launching"},
            lease=lease,
            runner_binding_digest=runner_binding_digest,
        )

    def seal(
        self,
        effect_id: str,
        result: EffectResult | ScopeResult | AcceptanceResult,
        *,
        expected_revision: int,
        lease: Lease | None = None,
        runner_binding_digest: str | None = None,
        scope_descriptor: ScopeDescriptor | None = None,
    ) -> EffectRecord:
        if (
            isinstance(result, EffectResult)
            and result.fixed_error_code == "launch_indeterminate"
        ):
            raise EffectConflict(
                "launch_indeterminate may only be stored by mark_indeterminate"
            )
        return self._transition(
            effect_id,
            expected_revision=expected_revision,
            target="sealed",
            allowed_sources={"prepared", "launching", "running"},
            lease=lease,
            runner_binding_digest=runner_binding_digest,
            result=result,
            scope_descriptor=scope_descriptor,
        )

    def mark_indeterminate(
        self, effect_id: str, *, expected_revision: int, lease: Lease
    ) -> EffectRecord:
        result = parse_effect_result(
            {
                "schema": "lockstep.effect-result/v1",
                "effect_id": effect_id,
                "outcome": "ERROR",
                "result_ref": None,
                "artifact_refs": [],
                "snapshot_ref": None,
                "diff_ref": None,
                "fixed_error_code": "launch_indeterminate",
                "evidence_refs": [],
            }
        )
        return self._transition(
            effect_id,
            expected_revision=expected_revision,
            target="indeterminate",
            allowed_sources={"launching"},
            lease=lease,
            result=result,
        )

    def mark_delivered(
        self,
        effect_id: str,
        *,
        expected_revision: int,
        lease: Lease | None = None,
    ) -> EffectRecord:
        current = self.get(effect_id)
        if current.phase == "delivered":
            return current
        return self._transition(
            effect_id,
            expected_revision=expected_revision,
            target="delivered",
            allowed_sources={"sealed", "indeterminate"},
            lease=lease,
        )
