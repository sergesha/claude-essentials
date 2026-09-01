"""B1 RED for ordering the real legacy writer against the v2 transition."""

from __future__ import annotations

from pathlib import Path

from tests.runtime._runtime_schema_transition_b1 import (
    expected_database,
    legacy_first,
    seed_exact_legacy_database,
    transition_first,
)


def test_transition_orders_legacy_writer_against_epoch_two(
    tmp_path: Path,
) -> None:
    legacy_first_path = tmp_path / "legacy-first.sqlite"
    seed_exact_legacy_database(legacy_first_path)
    first = legacy_first(legacy_first_path)

    transition_first_path = tmp_path / "transition-first.sqlite"
    seed_exact_legacy_database(transition_first_path)
    second = transition_first(transition_first_path)

    assert {"legacy_first": first, "transition_first": second} == {
        "legacy_first": {
            "schema_fence_held": True,
            "fence_observation": {
                "error": None,
                "database": expected_database("legacy-first"),
            },
            "writer_error": None,
            "transition_error": None,
            "database": expected_database("legacy-first"),
        },
        "transition_first": {
            "transition_error": None,
            "legacy_process_opened_first": True,
            "writer_failed": True,
            "database": expected_database(None),
        },
    }
