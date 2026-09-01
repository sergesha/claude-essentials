"""Real SQLite/process harness for the unwired runtime-schema transition."""

from __future__ import annotations

import multiprocessing
import sqlite3
import time
from pathlib import Path

from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, create_engine

from lockstep.runtime.owner_state import seal_owner_file
from lockstep.runtime.advisory_lock import AdvisoryLockTimeout, advisory_file_lock
from lockstep.runtime.storage import SQLiteStore, _define_tables


_V2_TABLES = {
    "run_drive_watches",
    "runtime_schema_migrations",
    "runtime_schema_epoch",
}
_EXPECTED_V2_TABLES = (
    "consent_epochs",
    "effect_observations",
    "effect_runtime_inputs",
    "effects",
    "leases",
    "publication_consents",
    "run_drive_watches",
    "run_start_inputs",
    "runs",
    "runtime_schema_epoch",
    "runtime_schema_migrations",
)
_RECIPE_DIGEST = "a" * 64
_INPUT_DIGEST = "b" * 64
_ADMITTED_AT = "2026-08-27T12:00:00+00:00"


def seed_exact_legacy_database(path: Path) -> None:
    metadata = MetaData()
    external = MetaData()
    _define_tables(metadata, external)
    Table(
        "effect_dispatch_watches",
        metadata,
        Column(
            "public_run_id",
            String,
            ForeignKey("runs.public_run_id"),
            primary_key=True,
        ),
        Column("input_blob_sha256", String(64), nullable=False),
        Column("input_blob_size", Integer, nullable=False),
        Column("admitted_at", String, nullable=False),
    )
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        metadata.create_all(
            engine,
            tables=tuple(
                table
                for table in metadata.sorted_tables
                if table.name not in _V2_TABLES
            ),
        )
        external.create_all(engine)
    finally:
        engine.dispose()
    seal_owner_file(path, writable=True)


def seed_empty_database(path: Path) -> None:
    path.touch(mode=0o600)
    seal_owner_file(path, writable=True)


def seed_exact_v2_database(path: Path) -> None:
    store = SQLiteStore(path)
    store.close()


def poison_effects_table_ddl(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("ALTER TABLE effects ADD COLUMN poison TEXT")
        connection.commit()
    finally:
        connection.close()


def poison_mixed_legacy_schema(path: Path) -> None:
    metadata = MetaData()
    external = MetaData()
    tables = _define_tables(metadata, external)
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        tables.runtime_schema_epoch.create(engine)
        with engine.begin() as connection:
            connection.execute(
                tables.runtime_schema_epoch.insert().values(singleton=1, epoch=2)
            )
    finally:
        engine.dispose()


def poison_extra_legacy_schema_object(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE VIEW poison_view AS SELECT 1 AS poison")
        connection.commit()
    finally:
        connection.close()


def poison_orphan_legacy_watch(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO effect_dispatch_watches VALUES (?, ?, ?, ?)",
            ("orphan", _INPUT_DIGEST, 2, _ADMITTED_AT),
        )
        connection.commit()
    finally:
        connection.close()


def poison_v2_epoch_one(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE runtime_schema_epoch SET epoch = 1")
        connection.commit()
    finally:
        connection.close()


def poison_v2_missing_epoch(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("DELETE FROM runtime_schema_epoch")
        connection.commit()
    finally:
        connection.close()


def poison_v2_watch_check_quoting(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type = 'table' AND name = 'run_drive_watches'"
        ).fetchone()
        assert row is not None
        poisoned = row[0].replace(
            "input_blob_sha256 IS NULL",
            '"input_blob_sha256 " IS NULL',
        ).replace(
            "input_blob_sha256 IS NOT NULL",
            '"input_blob_sha256 " IS NOT NULL',
        )
        assert poisoned != row[0]
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_schema SET sql = ? "
            "WHERE type = 'table' AND name = 'run_drive_watches'",
            (poisoned,),
        )
        version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
        connection.commit()
    finally:
        connection.close()


def poison_v2_watch_conflict_comment(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type = 'table' AND name = 'run_drive_watches'"
        ).fetchone()
        assert row is not None
        poisoned = row[0].replace(
            "UNIQUE (public_run_id)",
            "UNIQUE (public_run_id) ON/**/CONFLICT REPLACE",
        )
        assert poisoned != row[0]
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_schema SET sql = ? "
            "WHERE type = 'table' AND name = 'run_drive_watches'",
            (poisoned,),
        )
        version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
        connection.commit()
    finally:
        connection.close()


def poison_v2_watch_generated_column(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type = 'table' AND name = 'run_drive_watches'"
        ).fetchone()
        assert row is not None
        poisoned = row[0].replace(
            "admitted_at VARCHAR NOT NULL, ",
            "admitted_at VARCHAR NOT NULL, poison TEXT AS (input_blob_size), ",
        )
        assert poisoned != row[0]
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_schema SET sql = ? "
            "WHERE type = 'table' AND name = 'run_drive_watches'",
            (poisoned,),
        )
        version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
        connection.commit()
    finally:
        connection.close()


def poison_v2_watch_without_autoincrement(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type = 'table' AND name = 'run_drive_watches'"
        ).fetchone()
        assert row is not None
        poisoned = row[0].replace(" PRIMARY KEY AUTOINCREMENT", " PRIMARY KEY")
        assert poisoned != row[0]
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_schema SET sql = ? "
            "WHERE type = 'table' AND name = 'run_drive_watches'",
            (poisoned,),
        )
        version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
        connection.commit()
    finally:
        connection.close()


