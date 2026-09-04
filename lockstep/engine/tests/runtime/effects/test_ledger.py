from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from lockstep.runtime.native_models import NativeCoordinate


def descriptor_value(logical_id: str = "implement") -> dict:
    return {
        "schema": "lockstep.effect/v1",
        "kind": "managed",
        "logical_id": logical_id,
        "runner": {
            "selector": "codex",
            "required_capabilities": ["workspace", "bounded_result"],
        },
        "inputs": {"brief": {"state_key": "brief"}},
        "writes": ["src/"],
        "artifacts": [],
        "deadline_seconds": 300,
        "scope_state_keys": [],
        "result_schema": "lockstep.effect-result/v1",
    }


def result_value(effect_id: str, *, outcome: str = "PASS", suffix: str = "a") -> dict:
    return {
        "schema": "lockstep.effect-result/v1",
        "effect_id": effect_id,
        "outcome": outcome,
        "result_ref": "blob:" + suffix * 64,
        "artifact_refs": [],
        "snapshot_ref": None,
        "diff_ref": None,
        "fixed_error_code": None if outcome != "ERROR" else "runner_failed",
        "evidence_refs": [],
    }


@pytest.fixture
def ledger(tmp_path):
    from lockstep.runtime.effects.ledger import EffectLedger
    from lockstep.runtime.storage import SQLiteStore

    storage = SQLiteStore(tmp_path / "runtime.db")
    yield EffectLedger(storage), storage
    storage.close()


def prepare(ledger, *, logical_id: str = "implement", runner: str = "b" * 64):
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    descriptor = parse_effect_descriptor(descriptor_value(logical_id))
    coordinate = NativeCoordinate("thread", "checkpoint", "ns", "task", "interrupt")
    return ledger.prepare(
        coordinate,
        descriptor,
        deadline_at=datetime(2026, 8, 20, 11, tzinfo=UTC),
        runner_binding_digest=runner,
        workspace_ref="snapshot:" + "c" * 64,
        request_digest="d" * 64,
        grant_digest="e" * 64,
    )


def effect_lease(storage, effect_id: str, owner: str = "worker-a"):
    from lockstep.runtime.leases import LeaseStore

    return LeaseStore(storage).acquire("effect", effect_id, owner, 300)




def test_prepare_is_idempotent_but_rejects_changed_descriptor_or_runner(ledger) -> None:
    from lockstep.runtime.effects.ledger import EffectConflict

    effect_ledger, _storage = ledger
    first = prepare(effect_ledger)
    assert first.phase == "prepared"
    assert effect_ledger.get(first.effect_id) == first
    assert prepare(effect_ledger) == first

    with pytest.raises(EffectConflict, match="coordinate"):
        prepare(effect_ledger, logical_id="changed")
    with pytest.raises(EffectConflict, match="runner"):
        prepare(effect_ledger, runner="d" * 64)


def test_effect_phase_storage_keeps_existing_string_contract(ledger) -> None:
    effect_ledger, storage = ledger
    prepared = prepare(effect_ledger)

    with storage.read_connection() as connection:
        stored_prepared = connection.execute(
            select(storage.tables.effects.c.phase).where(
                storage.tables.effects.c.effect_id == prepared.effect_id
            )
        ).scalar_one()

    lease = effect_lease(storage, prepared.effect_id)
    effect_ledger.mark_launching(
        prepared.effect_id,
        expected_revision=prepared.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
        launch_commitment_digest="f" * 64,
    )

    with storage.read_connection() as connection:
        stored_launching = connection.execute(
            select(storage.tables.effects.c.phase).where(
                storage.tables.effects.c.effect_id == prepared.effect_id
            )
        ).scalar_one()
        observation_phases = tuple(
            connection.execute(
                select(storage.tables.effect_observations.c.phase)
                .where(
                    storage.tables.effect_observations.c.effect_id
                    == prepared.effect_id
                )
                .order_by(storage.tables.effect_observations.c.revision)
            ).scalars()
        )

    assert type(stored_prepared) is str
    assert stored_prepared == "prepared"
    assert type(stored_launching) is str
    assert stored_launching == "launching"
    assert observation_phases == ("launching",)


