from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest

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


def test_effect_table_owns_external_facts_only(ledger) -> None:
    _ledger, storage = ledger
    assert set(storage.tables.effects.c.keys()) == {
        "effect_id",
        "thread_id",
        "checkpoint_ns",
        "checkpoint_id",
        "task_id",
        "interrupt_id",
        "descriptor_digest",
        "effect_kind",
        "deadline_at",
        "phase",
        "lease_epoch",
        "runner_binding_digest",
        "workspace_ref",
        "request_digest",
        "grant_digest",
        "launch_commitment_digest",
        "result_ref",
        "fixed_error_code",
        "created_at",
        "updated_at",
        "revision",
    }
    forbidden = {"status", "node", "next", "route", "branch", "terminal", "checkpoint"}
    assert forbidden.isdisjoint(storage.tables.effects.c.keys())
    assert not hasattr(storage.tables, "work_items")
    assert not hasattr(storage.tables, "timers")
    assert set(storage.tables.effect_observations.c.keys()) == {
        "effect_id",
        "revision",
        "phase",
        "result_json",
        "observed_at",
    }


def test_run_drive_watch_admission_is_atomic_idempotent_and_not_status(ledger) -> None:
    from lockstep.runtime.blobs import BlobRef
    from lockstep.runtime.catalog import RunBinding, RunCatalog

    effect_ledger, storage = ledger
    catalog = RunCatalog(storage)
    binding = RunBinding(
        "run-1", "thread-1", "a" * 64, "bundle:" + "b" * 64, "/project"
    )
    first_binding, first = effect_ledger.admit_start(
        catalog, binding, BlobRef("c" * 64, 2)
    )
    assert first_binding.public_run_id == "run-1"
    assert (
        first.admission_seq,
        first.public_run_id,
        first.input_blob_sha256,
        first.input_blob_size,
    ) == (1, "run-1", "c" * 64, 2)
    assert effect_ledger.admit_start(catalog, binding, BlobRef("c" * 64, 2)) == (
        first_binding,
        first,
    )

    assert effect_ledger.list_run_drive_watches(
        after_admission_seq=0,
        high_water=first.admission_seq,
        limit=2,
    ) == (first,)
    assert effect_ledger.acknowledge_run_drive_watch("run-1") is None
    assert effect_ledger.list_run_drive_watches(
        after_admission_seq=0,
        high_water=first.admission_seq,
        limit=2,
    ) == ()
    assert set(storage.tables.run_drive_watches.c.keys()) == {
        "admission_seq",
        "public_run_id",
        "input_blob_sha256",
        "input_blob_size",
        "admitted_at",
    }


def test_max_run_drive_admission_seq_returns_none_then_current_db_max(ledger) -> None:
    from lockstep.runtime.blobs import BlobRef
    from lockstep.runtime.catalog import RunBinding, RunCatalog

    effect_ledger, storage = ledger
    catalog = RunCatalog(storage)
    assert effect_ledger.max_run_drive_admission_seq() is None

    for index in (1, 2):
        effect_ledger.admit_start(
            catalog,
            RunBinding(
                f"run-{index}",
                f"thread-{index}",
                "a" * 64,
                "bundle:" + "b" * 64,
                "/project",
            ),
            BlobRef("c" * 64, 2),
        )

    assert effect_ledger.max_run_drive_admission_seq() == 2


