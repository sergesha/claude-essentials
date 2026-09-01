"""B1 RED freeze for every independent v2 command-side SQL writer."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.runtime._runtime_schema_writer_fence_b1 import (
    CASE_FACTORIES,
    EXPECTED_FENCE_ERROR,
    observe_action,
    prepare_epoch_one,
)


@pytest.mark.parametrize("case_name", tuple(CASE_FACTORIES), ids=tuple(CASE_FACTORIES))
def test_epoch_one_rejects_every_v2_command_writer_write_free(
    tmp_path: Path,
    case_name: str,
) -> None:
    from lockstep.runtime.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "runtime.sqlite")
    try:
        case = CASE_FACTORIES[case_name](store)
        prepare_epoch_one(store)
        observed = observe_action(case, store)

        assert observed == {
            "error": f"RuntimeError: {EXPECTED_FENCE_ERROR}",
            "logical_rows_unchanged": True,
            "sqlite_family_unchanged": True,
            "authority_entered": False,
        }
    finally:
        store.close()