def test_effect_ledger_rejects_unknown_persisted_phase(ledger) -> None:
    effect_ledger, storage = ledger
    prepared = prepare(effect_ledger)
    with storage.write_transaction() as connection:
        connection.execute(
            update(storage.tables.effects)
            .where(storage.tables.effects.c.effect_id == prepared.effect_id)
            .values(phase="unknown")
        )

    with pytest.raises(ValueError):
        effect_ledger.get(prepared.effect_id)




def test_legal_monotonic_phases_and_cas(ledger) -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_result
    from lockstep.runtime.effects.ledger import (
        EffectConflict,
        IllegalEffectTransition,
        StaleEffectRevision,
    )

    effect_ledger, storage = ledger
    prepared = prepare(effect_ledger)
    lease = effect_lease(storage, prepared.effect_id)
    with pytest.raises(IllegalEffectTransition):
        effect_ledger.mark_running(
            prepared.effect_id,
            expected_revision=prepared.revision,
            lease=lease,
            runner_binding_digest="b" * 64,
        )
    assert effect_ledger.get(prepared.effect_id) == prepared

    launching = effect_ledger.mark_launching(
        prepared.effect_id,
        expected_revision=prepared.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
        launch_commitment_digest="f" * 64,
    )
    with pytest.raises(StaleEffectRevision):
        effect_ledger.mark_running(
            prepared.effect_id,
            expected_revision=prepared.revision,
            lease=lease,
            runner_binding_digest="b" * 64,
        )
    assert effect_ledger.get(prepared.effect_id) == launching

    running = effect_ledger.mark_running(
        launching.effect_id,
        expected_revision=launching.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
    )
    result = parse_effect_result(result_value(running.effect_id))
    with pytest.raises(EffectConflict, match="runner binding"):
        effect_ledger.seal(
            running.effect_id,
            result,
            expected_revision=running.revision,
            lease=lease,
        )
    assert effect_ledger.get(running.effect_id) == running
    sealed = effect_ledger.seal(
        running.effect_id,
        result,
        expected_revision=running.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
    )
    delivered = effect_ledger.mark_delivered(
        sealed.effect_id, expected_revision=sealed.revision
    )
    assert [launching.phase, running.phase, sealed.phase, delivered.phase] == [
        "launching",
        "running",
        "sealed",
        "delivered",
    ]
    assert delivered.result == result
    assert (
        effect_ledger.mark_delivered(
            delivered.effect_id, expected_revision=sealed.revision
        )
        == delivered
    )


def test_direct_scope_path_and_expired_effect_do_not_launch(ledger) -> None:
    from lockstep.runtime.effects.descriptors import (
        build_scope_result,
        parse_effect_descriptor,
    )
    from lockstep.runtime.effects.ledger import IllegalEffectTransition

    effect_ledger, storage = ledger
    descriptor = parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "scope",
            "logical_id": "parallel-scope",
            "scope_kind": "parallel",
            "duration_seconds": None,
            "runner_selector": None,
            "ancestor_deadline_state_keys": [],
            "result_state_key": "parallel_scope",
            "result_schema": "lockstep.scope-result/v1",
        }
    )
    prepared = effect_ledger.prepare(
        NativeCoordinate("thread", "checkpoint", "ns", "task", "interrupt"),
        descriptor,
        deadline_at=None,
        runner_binding_digest=None,
        workspace_ref=None,
    )
    lease = effect_lease(storage, prepared.effect_id)
    with pytest.raises(IllegalEffectTransition, match="scope"):
        effect_ledger.mark_launching(
            prepared.effect_id,
            expected_revision=prepared.revision,
            lease=lease,
            runner_binding_digest="b" * 64,
        )
    assert effect_ledger.get(prepared.effect_id) == prepared
    result = build_scope_result(
        effect_id=prepared.effect_id,
        scope_digest=descriptor.digest,
        scope_kind="parallel",
        now=datetime(2026, 8, 20, 10, tzinfo=UTC),
        duration_seconds=None,
        ancestors=(),
    )
    sealed = effect_ledger.seal(
        prepared.effect_id,
        result,
        expected_revision=prepared.revision,
        scope_descriptor=descriptor,
    )
    assert sealed.phase == "sealed"
    assert (
        effect_ledger.mark_delivered(
            sealed.effect_id, expected_revision=sealed.revision
        ).phase
        == "delivered"
    )