def test_list_run_drive_watches_validates_and_returns_bounded_ordered_page(
    ledger,
) -> None:
    from lockstep.runtime.blobs import BlobRef
    from lockstep.runtime.catalog import RunBinding, RunCatalog
    from lockstep.runtime.effects.ledger import EffectLedger, RunDriveWatch

    _effect_ledger, storage = ledger
    offset_instant = datetime(
        2026,
        8,
        20,
        12,
        tzinfo=timezone(timedelta(hours=2)),
    )
    effect_ledger = EffectLedger(storage, clock=lambda: offset_instant)
    catalog = RunCatalog(storage)
    for index, digest in enumerate(("a" * 64, "b" * 64, "c" * 64), start=1):
        effect_ledger.admit_start(
            catalog,
            RunBinding(
                f"run-{index}",
                f"thread-{index}",
                "d" * 64,
                "bundle:" + "e" * 64,
                "/project",
            ),
            BlobRef(digest, index),
        )

    expected_utc = datetime(2026, 8, 20, 10, tzinfo=UTC)
    all_rows = effect_ledger.list_run_drive_watches(
        after_admission_seq=0,
        high_water=3,
        limit=128,
    )
    assert all_rows == (
        RunDriveWatch(1, "run-1", "a" * 64, 1, expected_utc),
        RunDriveWatch(2, "run-2", "b" * 64, 2, expected_utc),
        RunDriveWatch(3, "run-3", "c" * 64, 3, expected_utc),
    )
    assert all(row.admitted_at.tzinfo is UTC for row in all_rows)
    assert effect_ledger.list_run_drive_watches(
        after_admission_seq=0,
        high_water=2,
        limit=128,
    ) == all_rows[:2]
    assert effect_ledger.list_run_drive_watches(
        after_admission_seq=1,
        high_water=3,
        limit=1,
    ) == (all_rows[1],)
    assert effect_ledger.list_run_drive_watches(
        after_admission_seq=3,
        high_water=3,
        limit=128,
    ) == ()

    for after_admission_seq, high_water in (
        (-1, 3),
        (True, 3),
        (1.0, 3),
        (4, 3),
        (0, -1),
        (0, True),
        (0, 1.0),
    ):
        with pytest.raises(
            ValueError,
            match=(
                "^run-drive watch bounds must be integers with "
                "0 <= after_admission_seq <= high_water$"
            ),
        ):
            effect_ledger.list_run_drive_watches(
                after_admission_seq=after_admission_seq,
                high_water=high_water,
                limit=1,
            )
    for limit in (0, 129, True, 1.0):
        with pytest.raises(
            ValueError,
            match="^run-drive watch limit must be an integer from 1 to 128$",
        ):
            effect_ledger.list_run_drive_watches(
                after_admission_seq=0,
                high_water=3,
                limit=limit,
            )


def test_list_run_drive_watches_by_public_run_ids_is_exact_bounded_and_ordered(
    ledger,
) -> None:
    from lockstep.runtime.blobs import BlobRef
    from lockstep.runtime.catalog import RunBinding, RunCatalog

    effect_ledger, storage = ledger
    catalog = RunCatalog(storage)
    admitted = []
    for index in range(1, 4):
        _binding, watch = effect_ledger.admit_start(
            catalog,
            RunBinding(
                f"run-{index}",
                f"thread-{index}",
                "a" * 64,
                "bundle:" + "b" * 64,
                "/project",
            ),
            BlobRef("c" * 64, index),
        )
        admitted.append(watch)

    assert effect_ledger.list_run_drive_watches_by_public_run_ids(
        ("missing", "run-1", "run-3")
    ) == (admitted[0], admitted[2])

    invalid = (
        (),
        ["run-1"],
        ("run-2", "run-1"),
        ("run-1", "run-1"),
        ("",),
        tuple(f"run-{index:03d}" for index in range(129)),
    )
    for public_run_ids in invalid:
        with pytest.raises(ValueError, match="run-drive watch IDs"):
            effect_ledger.list_run_drive_watches_by_public_run_ids(
                public_run_ids
            )


