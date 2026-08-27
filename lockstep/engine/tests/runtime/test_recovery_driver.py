"""Private command-owned recovery-driver boundary."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from importlib import import_module
from inspect import Parameter, signature
from pathlib import Path
from typing import get_type_hints

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
        command._recovery_thread_cursor,
        tuple(sorted(command._scenario_recovery_cursors.items())),
        command._pump_thread,
        command._pump_stop.is_set(),
        command._pump_wakeup.is_set(),
        command._pump_failure,
    )


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


def test_automatic_recovery_reaches_inert_sweep_once(tmp_path: Path) -> None:
    with _prepared_command(tmp_path) as command:
        durable_before = _durable_command_state(command)
        drive_before = _command_drive_state(command)
        driver = command._recovery_driver
        driver_before = dict(vars(driver))
        sweep = driver._sweep_run_drive_watches
        calls: list[tuple[str | None, int, bool]] = []

        def observe_sweep(
            *,
            project_identity: str | None,
            limit: int,
        ) -> tuple[str, ...]:
            contender_acquired: list[bool] = []

            def probe_recovery_lock() -> None:
                acquired = command._admission_recovery_lock.acquire(blocking=False)
                contender_acquired.append(acquired)
                if acquired:
                    command._admission_recovery_lock.release()

            contender = threading.Thread(target=probe_recovery_lock)
            contender.start()
            contender.join(timeout=5)
            assert not contender.is_alive()
            calls.append((project_identity, limit, contender_acquired[0]))
            return sweep(project_identity=project_identity, limit=limit)

        driver._sweep_run_drive_watches = observe_sweep
        try:
            command._recover_engine_effects()
        finally:
            del driver._sweep_run_drive_watches

        assert {
            "durable_unchanged": _durable_command_state(command) == durable_before,
            "drive_unchanged": _command_drive_state(command) == drive_before,
            "driver_unchanged": dict(vars(driver)) == driver_before,
            "sweep_calls": calls,
        } == {
            "durable_unchanged": True,
            "drive_unchanged": True,
            "driver_unchanged": True,
            "sweep_calls": [(None, command._MAX_ACTIVE_EFFECT_RUNS, False)],
        }


def test_recovery_driver_returns_false_without_sql_or_state_change(
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

        assert statements == []
        assert _durable_command_state(command) == durable_before
        assert _command_drive_state(command) == drive_before
        assert dict(vars(command._recovery_driver)) == driver_before
        assert failure is None
        assert outcome is False
