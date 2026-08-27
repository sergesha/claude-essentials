"""Private command-owned recovery-driver boundary."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import import_module
from inspect import Parameter, signature
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, get_type_hints

import pytest
from sqlalchemy import event


@contextmanager
def _prepared_command(tmp_path: Path):
    from lockstep.runtime.engine import Engine

    recipes = tmp_path / "recipes"
    recipes.mkdir()
    command = Engine.command(tmp_path / "state", recipes)
    try:
        command._prepare_writable_core()
        yield command
    finally:
        command._rollback_writable_core_activation()
        command.close()


@contextmanager
def _active_command(tmp_path: Path):
    from lockstep.runtime.engine import Engine

    recipes = tmp_path / "recipes"
    recipes.mkdir()
    command = Engine.command(tmp_path / "state", recipes)
    try:
        command._activate_writable_core()
        command._pump_stop.set()
        command._pump_wakeup.set()
        thread = command._pump_thread
        if thread is not None:
            thread.join(timeout=5)
            assert not thread.is_alive()
        yield command
    finally:
        command.close()


def _durable_command_state(command) -> dict[str, tuple[dict[str, object], ...]]:
    table_names = (
        "runs",
        "run_start_inputs",
        "run_drive_watches",
        "runtime_schema_migrations",
        "effects",
        "effect_observations",
    )
    with command.store.read_connection() as connection:
        return {
            name: tuple(
                dict(row._mapping)
                for row in connection.execute(
                    getattr(command.store.tables, name).select()
                ).all()
            )
            for name in table_names
        }


def _command_drive_state(command) -> tuple[object, ...]:
    return (
        tuple(sorted(command._active_effect_runs)),
        tuple(sorted(command._queued_effect_runs)),
        tuple(command._active_effect_queue),
        command._pump_thread,
        command._pump_stop.is_set(),
        command._pump_wakeup.is_set(),
        command._pump_failure,
    )


def _contender_can_acquire(lock) -> bool:
    acquired_by_contender: list[bool] = []

    def probe() -> None:
        acquired = lock.acquire(blocking=False)
        acquired_by_contender.append(acquired)
        if acquired:
            lock.release()

    contender = threading.Thread(target=probe)
    contender.start()
    contender.join(timeout=5)
    assert not contender.is_alive()
    return acquired_by_contender[0]


def test_temporary_binding_does_not_release_concurrent_same_binding() -> None:
    from lockstep.runtime.recovery_driver import _bound_runtime

    binding = SimpleNamespace(public_run_id="run-1")
    unbound = []
    runtime = SimpleNamespace(
        binding=lambda _run_id: (_ for _ in ()).throw(KeyError("run-1")),
        bind=lambda _binding: False,
        unbind=unbound.append,
    )

    with _bound_runtime(runtime, binding) as available:
        assert available is True

    assert unbound == []


@contextmanager
def _observed_sweeps(command):
    driver = command._recovery_driver
    driver_before = dict(vars(driver))
    sweep = driver._sweep_run_drive_watches
    calls: list[tuple[str | None, int, bool, bool]] = []

    def observe_sweep(
        *,
        project_identity: str | None,
        limit: int,
    ) -> tuple[str, ...]:
        calls.append(
            (
                project_identity,
                limit,
                _contender_can_acquire(command._activation_lock),
                _contender_can_acquire(command._admission_recovery_lock),
            )
        )
        return sweep(project_identity=project_identity, limit=limit)

    driver._sweep_run_drive_watches = observe_sweep
    try:
        yield calls
    finally:
        del driver._sweep_run_drive_watches
    assert dict(vars(driver)) == driver_before


def test_recovery_driver_has_exact_private_command_composition_surface(
    tmp_path: Path,
) -> None:
    from lockstep.runtime import service as service_module
    from lockstep.runtime import recovery_driver as recovery_driver_module
    from lockstep.runtime.effects.ledger import RunDriveWatch
    from lockstep.runtime.engine import Engine
    from lockstep.runtime.service import LockstepCommandService

    driver_type = getattr(recovery_driver_module, "RecoveryDriver", None)
    assert driver_type is not None
    constructor = signature(driver_type)
    assert tuple(constructor.parameters) == (
        "catalog",
        "runtime",
        "effects",
        "blobs",
        "migrator",
        "coordinator",
        "snapshot_resolver",
        "exclude_run_drive",
        "drive_recovered_run",
    )
    assert all(
        parameter.kind is Parameter.KEYWORD_ONLY
        for parameter in constructor.parameters.values()
    )
    constructor_hints = get_type_hints(driver_type.__init__)
    assert constructor_hints["drive_recovered_run"] == Callable[[str], bool]
    method = getattr(driver_type, "_drive_run_watch", None)
    assert method is not None
    assert tuple(
        (parameter.name, parameter.kind)
        for parameter in signature(method).parameters.values()
    ) == (
        ("self", Parameter.POSITIONAL_OR_KEYWORD),
        ("watch", Parameter.POSITIONAL_OR_KEYWORD),
    )
    hints = get_type_hints(method)
    assert hints == {"watch": RunDriveWatch, "return": bool}

    with _prepared_command(tmp_path) as command:
        assert type(command._recovery_driver) is driver_type

    assert not hasattr(LockstepCommandService, "_drive_run_watch")
    assert not hasattr(service_module, "RecoveryDriver")
    runtime_package = import_module("lockstep.runtime")
    assert not hasattr(runtime_package, "RecoveryDriver")

    projection = Engine.observe(tmp_path / "state", tmp_path / "recipes")
    try:
        assert not any(
            isinstance(value, driver_type)
            for value in vars(projection).values()
        )
    finally:
        projection.close()


def test_recovery_driver_has_exact_private_sweep_surface() -> None:
    from lockstep.runtime import recovery_driver as recovery_driver_module

    driver_type = getattr(recovery_driver_module, "RecoveryDriver", None)
    assert driver_type is not None
    method = getattr(driver_type, "_sweep_run_drive_watches", None)
    assert method is not None, "R2a.1 must expose the sole private sweep boundary"
    assert tuple(
        (parameter.name, parameter.kind)
        for parameter in signature(method).parameters.values()
    ) == (
        ("self", Parameter.POSITIONAL_OR_KEYWORD),
        ("project_identity", Parameter.KEYWORD_ONLY),
        ("limit", Parameter.KEYWORD_ONLY),
    )
    hints = get_type_hints(method)
    assert hints == {
        "project_identity": str | None,
        "limit": int,
        "return": tuple[str, ...],
    }


def test_sweep_limit_counts_accepted_drives_not_scanned_rows(
    monkeypatch,
) -> None:
    from lockstep.runtime.effects.ledger import RunDriveWatch
    from lockstep.runtime.recovery_driver import RecoveryDriver

    admitted_at = datetime(2026, 8, 27, tzinfo=UTC)
    watches = tuple(
        RunDriveWatch(index, f"run-{index:03d}", None, None, admitted_at)
        for index in range(1, 132)
    )
    list_calls = []
    max_calls = []
    sweep_order = []

    def list_watches(*, after_admission_seq, high_water, limit):
        list_calls.append((after_admission_seq, high_water, limit))
        return tuple(
            watch
            for watch in watches
            if after_admission_seq < watch.admission_seq <= high_water
        )[:limit]

    def capture_max():
        max_calls.append(True)
        sweep_order.append("high-water")
        return 131

    def apply_backfill_page():
        sweep_order.append("backfill")
        return ()

    driver = object.__new__(RecoveryDriver)
    driver._exclude_run_drive = lambda _run_id: False
    driver._backfill = SimpleNamespace(apply_next_page=apply_backfill_page)
    driver._effects = SimpleNamespace(
        max_run_drive_admission_seq=capture_max,
        list_run_drive_watches=list_watches,
    )
    driven = []
    monkeypatch.setattr(driver, "_matches_project", lambda *_args: True)

    def drive(watch):
        driven.append(watch.admission_seq)
        return watch.admission_seq >= 130

    monkeypatch.setattr(driver, "_drive_run_watch", drive)

    assert driver._sweep_run_drive_watches(
        project_identity=None, limit=1
    ) == ("run-130",)
    assert max_calls == [True]
    assert sweep_order == ["high-water", "backfill"]
    assert list_calls == [(0, 131, 128), (128, 131, 128)]
    assert driven == list(range(1, 131))


def test_backfill_page_drives_only_exact_inserted_cohort_above_high_water(
    monkeypatch,
) -> None:
    from lockstep.runtime.effects.ledger import RunDriveWatch
    from lockstep.runtime.recovery_driver import RecoveryDriver

    admitted_at = datetime(2026, 8, 27, tzinfo=UTC)
    ordinary = RunDriveWatch(2, "ordinary", None, None, admitted_at)
    migrated = RunDriveWatch(3, "migrated", None, None, admitted_at)
    concurrent = RunDriveWatch(4, "concurrent", None, None, admitted_at)
    exact_calls = []

    def exact_watches(public_run_ids):
        exact_calls.append(public_run_ids)
        assert concurrent.public_run_id not in public_run_ids
        return (migrated,)

    driver = object.__new__(RecoveryDriver)
    driver._backfill = SimpleNamespace(
        apply_next_page=lambda: (migrated.public_run_id,)
    )
    driver._effects = SimpleNamespace(
        max_run_drive_admission_seq=lambda: ordinary.admission_seq,
        list_run_drive_watches=lambda **_kwargs: (ordinary,),
        list_run_drive_watches_by_public_run_ids=exact_watches,
    )
    driven = []
    monkeypatch.setattr(
        driver,
        "_try_drive_run_watch",
        lambda watch, project: driven.append((watch.public_run_id, project))
        or watch.public_run_id == "migrated",
    )

    assert driver._sweep_run_drive_watches(
        project_identity="/project", limit=1
    ) == ("migrated",)
    assert exact_calls == [("migrated",)]
    assert driven == [
        ("ordinary", "/project"),
        ("migrated", "/project"),
    ]


def test_ordinary_acceptance_exhausts_shared_budget_before_backfill_cohort(
    monkeypatch,
) -> None:
    from lockstep.runtime.effects.ledger import RunDriveWatch
    from lockstep.runtime.recovery_driver import RecoveryDriver

    admitted_at = datetime(2026, 8, 27, tzinfo=UTC)
    ordinary = RunDriveWatch(1, "ordinary", None, None, admitted_at)
    exact_calls = []
    driver = object.__new__(RecoveryDriver)
    driver._backfill = SimpleNamespace(apply_next_page=lambda: ("migrated",))
    driver._effects = SimpleNamespace(
        max_run_drive_admission_seq=lambda: ordinary.admission_seq,
        list_run_drive_watches=lambda **_kwargs: (ordinary,),
        list_run_drive_watches_by_public_run_ids=lambda ids: exact_calls.append(
            ids
        ),
    )
    driven = []
    monkeypatch.setattr(
        driver,
        "_try_drive_run_watch",
        lambda watch, project: driven.append((watch.public_run_id, project))
        or True,
    )

    assert driver._sweep_run_drive_watches(
        project_identity="/project", limit=1
    ) == ("ordinary",)
    assert driven == [("ordinary", "/project")]
    assert exact_calls == []


@pytest.mark.parametrize("accepted", (False, True))
def test_non_decision_watch_delegates_only_public_run_id(
    accepted: bool,
) -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor
    from lockstep.runtime.effects.ledger import RunDriveWatch
    from lockstep.runtime.effects.models import ScopeDescriptor
    from lockstep.runtime.recovery_driver import RecoveryDriver

    run_id = "run-1"
    binding = SimpleNamespace(public_run_id=run_id)
    interrupt = SimpleNamespace(
        value={"lockstep_effect": {"schema": "lockstep.effect/v1"}},
        coordinate=SimpleNamespace(),
    )
    snapshot = SimpleNamespace(
        checkpoint_id="checkpoint",
        pending=(interrupt,),
        next=(),
    )
    terminal = SimpleNamespace(
        checkpoint_id="terminal",
        pending=(),
        next=(),
    )
    snapshots = iter((snapshot, terminal)) if accepted else iter((snapshot,))
    events = []
    bound = {}

    def current_binding(_run_id):
        if run_id not in bound:
            raise KeyError(run_id)
        return bound[run_id]

    def bind_runtime(recovered_binding):
        created = run_id not in bound
        bound[run_id] = recovered_binding
        return created

    def unbind_runtime(_run_id):
        bound.pop(run_id, None)
        events.append(("unbind", run_id))

    def read_snapshot(_run_id, **_kwargs):
        assert run_id in bound
        value = next(snapshots)
        if value is terminal:
            events.append(("snapshot", run_id))
        return value

    def drive_recovered(*args, **kwargs):
        events.append(("drive", args, kwargs, run_id in bound))
        if accepted:
            bind_runtime(binding)
        return accepted

    driver = object.__new__(RecoveryDriver)
    driver._catalog = SimpleNamespace(get=lambda _run_id: binding)
    driver._runtime = SimpleNamespace(
        binding=current_binding,
        bind=bind_runtime,
        unbind=unbind_runtime,
        snapshot=read_snapshot,
    )
    driver._drive_recovered_run = drive_recovered
    driver._coordinator = SimpleNamespace(
        reconcile_consumed=lambda recovered_run_id: (
            events.append(("reconcile", recovered_run_id)) or ()
        )
    )
    driver._effects = SimpleNamespace(
        acknowledge_run_drive_watch=lambda recovered_run_id: events.append(
            ("acknowledge", recovered_run_id)
        )
    )
    raw_descriptor = {
        "schema": "lockstep.effect/v1",
        "kind": "scope",
        "logical_id": "parallel-scope",
        "scope_kind": "parallel",
        "duration_seconds": 300,
        "runner_selector": None,
        "ancestor_deadline_state_keys": [],
        "result_state_key": "parallel_scope_result",
        "result_schema": "lockstep.scope-result/v1",
    }
    assert isinstance(parse_effect_descriptor(raw_descriptor), ScopeDescriptor)
    interrupt.value["lockstep_effect"] = raw_descriptor
    watch = RunDriveWatch(
        1, run_id, None, None, datetime(2026, 8, 27, tzinfo=UTC)
    )

    assert driver._drive_run_watch(watch) is accepted
    expected = [("unbind", run_id), ("drive", (run_id,), {}, False)]
    if accepted:
        expected.extend(
            (
                ("snapshot", run_id),
                ("reconcile", run_id),
                ("acknowledge", run_id),
            )
        )
    assert events == expected


def test_sweep_excludes_fresh_admission_without_stopping_other_work(
    monkeypatch,
) -> None:
    from lockstep.runtime.effects.ledger import RunDriveWatch
    from lockstep.runtime.recovery_driver import RecoveryDriver

    admitted_at = datetime(2026, 8, 27, tzinfo=UTC)
    watches = (
        RunDriveWatch(1, "fresh", None, None, admitted_at),
        RunDriveWatch(2, "other-work", None, None, admitted_at),
    )
    driver = object.__new__(RecoveryDriver)
    driver._exclude_run_drive = lambda run_id: run_id == "fresh"
    driver._backfill = SimpleNamespace(apply_next_page=lambda: ())
    driver._effects = SimpleNamespace(
        max_run_drive_admission_seq=lambda: 2,
        list_run_drive_watches=lambda **_kwargs: watches,
    )
    driven = []
    monkeypatch.setattr(driver, "_matches_project", lambda *_args: True)
    monkeypatch.setattr(
        driver,
        "_drive_run_watch",
        lambda watch: driven.append(watch.public_run_id) or True,
    )

    assert driver._sweep_run_drive_watches(
        project_identity=None,
        limit=1,
    ) == ("other-work",)
    assert driven == ["other-work"]


def test_prepared_command_driver_observes_dynamic_fresh_admission_exclusion(
    tmp_path,
    monkeypatch,
) -> None:
    from lockstep.runtime.effects.ledger import RunDriveWatch

    admitted_at = datetime(2026, 8, 27, tzinfo=UTC)
    watch = RunDriveWatch(1, "fresh", None, None, admitted_at)
    with _prepared_command(tmp_path) as command:
        driver = command._recovery_driver  # noqa: SLF001 - composition contract
        driven = []
        monkeypatch.setattr(driver, "_matches_project", lambda *_args: True)
        monkeypatch.setattr(
            driver,
            "_drive_run_watch",
            lambda item: driven.append(item.public_run_id) or True,
        )

        command._initial_recovery_exclusion = "fresh"  # noqa: SLF001
        assert driver._try_drive_run_watch(watch, None) is False  # noqa: SLF001
        assert driven == []

        command._initial_recovery_exclusion = None  # noqa: SLF001
        assert driver._try_drive_run_watch(watch, None) is True  # noqa: SLF001
        assert driven == ["fresh"]


def test_sweep_isolates_only_known_per_run_integrity_errors(
    monkeypatch, caplog
) -> None:
    import logging

    from lockstep.runtime.effects.ledger import RunDriveWatch
    from lockstep.runtime.project_snapshots import SnapshotStorageError
    from lockstep.runtime.recovery_driver import RecoveryDriver

    admitted_at = datetime(2026, 8, 27, tzinfo=UTC)
    watches = tuple(
        RunDriveWatch(index, f"run-{index}", None, None, admitted_at)
        for index in (1, 2)
    )
    driver = object.__new__(RecoveryDriver)
    driver._exclude_run_drive = lambda _run_id: False
    driver._backfill = SimpleNamespace(apply_next_page=lambda: ())
    driver._effects = SimpleNamespace(
        max_run_drive_admission_seq=lambda: 2,
        list_run_drive_watches=lambda **_kwargs: watches,
    )
    monkeypatch.setattr(driver, "_matches_project", lambda *_args: True)

    def drive(watch):
        if watch.admission_seq == 1:
            raise SnapshotStorageError("sensitive owner-state detail")
        return True

    monkeypatch.setattr(driver, "_drive_run_watch", drive)

    with caplog.at_level(logging.WARNING, logger="lockstep.runtime.recovery_driver"):
        recovered = driver._sweep_run_drive_watches(
            project_identity=None, limit=1
        )

    assert recovered == ("run-2",)
    assert tuple(record.getMessage() for record in caplog.records) == (
        "run-drive recovery skipped run-1 after SnapshotStorageError",
    )
    assert "sensitive owner-state detail" not in caplog.text

    def fail_globally(_watch):
        raise RuntimeError("global failure")

    monkeypatch.setattr(driver, "_drive_run_watch", fail_globally)
    with pytest.raises(RuntimeError, match="global failure"):
        driver._sweep_run_drive_watches(project_identity=None, limit=1)


def test_automatic_recovery_reaches_inert_sweep_once(tmp_path: Path) -> None:
    with _prepared_command(tmp_path) as command:
        durable_before = _durable_command_state(command)
        drive_before = _command_drive_state(command)
        with _observed_sweeps(command) as calls:
            command._recover_engine_effects()

        assert {
            "durable_unchanged": _durable_command_state(command) == durable_before,
            "drive_unchanged": _command_drive_state(command) == drive_before,
            "sweep_calls": calls,
        } == {
            "durable_unchanged": True,
            "drive_unchanged": True,
            "sweep_calls": [
                (None, command._MAX_ACTIVE_EFFECT_RUNS, True, False)
            ],
        }


def test_explicit_recovery_reaches_inert_project_sweep_once(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with _active_command(tmp_path) as command:
        durable_before = _durable_command_state(command)
        drive_before = _command_drive_state(command)
        with _observed_sweeps(command) as calls:
            result = command.scenario_recover(str(project), limit=7)

        assert {
            "result": result,
            "durable_unchanged": _durable_command_state(command) == durable_before,
            "drive_unchanged": _command_drive_state(command) == drive_before,
            "sweep_calls": calls,
        } == {
            "result": {"recovered": [], "count": 0, "limit": 7},
            "durable_unchanged": True,
            "drive_unchanged": True,
            "sweep_calls": [(str(project.resolve()), 7, False, False)],
        }


def test_recovery_driver_isolates_invalid_legacy_binding_write_free(
    tmp_path: Path,
) -> None:
    from lockstep.runtime.catalog import RunBinding
    from lockstep.runtime.storage import (
        LegacyRunDriveClassification,
        RuntimeSchemaMigrator,
    )

    with _prepared_command(tmp_path) as command:
        command.catalog.create(
            RunBinding(
                "run-001",
                "thread-run-001",
                "a" * 64,
                "bundle:" + "b" * 64,
                "/project",
            )
        )
        RuntimeSchemaMigrator(command.store).apply_run_drive_watch_page(
            expected_after_public_run_id=None,
            classified=(
                LegacyRunDriveClassification("run-001", "nonterminal"),
            ),
            exhausted=False,
        )
        watches = command.effects.list_run_drive_watches(
            after_admission_seq=0,
            high_water=1,
            limit=1,
        )
        assert len(watches) == 1
        watch = watches[0]
        assert watch.input_blob_sha256 is None
        assert watch.input_blob_size is None

        durable_before = _durable_command_state(command)
        drive_before = _command_drive_state(command)
        driver_before = dict(vars(command._recovery_driver))
        statements: list[str] = []

        def observe_sql(
            _connection, _cursor, statement, _parameters, _context, _many
        ) -> None:
            statements.append(statement)

        event.listen(command.store.engine, "before_cursor_execute", observe_sql)
        outcome = None
        failure = None
        try:
            outcome = command._recovery_driver._drive_run_watch(watch)
        except Exception as exc:  # temporary staged surface must remain observable
            failure = exc
        finally:
            event.remove(
                command.store.engine,
                "before_cursor_execute",
                observe_sql,
            )

        writes = tuple(
            statement
            for statement in statements
            if statement.lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE", "REPLACE")
            )
        )
        assert writes == ()
        assert _durable_command_state(command) == durable_before
        assert _command_drive_state(command) == drive_before
        assert dict(vars(command._recovery_driver)) == driver_before
        assert failure is None
        assert outcome is False
