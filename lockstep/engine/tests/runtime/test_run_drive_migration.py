"""Task 12R0 B-schema REDs for migration metadata and epoch fencing."""

from __future__ import annotations

from datetime import UTC, datetime
from inspect import Parameter, signature
from pathlib import Path
from typing import get_type_hints

import pytest
from sqlalchemy import inspect as sa_inspect


MIGRATION_COLUMNS = (
    "name", "schema_version", "after_public_run_id", "completed_at", "updated_at"
)


def test_run_drive_migration_ddl_contract(tmp_path: Path) -> None:
    from lockstep.runtime.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "runtime.db")
    try:
        migrations = getattr(store.tables, "runtime_schema_migrations", None)
        epoch = getattr(store.tables, "runtime_schema_epoch", None)
        assert migrations is not None, "R2a must declare migration progress"
        assert epoch is not None, "R2a must declare the singleton schema epoch"
        assert tuple(migrations.c.keys()) == MIGRATION_COLUMNS
        assert tuple(epoch.c.keys()) == ("singleton", "epoch")
        assert migrations.c.name.primary_key
        assert not migrations.c.schema_version.nullable
        assert migrations.c.after_public_run_id.nullable
        assert migrations.c.completed_at.nullable
        assert not migrations.c.updated_at.nullable
        assert epoch.c.singleton.primary_key
        assert not epoch.c.epoch.nullable
    finally:
        store.close()


def test_migration_metadata_is_not_scheduler_state(tmp_path: Path) -> None:
    from lockstep.runtime.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "runtime.db")
    try:
        table = getattr(store.tables, "runtime_schema_migrations", None)
        assert table is not None, "R2a must expose only schema-upgrade progress"
        assert tuple(table.c.keys()) == MIGRATION_COLUMNS
        forbidden = {
            "coordinate", "pending_kind", "route", "status", "outcome",
            "owner", "effect_phase", "grant",
        }
        assert forbidden.isdisjoint(table.c.keys())
    finally:
        store.close()


def test_runtime_schema_epoch_singleton_is_v2(tmp_path: Path) -> None:
    from lockstep.runtime.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "runtime.db")
    try:
        epoch = getattr(store.tables, "runtime_schema_epoch", None)
        assert epoch is not None, "R2a must persist the singleton schema epoch"
        with store.read_connection() as connection:
            rows = connection.execute(epoch.select()).all()
        assert [(row.singleton, row.epoch) for row in rows] == [(1, 2)]
    finally:
        store.close()


def test_runtime_schema_epoch_has_singleton_check_constraint(tmp_path: Path) -> None:
    from lockstep.runtime.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "runtime.db")
    try:
        epoch = getattr(store.tables, "runtime_schema_epoch", None)
        assert epoch is not None, "R2a must persist the singleton schema epoch"
        checks = {
            "".join(item["sqltext"].lower().split())
            for item in sa_inspect(store.engine).get_check_constraints(
                "runtime_schema_epoch"
            )
        }
        assert any("singleton=1" in sql for sql in checks)
    finally:
        store.close()


def test_v2_write_transaction_rejects_legacy_epoch_before_yield_write_free(
    tmp_path: Path,
) -> None:
    from lockstep.runtime.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "runtime.db")
    try:
        epoch = store.tables.runtime_schema_epoch
        migrations = store.tables.runtime_schema_migrations
        with store.engine.begin() as connection:
            connection.execute(epoch.update().values(epoch=1))

        entered = False
        with pytest.raises(
            RuntimeError,
            match="^runtime schema epoch 2 is required for v2 writes$",
        ):
            with store._v2_write_transaction() as connection:
                entered = True
                connection.execute(
                    migrations.insert().values(
                        name="must-not-commit",
                        schema_version=2,
                        after_public_run_id=None,
                        completed_at=None,
                        updated_at="2026-08-27T00:00:00+00:00",
                    )
                )
        assert entered is False

        with store.read_connection() as connection:
            observed_epoch = connection.execute(epoch.select()).one().epoch
            forbidden = connection.execute(
                migrations.select().where(migrations.c.name == "must-not-commit")
            ).first()
        assert observed_epoch == 1
        assert forbidden is None
    finally:
        store.close()


