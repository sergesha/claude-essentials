"""B1 RED command pre-open routing for the exact runtime-schema transition."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import lockstep.runtime.service as service_module
from lockstep.runtime.engine import Engine
from lockstep.runtime.owner_state import initialize_owner_state
from lockstep.runtime.storage import RuntimeSchemaMigrator
from tests.runtime._runtime_schema_transition_b1 import (
    _database_shape,
    expected_database,
    seed_empty_database,
    seed_exact_legacy_database,
    seed_exact_v2_database,
)


@pytest.mark.parametrize(
    "seed",
    (seed_exact_legacy_database, seed_empty_database, seed_exact_v2_database),
    ids=("legacy", "empty", "v2"),
)
def test_command_activation_transitions_before_sqlite_store_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed: Callable[[Path], None],
) -> None:
    state = initialize_owner_state(tmp_path / "state")
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    database = state / "runtime.sqlite"
    seed(database)
    calls: list[str] = []
    original_transition = RuntimeSchemaMigrator.transition_legacy_to_v2
    original_store = service_module.SQLiteStore

    def transition(cls, path: Path) -> None:
        calls.append("transition")
        original_transition(path)

    def open_store(path: Path):
        calls.append("store")
        return original_store(path)

    monkeypatch.setattr(
        RuntimeSchemaMigrator,
        "transition_legacy_to_v2",
        classmethod(transition),
    )
    monkeypatch.setattr(service_module, "SQLiteStore", open_store)

    command = Engine.command(state, recipes)
    assert calls == []
    try:
        assert command.scenario_recover(str(project), limit=1) == {
            "recovered": [],
            "count": 0,
            "limit": 1,
        }
    finally:
        command.close()

    assert calls == ["transition", "store"]
    assert _database_shape(database) == expected_database(None)


def test_projection_never_calls_runtime_schema_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = initialize_owner_state(tmp_path / "state")
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    database = state / "runtime.sqlite"
    seed_exact_legacy_database(database)

    def forbidden_transition(cls, path: Path) -> None:
        raise AssertionError(f"projection attempted schema transition for {path}")

    monkeypatch.setattr(
        RuntimeSchemaMigrator,
        "transition_legacy_to_v2",
        classmethod(forbidden_transition),
    )

    projection = Engine.observe(state, recipes)
    try:
        assert projection.list_runs(str(project)) == []
    finally:
        projection.close()
