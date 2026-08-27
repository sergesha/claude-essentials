"""Recovery trace for the B1 migration-metadata control."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine as SQLAlchemyEngine

from lockstep.runtime.engine import Engine
from lockstep.runtime.storage import RuntimeSchemaMigrator
from tests.runtime._run_drive_b1_harness import prepared_native_reopen
from tests.runtime._run_drive_backfill_b1_harness import (
    complete_backfill_with_driver,
    seed_backfill_population,
)


@contextmanager
def _prepared_command(tmp_path: Path):
    recipes = tmp_path / "recipes"
    recipes.mkdir(parents=True)
    command = Engine.command(tmp_path / "state", recipes)
    try:
        command._prepare_writable_core()
        yield command
    finally:
        command._rollback_writable_core_activation()
        command.close()


@contextmanager
def _observe_recovery(command):
    phase = ["automatic"]
    metadata_sql = []
    sweep_calls = []
    driver = command._recovery_driver
    sweep = driver._sweep_run_drive_watches

    def observe_sql(
        _connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        normalized = " ".join(
            statement.lower().replace('"', "").replace("`", "").split()
        )
        operation = normalized.split(maxsplit=1)[0]
        if (
            operation in {"select", "update"}
            and "runtime_schema_migrations" in normalized
        ):
            metadata_sql.append((phase[0], normalized))

    def observe_sweep(*, project_identity: str | None, limit: int):
        sweep_calls.append((phase[0], project_identity, limit))
        return sweep(project_identity=project_identity, limit=limit)

    driver._sweep_run_drive_watches = observe_sweep
    event.listen(SQLAlchemyEngine, "before_cursor_execute", observe_sql)
    try:
        yield phase, metadata_sql, sweep_calls
    finally:
        event.remove(SQLAlchemyEngine, "before_cursor_execute", observe_sql)
        del driver._sweep_run_drive_watches


def _migration_row(command) -> tuple[dict[str, object], ...]:
    with command.store.read_connection() as connection:
        rows = connection.execute(
            command.store.tables.runtime_schema_migrations.select()
        ).all()
    return tuple(dict(row._mapping) for row in rows)


def _stop_pump(command) -> None:
    command._pump_stop.set()
    command._pump_wakeup.set()
    thread = command._pump_thread
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive()


def assert_neutral_recovery_ignores_migration_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    with _prepared_command(tmp_path) as command:
        progress = RuntimeSchemaMigrator(
            command.store
        ).apply_run_drive_watch_page(
            expected_after_public_run_id=None,
            classified=(),
            exhausted=False,
        )
        assert (
            progress.after_public_run_id,
            progress.completed,
            progress.inserted_public_run_ids,
            progress.malformed_public_run_ids,
        ) == (None, False, (), ())
        before = _migration_row(command)
        assert len(before) == 1
        assert (
            before[0]["name"],
            before[0]["schema_version"],
            before[0]["after_public_run_id"],
            before[0]["completed_at"],
        ) == ("run-drive-watch-v2", 2, None, None)
        assert command.effects.max_run_drive_admission_seq() is None

        command._pump_stop.set()
        with _observe_recovery(command) as (
            phase,
            metadata_sql,
            sweep_calls,
        ):
            command._finish_writable_core_activation()
            _stop_pump(command)
            phase[0] = "explicit"
            result = command.scenario_recover(str(project), limit=7)
        after = _migration_row(command)

        assert {
            "metadata_sql": metadata_sql,
            "sweep_calls": sweep_calls,
            "row_unchanged": after == before,
            "explicit_result": result,
        } == {
            "metadata_sql": [],
            "sweep_calls": [
                ("automatic", None, command._MAX_ACTIVE_EFFECT_RUNS),
                ("explicit", str(project.resolve()), 7),
            ],
            "row_unchanged": True,
            "explicit_result": {"recovered": [], "count": 0, "limit": 7},
        }


def assert_completed_recovery_ignores_migration_metadata(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True)
    population = seed_backfill_population(tmp_path)
    complete_backfill_with_driver(population)
    with prepared_native_reopen(
        population.state_dir,
        population.recipes_dir,
        population.runtime_context,
    ) as command:
        progress = RuntimeSchemaMigrator(
            command.store
        ).run_drive_watch_migration_state()
        assert progress is not None and progress.completed
        assert progress.after_public_run_id == population.target_id
        assert command.effects.max_run_drive_admission_seq() is None

    with prepared_native_reopen(
        population.state_dir,
        population.recipes_dir,
        population.runtime_context,
    ) as command:
        before = _migration_row(command)
        command._pump_stop.set()
        with _observe_recovery(command) as (
            phase,
            metadata_sql,
            sweep_calls,
        ):
            command._finish_writable_core_activation()
            _stop_pump(command)
            phase[0] = "explicit"
            result = command.scenario_recover(
                str(population.project.resolve()), limit=7
            )
        after = _migration_row(command)

        assert metadata_sql == []
        assert sweep_calls == [
            ("automatic", None, command._MAX_ACTIVE_EFFECT_RUNS),
            ("explicit", str(population.project.resolve()), 7),
        ]
        assert after == before
        assert result == {"recovered": [], "count": 0, "limit": 7}