def _migration_type(name: str):
    from lockstep.runtime import storage as storage_module

    value = getattr(storage_module, name, None)
    assert value is not None, f"R2a must declare exact {name}"
    return value


def test_legacy_run_drive_classification_exact_dto_fields() -> None:
    classification_type = _migration_type("LegacyRunDriveClassification")
    assert tuple(classification_type.__dataclass_fields__) == (
        "public_run_id",
        "disposition",
    )


def test_legacy_run_drive_classification_accepts_only_frozen_value_domain() -> None:
    classification_type = _migration_type("LegacyRunDriveClassification")

    for disposition in ("nonterminal", "terminal", "malformed"):
        classification = classification_type("run-1", disposition)
        assert classification.public_run_id == "run-1"
        assert classification.disposition == disposition

    for public_run_id, disposition in (
        ("", "nonterminal"),
        ("run-1", ""),
        ("run-1", "running"),
        ("run-1", "NONTERMINAL"),
    ):
        with pytest.raises(ValueError):
            classification_type(public_run_id, disposition)


def test_migration_progress_exact_dto_fields() -> None:
    progress_type = _migration_type("MigrationProgress")
    assert tuple(progress_type.__dataclass_fields__) == (
        "after_public_run_id",
        "completed",
        "inserted_public_run_ids",
        "malformed_public_run_ids",
    )


def test_migration_progress_accepts_exact_values_and_requires_strict_boolean() -> None:
    progress_type = _migration_type("MigrationProgress")

    empty = progress_type(None, False, (), ())
    assert (
        empty.after_public_run_id,
        empty.completed,
        empty.inserted_public_run_ids,
        empty.malformed_public_run_ids,
    ) == (None, False, (), ())

    populated = progress_type("run-2", True, ("run-1",), ("run-2",))
    assert (
        populated.after_public_run_id,
        populated.completed,
        populated.inserted_public_run_ids,
        populated.malformed_public_run_ids,
    ) == ("run-2", True, ("run-1",), ("run-2",))

    with pytest.raises(TypeError, match="completed must be a boolean"):
        progress_type(None, 1, (), ())


def test_migration_progress_requires_exact_public_id_shapes() -> None:
    progress_type = _migration_type("MigrationProgress")

    assert progress_type(None, False, (), ()).after_public_run_id is None
    populated = progress_type("run-2", True, ("run-1",), ("run-2",))
    assert (
        populated.after_public_run_id,
        populated.inserted_public_run_ids,
        populated.malformed_public_run_ids,
    ) == ("run-2", ("run-1",), ("run-2",))

    for after_public_run_id in ("", 1):
        with pytest.raises(
            ValueError,
            match="^after_public_run_id must be a non-empty string$",
        ):
            progress_type(after_public_run_id, False, (), ())

    with pytest.raises(
        TypeError,
        match="^inserted_public_run_ids must be a tuple$",
    ):
        progress_type(None, False, ["run-1"], ())
    with pytest.raises(
        TypeError,
        match="^malformed_public_run_ids must be a tuple$",
    ):
        progress_type(None, False, (), ["run-1"])

    for inserted_public_run_ids in (("",), (1,)):
        with pytest.raises(
            ValueError,
            match="^inserted_public_run_ids must contain non-empty strings$",
        ):
            progress_type(None, False, inserted_public_run_ids, ())
    for malformed_public_run_ids in (("",), (1,)):
        with pytest.raises(
            ValueError,
            match="^malformed_public_run_ids must contain non-empty strings$",
        ):
            progress_type(None, False, (), malformed_public_run_ids)


def test_migration_progress_result_ids_are_sorted_unique_disjoint_and_bounded() -> None:
    progress_type = _migration_type("MigrationProgress")
    ids = tuple(f"run-{index:03d}" for index in range(128))

    boundary = progress_type("run-127", False, ids[:64], ids[64:])
    assert boundary.inserted_public_run_ids == ids[:64]
    assert boundary.malformed_public_run_ids == ids[64:]

    for inserted_public_run_ids in (
        ("run-002", "run-001"),
        ("run-001", "run-001"),
    ):
        with pytest.raises(
            ValueError,
            match="^inserted_public_run_ids must be sorted and unique$",
        ):
            progress_type(None, False, inserted_public_run_ids, ())
    for malformed_public_run_ids in (
        ("run-002", "run-001"),
        ("run-001", "run-001"),
    ):
        with pytest.raises(
            ValueError,
            match="^malformed_public_run_ids must be sorted and unique$",
        ):
            progress_type(None, False, (), malformed_public_run_ids)

    with pytest.raises(
        ValueError,
        match="^migration progress result IDs must be disjoint$",
    ):
        progress_type(None, False, ("run-001",), ("run-001",))

    too_many_ids = tuple(f"run-{index:03d}" for index in range(129))
    with pytest.raises(
        ValueError,
        match="^migration progress result IDs must contain at most 128 entries$",
    ):
        progress_type(None, False, too_many_ids[:64], too_many_ids[64:])