def test_launch_indeterminate_is_the_only_ambiguity_result_and_never_relaunches(
    ledger,
) -> None:
    from lockstep.runtime.effects.ledger import IllegalEffectTransition

    effect_ledger, storage = ledger
    prepared = prepare(effect_ledger)
    lease = effect_lease(storage, prepared.effect_id)
    launching = effect_ledger.mark_launching(
        prepared.effect_id,
        expected_revision=prepared.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
        launch_commitment_digest="f" * 64,
    )
    record = effect_ledger.mark_indeterminate(
        prepared.effect_id,
        expected_revision=launching.revision,
        lease=lease,
    )
    assert record.phase == "indeterminate"
    assert record.result is not None
    assert record.result.outcome == "ERROR"
    assert record.result.fixed_error_code == "launch_indeterminate"
    with pytest.raises(IllegalEffectTransition):
        effect_ledger.mark_launching(
            prepared.effect_id,
            expected_revision=record.revision,
            lease=lease,
            runner_binding_digest="b" * 64,
        )






def test_seal_is_idempotent_for_same_result_and_conflicts_for_different_result(
    ledger,
) -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_result
    from lockstep.runtime.effects.ledger import EffectConflict

    effect_ledger, storage = ledger
    prepared = prepare(effect_ledger)
    lease = effect_lease(storage, prepared.effect_id)
    launching = effect_ledger.mark_launching(
        prepared.effect_id,
        expected_revision=prepared.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
        launch_commitment_digest="f" * 64,
    )
    running = effect_ledger.mark_running(
        prepared.effect_id,
        expected_revision=launching.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
    )
    result = parse_effect_result(result_value(prepared.effect_id))
    sealed = effect_ledger.seal(
        prepared.effect_id,
        result,
        expected_revision=running.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
    )
    assert (
        effect_ledger.seal(
            prepared.effect_id, result, expected_revision=prepared.revision
        )
        == sealed
    )
    with pytest.raises(EffectConflict, match="different result"):
        effect_ledger.seal(
            prepared.effect_id,
            parse_effect_result(result_value(prepared.effect_id, suffix="e")),
            expected_revision=sealed.revision,
        )
    assert effect_ledger.get(prepared.effect_id) == sealed


def test_concurrent_sqlite_seal_commits_one_exact_result(ledger) -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_result
    from lockstep.runtime.effects.ledger import EffectConflict

    effect_ledger, storage = ledger
    prepared = prepare(effect_ledger)
    lease = effect_lease(storage, prepared.effect_id)
    launching = effect_ledger.mark_launching(
        prepared.effect_id,
        expected_revision=prepared.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
        launch_commitment_digest="f" * 64,
    )
    running = effect_ledger.mark_running(
        prepared.effect_id,
        expected_revision=launching.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
    )
    results = [
        parse_effect_result(result_value(prepared.effect_id, suffix="a")),
        parse_effect_result(result_value(prepared.effect_id, suffix="e")),
    ]

    def seal(result):
        try:
            return effect_ledger.seal(
                prepared.effect_id,
                result,
                expected_revision=running.revision,
                lease=lease,
                runner_binding_digest="b" * 64,
            )
        except EffectConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(seal, results))

    assert sum(item == "conflict" for item in outcomes) == 1
    stored = effect_ledger.get(prepared.effect_id)
    assert stored.phase == "sealed"
    assert stored.result in results


