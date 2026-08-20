"""Durable external-attempt facts keyed by exact native coordinates."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError

from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_result,
    parse_scope_result,
)
from lockstep.runtime.effects.models import (
    EffectDescriptor,
    EffectResult,
    ScopeDescriptor,
    ScopeResult,
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
    result_ref: str | None
    fixed_error_code: str | None
    created_at: datetime
    updated_at: datetime
    revision: int
    result: EffectResult | ScopeResult | None = None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _dump(value: datetime) -> str:
    return _utc(value).isoformat()


def _load(value: str | None) -> datetime | None:
    return (
        None
        if value is None
        else datetime.fromisoformat(value).astimezone(timezone.utc)
    )


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


class EffectLedger:
    """Owns attempt lifecycle facts, never workflow routing or status."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return _utc(self._clock())

    def _result_for(
        self, connection, effect_id: str
    ) -> EffectResult | ScopeResult | None:
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

    def list_nonterminal(self) -> list[EffectRecord]:
        table = self._store.tables.effects
        with self._store.read_connection() as connection:
            rows = connection.execute(
                select(table)
                .where(table.c.phase.not_in({"delivered"}))
                .order_by(table.c.deadline_at, table.c.effect_id)
            ).all()
            return [self._from_row(connection, row) for row in rows]

    def prepare(
        self,
        coordinate: NativeCoordinate,
        descriptor: EffectDescriptor | ScopeDescriptor,
        *,
        deadline_at: datetime | None,
        runner_binding_digest: str | None,
        workspace_ref: str | None,
    ) -> EffectRecord:
        for name in ("thread_id", "checkpoint_id", "task_id", "interrupt_id"):
            _nonempty(getattr(coordinate, name), name)
        if not isinstance(coordinate.checkpoint_ns, str):
            raise TypeError("checkpoint_ns must be a string")
        binding = _binding_digest(runner_binding_digest)
        if workspace_ref is not None:
            workspace_ref = _nonempty(workspace_ref, "workspace_ref")
        deadline = None if deadline_at is None else _utc(deadline_at)
        if isinstance(descriptor, EffectDescriptor):
            if descriptor.kind == "manual" and deadline is not None:
                raise ValueError("unmanaged manual effect may not bind a deadline")
            if descriptor.kind != "manual" and binding is None:
                raise ValueError("managed effect requires a runner binding")
            if (
                descriptor.deadline_seconds is not None or descriptor.scope_state_keys
            ) and deadline is None:
                raise ValueError("bounded effect requires its resolved deadline")
        elif descriptor.scope_kind == "call" and binding is None:
            raise ValueError("call scope requires a runner binding")
        effect_id = derive_effect_id(coordinate, descriptor.digest)
        table = self._store.tables.effects
        now = self._now()
        values = {
            "effect_id": effect_id,
            "thread_id": coordinate.thread_id,
            "checkpoint_ns": coordinate.checkpoint_ns,
            "checkpoint_id": coordinate.checkpoint_id,
            "task_id": coordinate.task_id,
            "interrupt_id": coordinate.interrupt_id,
            "descriptor_digest": descriptor.digest,
            "effect_kind": descriptor.kind,
            "deadline_at": None if deadline is None else _dump(deadline),
            "phase": "prepared",
            "lease_epoch": 0,
            "runner_binding_digest": binding,
            "workspace_ref": workspace_ref,
            "result_ref": None,
            "fixed_error_code": None,
            "created_at": _dump(now),
            "updated_at": _dump(now),
            "revision": 0,
        }
        coordinate_clause = and_(
            table.c.thread_id == coordinate.thread_id,
            table.c.checkpoint_ns == coordinate.checkpoint_ns,
            table.c.checkpoint_id == coordinate.checkpoint_id,
            table.c.task_id == coordinate.task_id,
            table.c.interrupt_id == coordinate.interrupt_id,
        )
        with self._store.write_transaction() as connection:
            existing = connection.execute(
                select(table).where(coordinate_clause)
            ).first()
            if existing is not None:
                current = self._from_row(connection, existing)
                if current.descriptor_digest != descriptor.digest:
                    raise EffectConflict(
                        "native coordinate already has a different descriptor"
                    )
                if current.runner_binding_digest != binding:
                    raise EffectConflict(
                        "effect already has a different runner binding"
                    )
                expected = {
                    "deadline_at": deadline,
                    "workspace_ref": workspace_ref,
                    "effect_kind": descriptor.kind,
                }
                if any(
                    getattr(current, key) != value for key, value in expected.items()
                ):
                    raise EffectConflict(
                        "effect preparation conflicts with immutable facts"
                    )
                return current
            try:
                connection.execute(table.insert().values(**values))
            except IntegrityError as exc:
                raise EffectConflict(
                    "native coordinate or effect identity conflicts"
                ) from exc
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
        result: EffectResult | ScopeResult | None = None,
        scope_descriptor: ScopeDescriptor | None = None,
    ) -> EffectRecord:
        if type(expected_revision) is not int or expected_revision < 0:
            raise TypeError("expected revision must be a non-negative integer")
        table = self._store.tables.effects
        observations = self._store.tables.effect_observations
        with self._store.write_transaction() as connection:
            row = connection.execute(
                select(table).where(table.c.effect_id == effect_id)
            ).first()
            if row is None:
                raise KeyError(effect_id)
            current = self._from_row(connection, row)
            if current.effect_kind == "scope" and target in {"launching", "running"}:
                raise IllegalEffectTransition("scope effects have no launch lifecycle")
            if result is not None and result.effect_id != effect_id:
                raise EffectConflict("result effect_id does not match ledger identity")
            if result is not None:
                if current.effect_kind == "scope" and not isinstance(
                    result, ScopeResult
                ):
                    raise EffectConflict("effect result kind does not match scope")
                if current.effect_kind != "scope" and not isinstance(
                    result, EffectResult
                ):
                    raise EffectConflict("effect result kind does not match descriptor")
            if isinstance(result, ScopeResult):
                if scope_descriptor is None:
                    raise EffectConflict("scope seal requires its validated descriptor")
                if scope_descriptor.digest != current.descriptor_digest:
                    raise EffectConflict(
                        "scope descriptor does not match prepared digest"
                    )
                if result.scope_digest != current.descriptor_digest:
                    raise EffectConflict("scope digest does not match descriptor")
                if result.scope_kind != scope_descriptor.scope_kind:
                    raise EffectConflict("scope result kind does not match descriptor")
                if (
                    result.outcome == "PASS"
                    and result.runner_selector != scope_descriptor.runner_selector
                ):
                    raise EffectConflict(
                        "scope runner selector does not match descriptor"
                    )
                if (
                    result.outcome == "PASS"
                    and result.runner_binding_digest != current.runner_binding_digest
                ):
                    raise EffectConflict(
                        "scope runner binding does not match prepared facts"
                    )
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
            if (
                current.phase in {"sealed", "indeterminate", "delivered"}
                and result is not None
            ):
                if current.result == result:
                    return current
                raise EffectConflict("effect is already sealed with a different result")
            if current.phase == "delivered" and target == "delivered":
                return current
            if current.revision != expected_revision:
                raise StaleEffectRevision(
                    f"expected revision {expected_revision}, found {current.revision}"
                )
            if current.phase not in allowed_sources:
                raise IllegalEffectTransition(
                    f"illegal effect phase edge {current.phase} -> {target}"
                )
            lease_required = target in {"launching", "running", "indeterminate"} or (
                target == "sealed" and current.phase in {"launching", "running"}
            )
            if lease_required:
                if lease is None:
                    raise StaleEffectLease("a current effect lease is required")
                self._validate_live_lease(connection, effect_id, lease)
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
            revision = current.revision + 1
            now = self._now()
            changes: dict[str, object] = {
                "phase": target,
                "revision": revision,
                "updated_at": _dump(now),
            }
            if lease is not None:
                changes["lease_epoch"] = lease.epoch
            result_json = None
            if result is not None:
                changes["result_ref"] = getattr(result, "result_ref", None)
                changes["fixed_error_code"] = result.fixed_error_code
                result_json = json.dumps(
                    result.to_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
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
    ) -> EffectRecord:
        return self._transition(
            effect_id,
            expected_revision=expected_revision,
            target="launching",
            allowed_sources={"prepared"},
            lease=lease,
            runner_binding_digest=runner_binding_digest,
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
        result: EffectResult | ScopeResult,
        *,
        expected_revision: int,
        lease: Lease | None = None,
        runner_binding_digest: str | None = None,
        scope_descriptor: ScopeDescriptor | None = None,
    ) -> EffectRecord:
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

    def mark_delivered(self, effect_id: str, *, expected_revision: int) -> EffectRecord:
        current = self.get(effect_id)
        if current.phase == "delivered":
            return current
        return self._transition(
            effect_id,
            expected_revision=expected_revision,
            target="delivered",
            allowed_sources={"sealed", "indeterminate"},
        )
