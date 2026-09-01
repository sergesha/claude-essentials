"""B1 RED state classification for the pre-open schema transition."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from lockstep.runtime.advisory_lock import advisory_file_lock
from lockstep.runtime.storage import RuntimeSchemaMigrator
from tests.runtime._runtime_schema_transition_b1 import (
    poison_effects_table_ddl,
    poison_extra_legacy_schema_object,
    poison_invalid_legacy_watch_values,
    poison_mixed_legacy_schema,
    poison_orphan_legacy_watch,
    poison_oversized_legacy_watch,
    poison_v2_epoch_one,
    poison_v2_missing_epoch,
    poison_v2_watch_check_quoting,
    poison_v2_watch_conflict_comment,
    poison_v2_watch_generated_column,
    poison_v2_watch_without_autoincrement,
    seed_zero_size_legacy_watch,
    seed_empty_database,
    seed_exact_legacy_database,
    seed_exact_v2_database,
)
from tests.runtime._sqlite_store_image import StoreImage


def _prepare_schema_fence(database: Path) -> None:
    with advisory_file_lock(database.parent / "runtime-schema.lock"):
        pass


def _transition_error_type(database: Path) -> type[Exception] | None:
    try:
        RuntimeSchemaMigrator.transition_legacy_to_v2(database)
    except Exception as exc:
        return type(exc)
    return None


def test_transition_leaves_zero_length_store_uninitialized(tmp_path: Path) -> None:
    database = tmp_path / "empty.sqlite"
    seed_empty_database(database)
    before = StoreImage.capture(database)

    errors = (
        _transition_error_type(database),
        _transition_error_type(database),
    )

    assert errors == (None, None)
    assert StoreImage.capture(database) == before
    assert tuple(path.name for path in tmp_path.iterdir()) == ("empty.sqlite",)


def test_transition_accepts_exact_v2_twice_write_free(tmp_path: Path) -> None:
    database = tmp_path / "current.sqlite"
    seed_exact_v2_database(database)
    _prepare_schema_fence(database)
    before = StoreImage.capture(database)

    errors = (
        _transition_error_type(database),
        _transition_error_type(database),
    )

    assert errors == (None, None)
    assert StoreImage.capture(database) == before


def test_transition_retry_after_committed_legacy_upgrade_is_v2_noop(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite"
    seed_exact_legacy_database(database)
    RuntimeSchemaMigrator.transition_legacy_to_v2(database)
    committed = StoreImage.capture(database)

    RuntimeSchemaMigrator.transition_legacy_to_v2(database)

    assert StoreImage.capture(database) == committed


def test_transition_preserves_exact_zero_byte_legacy_watch(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite"
    seed_exact_legacy_database(database)
    seed_zero_size_legacy_watch(database)

    RuntimeSchemaMigrator.transition_legacy_to_v2(database)

    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT input_blob_sha256, input_blob_size FROM run_drive_watches "
            "WHERE public_run_id = 'zero-size-watch'"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("b" * 64, 0)


@pytest.mark.parametrize(
    ("seed", "poison"),
    (
        (seed_exact_legacy_database, poison_effects_table_ddl),
        (seed_exact_v2_database, poison_effects_table_ddl),
        (seed_exact_legacy_database, poison_mixed_legacy_schema),
        (seed_exact_legacy_database, poison_extra_legacy_schema_object),
        (seed_exact_legacy_database, poison_orphan_legacy_watch),
        (seed_exact_v2_database, poison_v2_epoch_one),
        (seed_exact_v2_database, poison_v2_missing_epoch),
        (seed_exact_v2_database, poison_v2_watch_check_quoting),
        (seed_exact_v2_database, poison_v2_watch_conflict_comment),
        (seed_exact_v2_database, poison_v2_watch_generated_column),
        (seed_exact_v2_database, poison_v2_watch_without_autoincrement),
        (seed_exact_legacy_database, poison_invalid_legacy_watch_values),
        (seed_exact_legacy_database, poison_oversized_legacy_watch),
    ),
    ids=(
        "legacy-same-names-wrong-ddl",
        "v2-same-names-wrong-ddl",
        "mixed-v2",
        "extra-view",
        "orphan-watch",
        "v2-epoch-one",
        "v2-missing-epoch",
        "v2-poisoned-check-quoting",
        "v2-poisoned-conflict-comment",
        "v2-poisoned-generated-column",
        "v2-missing-autoincrement",
        "legacy-invalid-watch-values",
        "legacy-oversized-watch",
    ),
)
def test_transition_rejects_noncanonical_existing_state_write_free(
    tmp_path: Path,
    seed: Callable[[Path], None],
    poison: Callable[[Path], None],
) -> None:
    database = tmp_path / "runtime.sqlite"
    seed(database)
    poison(database)
    _prepare_schema_fence(database)
    before = StoreImage.capture(database)

    disposition = "returned"
    try:
        RuntimeSchemaMigrator.transition_legacy_to_v2(database)
    except NotImplementedError:
        disposition = "staged"
    except Exception:
        disposition = "rejected"

    assert {
        "disposition": disposition,
        "database_unchanged": StoreImage.capture(database) == before,
    } == {
        "disposition": "rejected",
        "database_unchanged": True,
    }