def test_prepared_managed_effect_can_only_seal_a_fixed_prelaunch_error(
    ledger,
) -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_result
    from lockstep.runtime.effects.ledger import IllegalEffectTransition

    effect_ledger, _storage = ledger
    prepared = prepare(effect_ledger)
    passed = parse_effect_result(result_value(prepared.effect_id))
    with pytest.raises(IllegalEffectTransition, match="pre-launch"):
        effect_ledger.seal(
            prepared.effect_id,
            passed,
            expected_revision=prepared.revision,
        )
    assert effect_ledger.get(prepared.effect_id) == prepared

    failed_value = result_value(prepared.effect_id, outcome="ERROR")
    failed_value.update(result_ref=None, fixed_error_code="prelaunch_failed")
    error = parse_effect_result(failed_value)
    sealed = effect_ledger.seal(
        prepared.effect_id,
        error,
        expected_revision=prepared.revision,
    )
    assert sealed.phase == "sealed"
    assert sealed.result == error


def test_identical_prepare_adopts_every_existing_effect_phase(ledger) -> None:
    from lockstep.runtime.effects.descriptors import (
        parse_effect_descriptor,
        parse_effect_result,
    )

    effect_ledger, storage = ledger
    descriptor = parse_effect_descriptor(descriptor_value())
    coordinate = NativeCoordinate("thread", "checkpoint", "ns", "task", "interrupt")

    def prepare_again():
        return effect_ledger.prepare(
            coordinate,
            descriptor,
            deadline_at=datetime(2026, 8, 20, 11, tzinfo=UTC),
            runner_binding_digest="b" * 64,
            workspace_ref="snapshot:" + "c" * 64,
            request_digest="d" * 64,
            grant_digest="e" * 64,
        )

    prepared = prepare_again()
    lease = effect_lease(storage, prepared.effect_id)
    launching = effect_ledger.mark_launching(
        prepared.effect_id,
        expected_revision=prepared.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
        launch_commitment_digest="f" * 64,
    )
    assert prepare_again() == launching
    running = effect_ledger.mark_running(
        prepared.effect_id,
        expected_revision=launching.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
    )
    assert prepare_again() == running
    sealed = effect_ledger.seal(
        prepared.effect_id,
        parse_effect_result(result_value(prepared.effect_id)),
        expected_revision=running.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
    )
    assert prepare_again() == sealed

    second_coordinate = NativeCoordinate(
        "thread-2", "checkpoint", "ns", "task", "interrupt"
    )

    def prepare_second():
        return effect_ledger.prepare(
            second_coordinate,
            descriptor,
            deadline_at=datetime(2026, 8, 20, 11, tzinfo=UTC),
            runner_binding_digest="b" * 64,
            workspace_ref="snapshot:" + "c" * 64,
            request_digest="d" * 64,
            grant_digest="e" * 64,
        )

    second = prepare_second()
    second_lease = effect_lease(storage, second.effect_id)
    second_launch = effect_ledger.mark_launching(
        second.effect_id,
        expected_revision=second.revision,
        lease=second_lease,
        runner_binding_digest="b" * 64,
        launch_commitment_digest="f" * 64,
    )
    indeterminate = effect_ledger.mark_indeterminate(
        second.effect_id,
        expected_revision=second_launch.revision,
        lease=second_lease,
    )
    assert prepare_second() == indeterminate




def test_expired_effect_lease_cannot_cross_launch_cas(ledger) -> None:
    from datetime import timedelta

    from lockstep.runtime.effects.ledger import EffectLedger, StaleEffectLease
    from lockstep.runtime.leases import LeaseStore

    class Clock:
        now = datetime(2026, 8, 20, 10, tzinfo=UTC)

        def __call__(self):
            return self.now

    _unused, storage = ledger
    clock = Clock()
    effect_ledger = EffectLedger(storage, clock=clock)
    leases = LeaseStore(storage, clock=clock)
    prepared = prepare(effect_ledger)
    stale = leases.acquire("effect", prepared.effect_id, "worker-a", 10)
    clock.now += timedelta(seconds=11)
    current = leases.acquire("effect", prepared.effect_id, "worker-b", 10)

    with pytest.raises(StaleEffectLease):
        effect_ledger.mark_launching(
            prepared.effect_id,
            expected_revision=prepared.revision,
            lease=stale,
            runner_binding_digest="b" * 64,
        )
    assert effect_ledger.get(prepared.effect_id) == prepared

    launching = effect_ledger.mark_launching(
        prepared.effect_id,
        expected_revision=prepared.revision,
        lease=current,
        runner_binding_digest="b" * 64,
        launch_commitment_digest="f" * 64,
    )
    assert launching.phase == "launching"
    assert launching.lease_epoch == current.epoch


