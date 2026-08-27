"""B1 RED state classification for the pre-open schema transition."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from lockstep.runtime.advisory_lock import advisory_file_lock
from lockstep.runtime.storage import RuntimeSchemaMigrator
from tests.runtime._runtime_schema_transition_b1 import (
    poison_effects_table_ddl,
    poison_extra_legacy_schema_object,
    poison_mixed_legacy_schema,
    poison_orphan_legacy_watch,
    poison_v2_epoch_one,
    poison_v2_missing_epoch,
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
    ),
    ids=(
        "legacy-same-names-wrong-ddl",
        "v2-same-names-wrong-ddl",
        "mixed-v2",
        "extra-view",
        "orphan-watch",
        "v2-epoch-one",
        "v2-missing-epoch",
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