def test_run_drive_migration_page_api_exact_signature() -> None:
    from lockstep.runtime import storage as storage_module

    migrator_type = getattr(storage_module, "RuntimeSchemaMigrator", None)
    assert migrator_type is not None, "R2a must expose the private migrator"
    method = getattr(migrator_type, "apply_run_drive_watch_page", None)
    assert callable(method)
    observed = tuple(
        (name, parameter.kind)
        for name, parameter in signature(method).parameters.items()
    )
    assert observed == (
        ("self", Parameter.POSITIONAL_OR_KEYWORD),
        ("expected_after_public_run_id", Parameter.KEYWORD_ONLY),
        ("classified", Parameter.KEYWORD_ONLY),
        ("exhausted", Parameter.KEYWORD_ONLY),
    )
    progress_type = getattr(storage_module, "MigrationProgress", None)
    assert progress_type is not None
    assert get_type_hints(method)["return"] == progress_type


def test_apply_run_drive_watch_page_rejects_invalid_page_domain_write_free(
    tmp_path: Path,
) -> None:
    from lockstep.runtime.storage import (
        LegacyRunDriveClassification,
        RuntimeSchemaMigrator,
        SQLiteStore,
    )

    store = SQLiteStore(tmp_path / "runtime.db")
    try:
        migrator = RuntimeSchemaMigrator(store)
        valid_record = LegacyRunDriveClassification("run-001", "nonterminal")

        with pytest.raises(TypeError, match="^classified must be a tuple$"):
            migrator.apply_run_drive_watch_page(
                expected_after_public_run_id=None,
                classified=[valid_record],
                exhausted=False,
            )
        with pytest.raises(
            TypeError,
            match=(
                "^classified must contain LegacyRunDriveClassification records$"
            ),
        ):
            migrator.apply_run_drive_watch_page(
                expected_after_public_run_id=None,
                classified=("run-001",),
                exhausted=False,
            )

        too_many = tuple(
            LegacyRunDriveClassification(f"run-{index:03d}", "nonterminal")
            for index in range(129)
        )
        with pytest.raises(
            ValueError,
            match="^classified must contain at most 128 records$",
        ):
            migrator.apply_run_drive_watch_page(
                expected_after_public_run_id=None,
                classified=too_many,
                exhausted=False,
            )

        for ids in (
            ("run-002", "run-001"),
            ("run-001", "run-001"),
        ):
            classified = tuple(
                LegacyRunDriveClassification(public_run_id, "nonterminal")
                for public_run_id in ids
            )
            with pytest.raises(
                ValueError,
                match="^classified public_run_ids must be sorted and unique$",
            ):
                migrator.apply_run_drive_watch_page(
                    expected_after_public_run_id=None,
                    classified=classified,
                    exhausted=False,
                )

        for first_id in ("run-002", "run-001"):
            with pytest.raises(
                ValueError,
                match=(
                    "^classified public_run_ids must be strictly after "
                    "expected_after_public_run_id$"
                ),
            ):
                migrator.apply_run_drive_watch_page(
                    expected_after_public_run_id="run-002",
                    classified=(
                        LegacyRunDriveClassification(first_id, "nonterminal"),
                    ),
                    exhausted=False,
                )

        for expected_after_public_run_id in ("", 1):
            with pytest.raises(
                ValueError,
                match=(
                    "^expected_after_public_run_id must be a non-empty string$"
                ),
            ):
                migrator.apply_run_drive_watch_page(
                    expected_after_public_run_id=expected_after_public_run_id,
                    classified=(),
                    exhausted=False,
                )
        for exhausted in (0, 1, None):
            with pytest.raises(TypeError, match="^exhausted must be a boolean$"):
                migrator.apply_run_drive_watch_page(
                    expected_after_public_run_id=None,
                    classified=(),
                    exhausted=exhausted,
                )

        with store.read_connection() as connection:
            migration_rows = connection.execute(
                store.tables.runtime_schema_migrations.select()
            ).all()
            watch_rows = connection.execute(
                store.tables.run_drive_watches.select()
            ).all()
        assert migration_rows == []
        assert watch_rows == []
    finally:
        store.close()


