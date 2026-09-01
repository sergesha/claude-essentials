"""Task 12 B1 driver REDs for restart-safe paged legacy backfill."""

from __future__ import annotations

import logging
from pathlib import Path

from lockstep.runtime.graph_runtime import GraphRuntime
from lockstep.runtime.read_resources import RuntimeReadResources
from lockstep.runtime.recovery_driver import RecoveryDriver
from lockstep.runtime.storage import RuntimeSchemaMigrator, SQLiteStore
from tests.runtime._run_drive_b1_harness import prepared_native_reopen
from tests.runtime._run_drive_backfill_b1_harness import (
    seed_backfill_population,
)


class _PageCommittedCrash(BaseException):
    pass


def _migration_facts(state_dir: Path):
    store = SQLiteStore(state_dir / "runtime.sqlite")
    try:
        with store.read_connection() as connection:
            rows = connection.execute(
                store.tables.runtime_schema_migrations.select()
            ).all()
        return tuple(
            (
                row.name,
                row.schema_version,
                row.after_public_run_id,
                row.completed_at is not None,
            )
            for row in rows
        )
    finally:
        store.close()


def _install_page_observers(monkeypatch, applied, snapshot_calls):
    apply_page = RuntimeSchemaMigrator.apply_run_drive_watch_page
    snapshot = GraphRuntime.snapshot

    def crash_after_page(
        self,
        *,
        expected_after_public_run_id,
        classified,
        exhausted,
    ):
        progress = apply_page(
            self,
            expected_after_public_run_id=expected_after_public_run_id,
            classified=classified,
            exhausted=exhausted,
        )
        applied.append((classified, progress))
        raise _PageCommittedCrash

    def observe_snapshot(self, run_id: str, *, subgraphs: bool = False):
        observed = snapshot(self, run_id, subgraphs=subgraphs)
        snapshot_calls.append((run_id, observed))
        return observed

    monkeypatch.setattr(
        RuntimeSchemaMigrator, "apply_run_drive_watch_page", crash_after_page
    )
    monkeypatch.setattr(GraphRuntime, "snapshot", observe_snapshot)
    return apply_page


def _run_page_crashes(population) -> int:
    crashes = 0
    for _attempt in range(2):
        with prepared_native_reopen(
            population.state_dir,
            population.recipes_dir,
            population.runtime_context,
        ) as command:
            try:
                command._recovery_driver._sweep_run_drive_watches(
                    project_identity=str(population.project.resolve()),
                    limit=128,
                )
            except _PageCommittedCrash:
                crashes += 1
                continue
            break
    return crashes


def _drive_completed_backfill(population, monkeypatch):
    drive_watch = RecoveryDriver._drive_run_watch
    reached = []

    def observe_drive(self, watch):
        reached.append(watch)
        return drive_watch(self, watch)

    monkeypatch.setattr(RecoveryDriver, "_drive_run_watch", observe_drive)
    with prepared_native_reopen(
        population.state_dir,
        population.recipes_dir,
        population.runtime_context,
    ) as command:
        high_water = command.effects.max_run_drive_admission_seq()
        assert high_water is not None
        stored = command.effects.list_run_drive_watches(
            after_admission_seq=0,
            high_water=high_water,
            limit=128,
        )
        command._recovery_driver._sweep_run_drive_watches(
            project_identity=str(population.project.resolve()),
            limit=128,
        )
    return stored, reached


def _run_crashing_backfill_processes(population, monkeypatch):
    applied = []
    snapshot_calls = []
    apply_page = _install_page_observers(
        monkeypatch, applied, snapshot_calls
    )
    crashes = _run_page_crashes(population)
    monkeypatch.setattr(
        RuntimeSchemaMigrator, "apply_run_drive_watch_page", apply_page
    )
    stored_before_drive = ()
    reached = []
    if crashes == 2:
        stored_before_drive, reached = _drive_completed_backfill(
            population, monkeypatch
        )
    return crashes, applied, snapshot_calls, stored_before_drive, reached


def _observed_facts(
    population, crashes, applied, snapshot_calls, stored_before_drive, reached
):
    classified_pages = tuple(
        tuple((item.public_run_id, item.disposition) for item in classified)
        for classified, _progress in applied
    )
    progress = tuple(
        (
            item.after_public_run_id,
            item.completed,
            item.inserted_public_run_ids,
            item.malformed_public_run_ids,
        )
        for _classified, item in applied
    )
    snapshot_prefix = tuple(run_id for run_id, _snapshot in snapshot_calls[:129])
    reached_watches = tuple(
        (
            watch.public_run_id,
            watch.input_blob_sha256,
            watch.input_blob_size,
        )
        for watch in reached
    )
    stored_watches = tuple(
        (watch.public_run_id, watch.input_blob_sha256, watch.input_blob_size)
        for watch in stored_before_drive
    )
    resources = RuntimeReadResources(population.state_dir)
    target_binding = resources.binding_for(
        population.target_id, str(population.project.resolve())
    )
    assert target_binding is not None
    with resources.native_app(target_binding) as app:
        target_after = app.snapshot(
            thread_id=population.target_thread_id, subgraphs=True
        )
    return {
        "page_commit_crashes": crashes,
        "classified_pages": classified_pages,
        "progress": progress,
        "snapshot_prefix": snapshot_prefix,
        "stored_before_drive": stored_watches,
        "reached_watches": reached_watches,
        "migration": _migration_facts(population.state_dir),
        "target_completed": target_after.pending == target_after.next == (),
    }


def _expected_facts(population):
    return {
        "page_commit_crashes": 2,
        "classified_pages": (
            tuple((run_id, "malformed") for run_id in population.malformed_ids),
            (
                (population.terminal_id, "terminal"),
                (population.target_id, "nonterminal"),
            ),
        ),
        "progress": (
            (
                population.malformed_ids[-1],
                False,
                (),
                population.malformed_ids,
            ),
            (population.target_id, True, (population.target_id,), ()),
        ),
        "snapshot_prefix": (
            *population.malformed_ids[1:],
            population.terminal_id,
            population.target_id,
        ),
        "stored_before_drive": ((population.target_id, None, None),),
        "reached_watches": ((population.target_id, None, None),),
        "migration": (
            ("run-drive-watch-v2", 2, population.target_id, True),
        ),
        "target_completed": True,
    }


def test_backfill_progress_survives_restart_past_terminal_prefix(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(logging.root.manager, "disable", logging.INFO)
    population = seed_backfill_population(tmp_path)
    traces = _run_crashing_backfill_processes(population, monkeypatch)

    assert _observed_facts(population, *traces) == _expected_facts(population)
