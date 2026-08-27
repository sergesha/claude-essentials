"""B1 control for a completed driver-owned run-watch backfill."""

from __future__ import annotations

from pathlib import Path

from lockstep.runtime.catalog import RunCatalog
from lockstep.runtime.graph_runtime import GraphRuntime
from lockstep.runtime.recovery_driver import _RunDriveBackfill
from lockstep.runtime.storage import RuntimeSchemaMigrator
from tests.runtime._run_drive_b1_harness import prepared_native_reopen
from tests.runtime._run_drive_backfill_b1_harness import (
    complete_backfill_with_driver,
    seed_backfill_population,
)
from tests.runtime._sqlite_store_image import StoreImage


def _prepare_completed_baseline(population) -> None:
    complete_backfill_with_driver(population)
    with prepared_native_reopen(
        population.state_dir,
        population.recipes_dir,
        population.runtime_context,
    ) as command:
        progress = RuntimeSchemaMigrator(
            command.store
        ).run_drive_watch_migration_state()
        assert progress is not None and progress.completed
        assert progress.after_public_run_id == population.target_id
        assert command.effects.max_run_drive_admission_seq() is None


def test_completed_backfill_never_rescans_or_rearms_terminal_runs(
    tmp_path: Path, monkeypatch
) -> None:
    population = seed_backfill_population(tmp_path)
    _prepare_completed_baseline(population)
    database = population.state_dir / "runtime.sqlite"
    before = StoreImage.capture(database)

    catalog_scans = []
    classifications = []
    page_applies = []
    snapshots = []
    migration_reads = []
    real_snapshot = GraphRuntime.snapshot
    real_migration_read = RuntimeSchemaMigrator.run_drive_watch_migration_state

    def reject_catalog_scan(self, after_public_run_id, *, limit):
        catalog_scans.append((after_public_run_id, limit))
        raise AssertionError("completed backfill rescanned the legacy catalog")

    def reject_classification(self, binding):
        classifications.append(binding.public_run_id)
        raise AssertionError("completed backfill reclassified a legacy run")

    def reject_page_apply(self, **kwargs):
        page_applies.append(kwargs)
        raise AssertionError("completed backfill applied another migration page")

    def observe_snapshot(self, run_id, *, subgraphs=False):
        snapshots.append(run_id)
        return real_snapshot(self, run_id, subgraphs=subgraphs)

    def observe_migration_read(self):
        progress = real_migration_read(self)
        migration_reads.append(progress)
        return progress

    monkeypatch.setattr(
        RunCatalog, "list_after_public_run_id", reject_catalog_scan
    )
    monkeypatch.setattr(_RunDriveBackfill, "_classify", reject_classification)
    monkeypatch.setattr(
        RuntimeSchemaMigrator, "apply_run_drive_watch_page", reject_page_apply
    )
    monkeypatch.setattr(GraphRuntime, "snapshot", observe_snapshot)
    monkeypatch.setattr(
        RuntimeSchemaMigrator,
        "run_drive_watch_migration_state",
        observe_migration_read,
    )

    for _reopen in range(2):
        with prepared_native_reopen(
            population.state_dir,
            population.recipes_dir,
            population.runtime_context,
        ) as command:
            command._recovery_driver._sweep_run_drive_watches(
                project_identity=str(population.project.resolve()),
                limit=128,
            )

    after = StoreImage.capture(database)
    assert catalog_scans == []
    assert classifications == []
    assert page_applies == []
    assert len(migration_reads) == 2
    assert all(progress is not None and progress.completed for progress in migration_reads)
    assert snapshots == []
    assert after.logical_rows == before.logical_rows
    assert after.sqlite_family == before.sqlite_family