def test_acknowledge_run_drive_watch_validates_and_converges_after_post_commit_crash(
    tmp_path,
    monkeypatch,
) -> None:
    from lockstep.runtime.blobs import BlobRef
    from lockstep.runtime.catalog import RunBinding, RunCatalog
    from lockstep.runtime.effects.ledger import EffectLedger
    from lockstep.runtime.storage import SQLiteStore

    database_path = tmp_path / "runtime.db"
    storage = SQLiteStore(database_path)
    reopened = None
    try:
        effect_ledger = EffectLedger(storage)
        catalog = RunCatalog(storage)
        for index in (1, 2):
            effect_ledger.admit_start(
                catalog,
                RunBinding(
                    f"run-{index}",
                    f"thread-{index}",
                    "a" * 64,
                    "bundle:" + "b" * 64,
                    "/project",
                ),
                BlobRef("c" * 64, index),
            )

        def remaining_ids(ledger: EffectLedger) -> tuple[str, ...]:
            return tuple(
                watch.public_run_id
                for watch in ledger.list_run_drive_watches(
                    after_admission_seq=0,
                    high_water=2,
                    limit=128,
                )
            )

        for public_run_id in ("", 1):
            with pytest.raises(
                ValueError,
                match="^public_run_id must be a non-empty string$",
            ):
                effect_ledger.acknowledge_run_drive_watch(public_run_id)
        assert remaining_ids(effect_ledger) == ("run-1", "run-2")

        transaction_entries = 0
        real_transaction = storage._v2_write_transaction

        @contextmanager
        def observed_transaction():
            nonlocal transaction_entries
            transaction_entries += 1
            with real_transaction() as connection:
                yield connection

        monkeypatch.setattr(storage, "_v2_write_transaction", observed_transaction)

        class SimulatedPostCommitCrash(RuntimeError):
            pass

        with pytest.raises(SimulatedPostCommitCrash):
            assert effect_ledger.acknowledge_run_drive_watch("run-1") is None
            assert transaction_entries == 1
            raise SimulatedPostCommitCrash

        storage.close()
        reopened = SQLiteStore(database_path)
        reopened_ledger = EffectLedger(reopened)
        assert remaining_ids(reopened_ledger) == ("run-2",)
        assert reopened_ledger.acknowledge_run_drive_watch("run-1") is None
        assert remaining_ids(reopened_ledger) == ("run-2",)
    finally:
        if reopened is not None:
            reopened.close()
        storage.close()


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


def test_prepare_requires_resolved_deadline_and_runner_bindings(ledger) -> None:
    from sqlalchemy import func, select

    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    effect_ledger, storage = ledger
    coordinate = NativeCoordinate("thread", "checkpoint", "", "task", "interrupt")
    managed = parse_effect_descriptor(descriptor_value())

    with pytest.raises(ValueError, match="deadline"):
        effect_ledger.prepare(
            coordinate,
            managed,
            deadline_at=None,
            runner_binding_digest="b" * 64,
            workspace_ref=None,
        )
    with pytest.raises(ValueError, match="runner binding"):
        effect_ledger.prepare(
            coordinate,
            managed,
            deadline_at=datetime(2026, 8, 20, 11, tzinfo=UTC),
            runner_binding_digest=None,
            workspace_ref=None,
        )

    manual_value = descriptor_value()
    manual_value.update(kind="manual", runner=None, deadline_seconds=None)
    manual = parse_effect_descriptor(manual_value)
    with pytest.raises(ValueError, match="manual.*deadline"):
        effect_ledger.prepare(
            coordinate,
            manual,
            deadline_at=datetime(2026, 8, 20, 11, tzinfo=UTC),
            runner_binding_digest=None,
            workspace_ref=None,
        )

    with storage.read_connection() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(storage.tables.effects)
            ).scalar_one()
            == 0
        )


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