def test_ledger_rejects_result_kind_scope_digest_and_scope_runner_mismatch(
    ledger,
) -> None:
    from lockstep.runtime.effects.descriptors import (
        build_scope_result,
        parse_effect_descriptor,
        parse_effect_result,
    )
    from lockstep.runtime.effects.ledger import EffectConflict

    effect_ledger, _storage = ledger
    ordinary = prepare(effect_ledger)
    wrong_kind = build_scope_result(
        effect_id=ordinary.effect_id,
        scope_digest="a" * 64,
        scope_kind="parallel",
        now=datetime(2026, 8, 20, 10, tzinfo=UTC),
        duration_seconds=None,
        ancestors=(),
    )
    with pytest.raises(EffectConflict, match="result kind"):
        effect_ledger.seal(
            ordinary.effect_id,
            wrong_kind,
            expected_revision=ordinary.revision,
        )

    call = parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "scope",
            "logical_id": "call-scope",
            "scope_kind": "call",
            "duration_seconds": 60,
            "runner_selector": "codex",
            "ancestor_deadline_state_keys": [],
            "result_state_key": "call_scope",
            "result_schema": "lockstep.scope-result/v1",
        }
    )
    prepared = effect_ledger.prepare(
        NativeCoordinate("thread-2", "checkpoint", "ns", "task", "interrupt"),
        call,
        deadline_at=None,
        runner_binding_digest="c" * 64,
        workspace_ref=None,
    )
    wrong_digest = build_scope_result(
        effect_id=prepared.effect_id,
        scope_digest="d" * 64,
        scope_kind="call",
        now=datetime(2026, 8, 20, 10, tzinfo=UTC),
        duration_seconds=60,
        ancestors=(),
        runner_selector="codex",
        runner_binding_digest="e" * 64,
    )
    with pytest.raises(EffectConflict, match="scope digest"):
        effect_ledger.seal(
            prepared.effect_id,
            wrong_digest,
            expected_revision=prepared.revision,
            scope_descriptor=call,
        )

    wrong_runner = build_scope_result(
        effect_id=prepared.effect_id,
        scope_digest=call.digest,
        scope_kind="call",
        now=datetime(2026, 8, 20, 10, tzinfo=UTC),
        duration_seconds=60,
        ancestors=(),
        runner_selector="codex",
        runner_binding_digest="e" * 64,
    )
    with pytest.raises(EffectConflict, match="runner binding"):
        effect_ledger.seal(
            prepared.effect_id,
            wrong_runner,
            expected_revision=prepared.revision,
            scope_descriptor=call,
        )

    wrong_selector = build_scope_result(
        effect_id=prepared.effect_id,
        scope_digest=call.digest,
        scope_kind="call",
        now=datetime(2026, 8, 20, 10, tzinfo=UTC),
        duration_seconds=60,
        ancestors=(),
        runner_selector="claude",
        runner_binding_digest="c" * 64,
    )
    with pytest.raises(EffectConflict, match="runner selector"):
        effect_ledger.seal(
            prepared.effect_id,
            wrong_selector,
            expected_revision=prepared.revision,
            scope_descriptor=call,
        )

    ordinary_result = parse_effect_result(result_value(prepared.effect_id))
    with pytest.raises(EffectConflict, match="result kind"):
        effect_ledger.seal(
            prepared.effect_id,
            ordinary_result,
            expected_revision=prepared.revision,
            scope_descriptor=call,
        )
