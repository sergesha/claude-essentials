"""Observable compatibility behavior for existing runtime stores."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from lockstep.runtime.engine import Engine, LockstepError
from lockstep.runtime.owner_state import seal_owner_file

from tests.runtime._sqlite_store_image import StoreImage

LEGACY_DATABASE = (
    Path(__file__).parents[1] / "fixtures/runtime/legacy-runtime-v1.sqlite"
)


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    state = tmp_path / "state"
    recipes = tmp_path / "recipes"
    state.mkdir(mode=0o700)
    recipes.mkdir()
    return state, recipes, state / "runtime.sqlite"


def _seed_supported_legacy_store(database: Path) -> None:
    shutil.copyfile(LEGACY_DATABASE, database)
    seal_owner_file(database, writable=True)


def _recover(state: Path, recipes: Path, project: Path) -> None:
    command = Engine.command(state, recipes)
    try:
        command.scenario_recover(str(project), limit=1)
    finally:
        command.close()


def test_supported_legacy_store_opens_and_reopens_through_public_engine(
    tmp_path: Path,
) -> None:
    state, recipes, database = _paths(tmp_path)
    _seed_supported_legacy_store(database)

    _recover(state, recipes, tmp_path)
    _recover(state, recipes, tmp_path)


def test_unknown_legacy_shape_is_rejected_without_modifying_the_store(
    tmp_path: Path,
) -> None:
    state, recipes, database = _paths(tmp_path)
    _seed_supported_legacy_store(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE VIEW unsupported_view AS SELECT 1 AS value")
    seal_owner_file(database, writable=True)
    before = StoreImage.capture(database)

    with pytest.raises((LockstepError, RuntimeError)):
        _recover(state, recipes, tmp_path)

    assert StoreImage.capture(database) == before