def test_public_seal_can_never_store_launch_indeterminate(ledger) -> None:
    from sqlalchemy import select

    from lockstep.runtime.effects.descriptors import (
        parse_effect_descriptor,
        parse_effect_result,
    )
    from lockstep.runtime.effects.ledger import EffectConflict

    effect_ledger, storage = ledger

    def facts() -> tuple[tuple[tuple, ...], tuple[tuple, ...]]:
        with storage.read_connection() as connection:
            effects = tuple(
                tuple(row)
                for row in connection.execute(
                    select(storage.tables.effects).order_by(
                        storage.tables.effects.c.effect_id
                    )
                )
            )
            observations = tuple(
                tuple(row)
                for row in connection.execute(
                    select(storage.tables.effect_observations).order_by(
                        storage.tables.effect_observations.c.effect_id,
                        storage.tables.effect_observations.c.revision,
                    )
                )
            )
        return effects, observations

    prepared = prepare(effect_ledger)
    lease = effect_lease(storage, prepared.effect_id)
    launching = effect_ledger.mark_launching(
        prepared.effect_id,
        expected_revision=prepared.revision,
        lease=lease,
        runner_binding_digest="b" * 64,
        launch_commitment_digest="f" * 64,
    )
    ambiguous = parse_effect_result(
        {
            **result_value(prepared.effect_id, outcome="ERROR"),
            "result_ref": None,
            "fixed_error_code": "launch_indeterminate",
        }
    )
    before_launching = facts()
    with pytest.raises(EffectConflict, match="mark_indeterminate"):
        effect_ledger.seal(
            launching.effect_id,
            ambiguous,
            expected_revision=launching.revision,
            lease=lease,
            runner_binding_digest="b" * 64,
        )
    assert facts() == before_launching

    manual_value = descriptor_value("manual")
    manual_value.update(kind="manual", runner=None, deadline_seconds=None)
    manual = parse_effect_descriptor(manual_value)
    manual_record = effect_ledger.prepare(
        NativeCoordinate("manual-thread", "checkpoint", "", "task", "interrupt"),
        manual,
        deadline_at=None,
        runner_binding_digest=None,
        workspace_ref=None,
    )
    manual_ambiguous = parse_effect_result(
        {
            **result_value(manual_record.effect_id, outcome="ERROR"),
            "result_ref": None,
            "fixed_error_code": "launch_indeterminate",
        }
    )
    before_manual = facts()
    with pytest.raises(EffectConflict, match="mark_indeterminate"):
        effect_ledger.seal(
            manual_record.effect_id,
            manual_ambiguous,
            expected_revision=manual_record.revision,
        )
    assert facts() == before_manual


def test_nul_write_rejects_before_ledger_prepare_without_any_fact(ledger) -> None:
    from sqlalchemy import func, select

    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    effect_ledger, storage = ledger
    value = descriptor_value()
    value["writes"] = ["src/\x00escape"]

    with pytest.raises(ValueError, match="NUL"):
        descriptor = parse_effect_descriptor(value)
        effect_ledger.prepare(
            NativeCoordinate("thread", "checkpoint", "", "task", "interrupt"),
            descriptor,
            deadline_at=datetime(2026, 8, 20, 11, tzinfo=UTC),
            runner_binding_digest="b" * 64,
            workspace_ref=None,
        )

    with storage.read_connection() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(storage.tables.effects)
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                select(func.count()).select_from(storage.tables.effect_observations)
            ).scalar_one()
            == 0
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


def test_rejections_leave_row_and_phase_byte_equivalent(ledger) -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_result
    from lockstep.runtime.effects.ledger import EffectConflict, StaleEffectRevision

    effect_ledger, storage = ledger
    prepared = prepare(effect_ledger)
    lease = effect_lease(storage, prepared.effect_id)

    def row_bytes() -> bytes:
        with storage.read_connection() as connection:
            row = connection.execute(storage.tables.effects.select()).mappings().one()
        return repr(sorted(row.items())).encode()

    before = row_bytes()
    with pytest.raises(StaleEffectRevision):
        effect_ledger.mark_launching(
            prepared.effect_id,
            expected_revision=99,
            lease=lease,
            runner_binding_digest="b" * 64,
        )
    assert row_bytes() == before

    with pytest.raises((TypeError, ValueError), match="revision"):
        effect_ledger.mark_launching(
            prepared.effect_id,
            expected_revision=True,
            lease=lease,
            runner_binding_digest="b" * 64,
        )
    assert row_bytes() == before

    wrong = parse_effect_result(result_value("another-effect"))
    with pytest.raises(EffectConflict, match="effect_id"):
        effect_ledger.seal(
            prepared.effect_id, wrong, expected_revision=prepared.revision
        )
    assert row_bytes() == before


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