def test_apply_run_drive_watch_page_durably_initializes_empty_non_exhausted_progress(
    tmp_path: Path,
) -> None:
    from lockstep.runtime.storage import (
        MigrationProgress,
        RuntimeSchemaMigrator,
        SQLiteStore,
    )

    database_path = tmp_path / "runtime.db"
    store = SQLiteStore(database_path)
    reopened = None
    try:
        before = datetime.now(UTC)
        progress = RuntimeSchemaMigrator(store).apply_run_drive_watch_page(
            expected_after_public_run_id=None,
            classified=(),
            exhausted=False,
        )
        after = datetime.now(UTC)
        assert progress == MigrationProgress(None, False, (), ())

        store.close()
        reopened = SQLiteStore(database_path)
        with reopened.read_connection() as connection:
            rows = connection.execute(
                reopened.tables.runtime_schema_migrations.select()
            ).all()
            watches = connection.execute(
                reopened.tables.run_drive_watches.select()
            ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.name == "run-drive-watch-v2"
        assert row.schema_version == 2
        assert row.after_public_run_id is None
        assert row.completed_at is None
        updated_at = datetime.fromisoformat(row.updated_at)
        assert updated_at.tzinfo is not None
        assert updated_at.utcoffset() is not None
        assert row.updated_at == updated_at.astimezone(UTC).isoformat()
        assert before <= updated_at <= after
        assert watches == []
    finally:
        if reopened is not None:
            reopened.close()
        store.close()


def test_apply_run_drive_watch_page_advances_terminal_and_malformed_without_watches(
    tmp_path: Path,
) -> None:
    from lockstep.runtime.catalog import RunBinding, RunCatalog
    from lockstep.runtime.storage import (
        LegacyRunDriveClassification,
        MigrationProgress,
        RuntimeSchemaMigrator,
        SQLiteStore,
    )

    database_path = tmp_path / "runtime.db"
    store = SQLiteStore(database_path)
    reopened = None
    try:
        catalog = RunCatalog(store)
        for index in (1, 2):
            catalog.create(
                RunBinding(
                    f"run-{index:03d}",
                    f"thread-{index:03d}",
                    "a" * 64,
                    "bundle:" + "b" * 64,
                    "/project",
                )
            )
        migrator = RuntimeSchemaMigrator(store)
        migrator.apply_run_drive_watch_page(
            expected_after_public_run_id=None,
            classified=(),
            exhausted=False,
        )

        before = datetime.now(UTC)
        progress = migrator.apply_run_drive_watch_page(
            expected_after_public_run_id=None,
            classified=(
                LegacyRunDriveClassification("run-001", "terminal"),
                LegacyRunDriveClassification("run-002", "malformed"),
            ),
            exhausted=False,
        )
        after = datetime.now(UTC)
        assert progress == MigrationProgress(
            after_public_run_id="run-002",
            completed=False,
            inserted_public_run_ids=(),
            malformed_public_run_ids=("run-002",),
        )

        store.close()
        reopened = SQLiteStore(database_path)
        with reopened.read_connection() as connection:
            rows = connection.execute(
                reopened.tables.runtime_schema_migrations.select()
            ).all()
            watches = connection.execute(
                reopened.tables.run_drive_watches.select()
            ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.name == "run-drive-watch-v2"
        assert row.schema_version == 2
        assert row.after_public_run_id == "run-002"
        assert row.completed_at is None
        updated_at = datetime.fromisoformat(row.updated_at)
        assert row.updated_at == updated_at.astimezone(UTC).isoformat()
        assert before <= updated_at <= after
        assert watches == []
    finally:
        if reopened is not None:
            reopened.close()
        store.close()
