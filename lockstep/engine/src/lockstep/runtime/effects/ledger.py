"""Durable external-attempt facts keyed by exact native coordinates."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import and_, delete, select, update
from sqlalchemy.exc import IntegrityError

from lockstep.runtime.blobs import BlobRef
from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.effects._ledger_policy import (
    PRELAUNCH_ERROR_CODES as PRELAUNCH_ERROR_CODES,
    EffectConflict,
    IllegalEffectTransition,
    StaleEffectLease,
    StaleEffectRevision,
    _terminal_transition_replay,
    _transition_values,
    _validate_effect_preparation as _validate_effect_preparation,
    _validate_prelaunch_seal,
    _validate_prepare_coordinate,
    _validate_prepare_descriptor,
    _validate_result_kind,
    _validate_scope_seal,
    _validate_transition_facts,
)
from lockstep.runtime.effects._ledger_queries import _EffectLedgerQueries
from lockstep.runtime.effects._ledger_records import (
    EffectRecord,
    RunDriveWatch,
    _PreparedEffectFacts,
    _binding_digest,
    _clock_now,
    _dump,
    _load,
    _nonempty,
    _utc,
)
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_result,
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

class EffectLedger(_EffectLedgerQueries):
    """Owns attempt lifecycle facts, never workflow routing or status."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def admit_start(
        self,
        catalog: RunCatalog,
        binding: RunBinding,
        input_blob: BlobRef,
        *,
        on_admit: Callable[[object, RunBinding], None] | None = None,
    ) -> tuple[RunBinding, RunDriveWatch]:
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
        admitted_at = _clock_now(self._clock)
        with self._store._v2_write_transaction() as connection:
            admitted_binding = catalog.create_in_transaction(connection, binding)
            if on_admit is not None:
                on_admit(connection, admitted_binding)
            row = connection.execute(
                select(table).where(table.c.public_run_id == binding.public_run_id)
            ).first()
            if row is not None:
                existing = self._run_drive_watch(row)
                if (
                    existing.input_blob_sha256 != input_blob.sha256
                    or existing.input_blob_size != input_blob.size
                ):
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
            inserted = connection.execute(
                select(table).where(
                    table.c.public_run_id == admitted_binding.public_run_id
                )
            ).one()
            return admitted_binding, self._run_drive_watch(inserted)

    def acknowledge_run_drive_watch(self, public_run_id: str) -> None:
        if type(public_run_id) is not str or not public_run_id:
            raise ValueError("public_run_id must be a non-empty string")
        table = self._store.tables.run_drive_watches
        with self._store._v2_write_transaction() as connection:
            connection.execute(
                delete(table).where(table.c.public_run_id == public_run_id)
            )

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
        now = _clock_now(self._clock)
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
        with self._store._v2_write_transaction() as connection:
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
        with self._store._v2_write_transaction() as connection:
            row = connection.execute(
                select(table).where(table.c.effect_id == effect_id)
            ).first()
            if row is None:
                raise KeyError(effect_id)
            current = self._from_row(connection, row)
            _validate_result_kind(current, effect_id, result)
            _validate_scope_seal(current, result, scope_descriptor)
            _validate_prelaunch_seal(current, target, result)
            replay = _terminal_transition_replay(current, target, result)
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
            workspace_ref, launch_digest = _validate_transition_facts(
                current,
                target=target,
                runner_binding_digest=runner_binding_digest,
                workspace_ref=workspace_ref,
                launch_commitment_digest=launch_commitment_digest,
            )
            now = _clock_now(self._clock)
            changes, result_json, revision, now = _transition_values(
                current,
                target=target,
                lease=lease,
                workspace_ref=workspace_ref,
                launch_digest=launch_digest,
                result=result,
                now=now,
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
            or expires_at <= _clock_now(self._clock)
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
