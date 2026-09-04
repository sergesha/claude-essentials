"""Observable compatibility behavior for existing runtime stores."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from lockstep.runtime.engine import Engine, LockstepError
from lockstep.runtime.owner_state import seal_owner_file
from tests.runtime._legacy_run_drive_fixtures import (
    AutoGrantAuthority,
    compile_recipe,
    stop_pump,
)
from tests.runtime._sqlite_store_image import StoreImage
from tests.runtime.providers.fakes import FakeRunner, _legacy_command_service

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
    command._reconstruct_runtime_execution_context = lambda **_kwargs: None
    try:
        command.scenario_recover(str(project), limit=1)
    finally:
        command.close()


def _replace_with_populated_legacy_store(database: Path, source: Path) -> None:
    """Copy coherent runtime facts into the frozen source-controlled v1 schema."""

    database.replace(source)
    shutil.copyfile(LEGACY_DATABASE, database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("ATTACH DATABASE ? AS current", (str(source),))
        for table in (
            "runs",
            "run_start_inputs",
            "effect_runtime_inputs",
            "consent_epochs",
            "publication_consents",
            "leases",
            "effects",
            "effect_observations",
        ):
            connection.execute(f"INSERT INTO {table} SELECT * FROM current.{table}")
        connection.execute(
            "INSERT INTO effect_dispatch_watches "
            "(public_run_id, input_blob_sha256, input_blob_size, admitted_at) "
            "SELECT public_run_id, input_blob_sha256, input_blob_size, admitted_at "
            "FROM current.run_drive_watches ORDER BY admission_seq"
        )
        connection.commit()
    finally:
        connection.close()
    seal_owner_file(database, writable=True)


def _observe(
    state: Path, recipes: Path, project: Path, run_id: str
) -> tuple[dict, list[dict]]:
    observer = Engine.observe(state, recipes)
    try:
        return observer.status(run_id, str(project)), observer.events(
            run_id, str(project)
        )
    finally:
        observer.close()


def test_populated_legacy_store_preserves_public_state_after_migration_and_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes, managed = compile_recipe(
        tmp_path,
        "persisted-effect",
        "  - verify:\n"
        "      id: persisted-effect\n"
        "      command: pytest -q\n"
        "      timeout: 60\n",
    )
    _recipes, worker = compile_recipe(
        tmp_path,
        "resume-after-migration",
        "  - step: edit\n"
        "    task: Edit the project\n"
        "    exit: Editing is complete\n",
    )
    state = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    database = state / "runtime.sqlite"
    authority = AutoGrantAuthority()
    runner = FakeRunner()
    command = _legacy_command_service(
        state,
        recipes,
        runners={"pinned": runner},
        effect_authority=authority,
    )
    stop_pump(command)
    try:
        real_start = command.runtime.ensure_started

        def crash_before_first_checkpoint(_run_id, _values):
            raise RuntimeError("crash before first checkpoint")

        monkeypatch.setattr(
            command.runtime, "ensure_started", crash_before_first_checkpoint
        )
        with pytest.raises(RuntimeError, match="crash before first checkpoint"):
            command.start(
                "resume-after-migration",
                {},
                str(project),
                compiler_provenance=worker.compiler_provenance,
            )
        monkeypatch.setattr(command.runtime, "ensure_started", real_start)
        watched_run_id = command.catalog.list(str(project.resolve()))[0].public_run_id

        effect_run_id = command.start(
            "persisted-effect",
            {},
            str(project),
            compiler_provenance=managed.compiler_provenance,
        )["run_id"]
        effect = command.effects.list_for_thread(
            command.catalog.get(effect_run_id).thread_id
        )[0]
        effect_status, effect_events = _observe(
            state, recipes, project, effect_run_id
        )
    finally:
        command.close()

    _replace_with_populated_legacy_store(
        database, tmp_path / "populated-v2-source.sqlite"
    )

    _recover(state, recipes, project)
    migrated_status, migrated_events = _observe(
        state, recipes, project, effect_run_id
    )
    migrated_watch, _watched_events = _observe(
        state, recipes, project, watched_run_id
    )
    _recover(state, recipes, project)
    reopened_status, reopened_events = _observe(
        state, recipes, project, effect_run_id
    )
    reopened_watch, _watched_events = _observe(
        state, recipes, project, watched_run_id
    )

    assert effect_status["status"] == "running"
    assert any(event.get("effect_id") == effect.effect_id for event in effect_events)
    assert migrated_status == reopened_status == effect_status
    assert migrated_events == reopened_events == effect_events
    assert migrated_watch == reopened_watch
    assert reopened_watch["status"] == "awaiting"
    assert reopened_watch["owner"] == "worker"
    assert reopened_watch["step"] == "edit"


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