def poison_invalid_legacy_watch_values(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
            (
                "invalid-watch",
                "thread-invalid-watch",
                _RECIPE_DIGEST,
                "bundle:" + _INPUT_DIGEST,
                "/project",
                _ADMITTED_AT,
            ),
        )
        connection.execute(
            "INSERT INTO effect_dispatch_watches VALUES (?, ?, ?, ?)",
            ("invalid-watch", "not-a-digest", -1, "not-a-time"),
        )
        connection.commit()
    finally:
        connection.close()


def poison_oversized_legacy_watch(path: Path) -> None:
    _poison_legacy_watch_size(path, "oversized-watch", 64 * 1024 * 1024 + 1)


def seed_zero_size_legacy_watch(path: Path) -> None:
    _poison_legacy_watch_size(path, "zero-size-watch", 0)


def _poison_legacy_watch_size(path: Path, run_id: str, size: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                "thread-" + run_id,
                _RECIPE_DIGEST,
                "bundle:" + _INPUT_DIGEST,
                "/project",
                _ADMITTED_AT,
            ),
        )
        connection.execute(
            "INSERT INTO effect_dispatch_watches VALUES (?, ?, ?, ?)",
            (run_id, _INPUT_DIGEST, size, _ADMITTED_AT),
        )
        connection.commit()
    finally:
        connection.close()


def _legacy_writer(
    path,
    opened,
    begin_gate,
    ready,
    commit_gate,
    result,
    run_id: str,
) -> None:
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    signalled = False
    try:
        if opened is not None:
            opened.put(True)
        if begin_gate is not None:
            begin_gate.get(timeout=10)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                f"thread-{run_id}",
                _RECIPE_DIGEST,
                "bundle:" + _INPUT_DIGEST,
                "/project",
                _ADMITTED_AT,
            ),
        )
        connection.execute(
            "INSERT INTO effect_dispatch_watches VALUES (?, ?, ?, ?)",
            (run_id, _INPUT_DIGEST, 2, _ADMITTED_AT),
        )
        ready.put("holding")
        signalled = True
        commit_gate.get(timeout=10)
        connection.commit()
        result.put(None)
    except BaseException as exc:
        connection.rollback()
        if not signalled:
            ready.put("failed")
        result.put(type(exc).__name__)
    finally:
        connection.close()


def _transition(path, started, result) -> None:
    from lockstep.runtime.storage import RuntimeSchemaMigrator

    started.put(True)
    try:
        RuntimeSchemaMigrator.transition_legacy_to_v2(path)
    except BaseException as exc:
        result.put(type(exc).__name__)
    else:
        result.put(None)


def _fence_observer(path, release_writer, result) -> None:
    writer_released = False
    try:
        lock_path = path.parent / "runtime-schema.lock"
        try:
            with advisory_file_lock(lock_path, timeout=0.1):
                database = _database_shape_at_fence(path)
        except AdvisoryLockTimeout:
            release_writer.put(True)
            writer_released = True
            with advisory_file_lock(lock_path, timeout=10):
                database = _database_shape_at_fence(path)
    except BaseException as exc:
        result.put({"error": type(exc).__name__, "database": None})
    else:
        result.put({"error": None, "database": database})
    finally:
        if not writer_released:
            release_writer.put(True)


def _join(process: multiprocessing.Process) -> None:
    process.join(timeout=15)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
        raise AssertionError("schema-transition child did not terminate")


def _cleanup_processes(*processes: multiprocessing.Process) -> None:
    for process in processes:
        if process.pid is None:
            continue
        if process.is_alive():
            process.kill()
        process.join(timeout=5)


def _close_queues(*queues) -> None:
    for queue in queues:
        try:
            queue.close()
            queue.join_thread()
        except BaseException:
            pass


def _wait_for_schema_fence(path: Path, process: multiprocessing.Process) -> bool:
    lock_path = path.parent / "runtime-schema.lock"
    deadline = time.monotonic() + 5
    while process.is_alive() and time.monotonic() < deadline:
        if lock_path.exists():
            try:
                with advisory_file_lock(lock_path, timeout=0.05):
                    pass
            except AdvisoryLockTimeout:
                return True
        time.sleep(0.02)
    return False


def _database_shape_from(connection: sqlite3.Connection) -> dict[str, object]:
    tables = tuple(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    )
    epochs = tuple(
        connection.execute(
            "SELECT singleton, epoch FROM runtime_schema_epoch"
        )
    ) if "runtime_schema_epoch" in tables else ()
    runs = tuple(
        row[0] for row in connection.execute(
            "SELECT public_run_id FROM runs ORDER BY public_run_id"
        )
    )
    watches = tuple(
        connection.execute(
            "SELECT admission_seq, public_run_id, input_blob_sha256, "
            "input_blob_size, admitted_at FROM run_drive_watches "
            "ORDER BY admission_seq"
        )
    ) if "run_drive_watches" in tables else ()
    migrations = tuple(
        connection.execute(
            "SELECT name FROM runtime_schema_migrations ORDER BY name"
        )
    ) if "runtime_schema_migrations" in tables else ()
    return {
        "tables": tables,
        "epochs": epochs,
        "runs": runs,
        "watches": tuple(row[1:] for row in watches),
        "positive_unique_sequences": (
            all(type(row[0]) is int and row[0] > 0 for row in watches)
            and len({row[0] for row in watches}) == len(watches)
        ),
        "migrations": migrations,
    }


def _database_shape_at_fence(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        return _database_shape_from(connection)
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _database_shape(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(path)
    try:
        return _database_shape_from(connection)
    finally:
        connection.close()


def legacy_first(path: Path) -> dict[str, object]:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Queue()
    writer_result = context.Queue()
    transition_started = context.Queue()
    transition_result = context.Queue()
    observer_result = context.Queue()
    writer = context.Process(
        target=_legacy_writer,
        args=(
            path,
            None,
            None,
            ready,
            release,
            writer_result,
            "legacy-first",
        ),
    )
    transition = context.Process(
        target=_transition,
        args=(path, transition_started, transition_result),
    )
    observer = context.Process(
        target=_fence_observer,
        args=(path, release, observer_result),
    )
    try:
        writer.start()
        assert ready.get(timeout=10) == "holding"
        transition.start()
        assert transition_started.get(timeout=10) is True
        fence_held = _wait_for_schema_fence(path, transition)
        observer.start()
        _join(writer)
        _join(transition)
        _join(observer)
        return {
            "schema_fence_held": fence_held,
            "fence_observation": observer_result.get(timeout=5),
            "writer_error": writer_result.get(timeout=5),
            "transition_error": transition_result.get(timeout=5),
            "database": _database_shape(path),
        }
    finally:
        _cleanup_processes(writer, transition, observer)
        _close_queues(
            ready,
            release,
            writer_result,
            transition_started,
            transition_result,
            observer_result,
        )


def transition_first(path: Path) -> dict[str, object]:
    context = multiprocessing.get_context("spawn")
    opened = context.Queue()
    begin_gate = context.Queue()
    ready = context.Queue()
    commit_gate = context.Queue()
    writer_result = context.Queue()
    writer = context.Process(
        target=_legacy_writer,
        args=(
            path,
            opened,
            begin_gate,
            ready,
            commit_gate,
            writer_result,
            "transition-first",
        ),
    )
    transition_started = context.Queue()
    transition_result = context.Queue()
    transition = context.Process(
        target=_transition,
        args=(path, transition_started, transition_result),
    )
    try:
        writer.start()
        assert opened.get(timeout=10) is True
        transition.start()
        assert transition_started.get(timeout=10) is True
        _join(transition)
        begin_gate.put(True)
        writer_state = ready.get(timeout=10)
        if writer_state == "holding":
            commit_gate.put(True)
        _join(writer)
        writer_error = writer_result.get(timeout=5)
        return {
            "transition_error": transition_result.get(timeout=5),
            "legacy_process_opened_first": True,
            "writer_failed": writer_state == "failed" and writer_error is not None,
            "database": _database_shape(path),
        }
    finally:
        _cleanup_processes(writer, transition)
        _close_queues(
            opened,
            begin_gate,
            ready,
            commit_gate,
            writer_result,
            transition_started,
            transition_result,
        )


def expected_database(run_id: str | None) -> dict[str, object]:
    return {
        "tables": _EXPECTED_V2_TABLES,
        "epochs": ((1, 2),),
        "runs": () if run_id is None else (run_id,),
        "watches": () if run_id is None else (
            (run_id, _INPUT_DIGEST, 2, _ADMITTED_AT),
        ),
        "positive_unique_sequences": True,
        "migrations": (),
    }
