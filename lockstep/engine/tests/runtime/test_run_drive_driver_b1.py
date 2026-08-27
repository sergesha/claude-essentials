"""Task 12 B1 run-drive policy REDs against real native checkpoints."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from lockstep.runtime.storage import (
    LegacyRunDriveClassification,
    RuntimeSchemaMigrator,
)
from tests.runtime._run_drive_b1_harness import (
    active_native_command,
    active_native_manual_park,
)


def _replace_with_null_watch(command, run_id: str):
    command.effects.acknowledge_run_drive_watch(run_id)
    RuntimeSchemaMigrator(command.store).apply_run_drive_watch_page(
        expected_after_public_run_id=None,
        classified=(LegacyRunDriveClassification(run_id, "nonterminal"),),
        exhausted=False,
    )
    high_water = command.effects.max_run_drive_admission_seq()
    assert high_water is not None
    watches = command.effects.list_run_drive_watches(
        after_admission_seq=0,
        high_water=high_water,
        limit=1,
    )
    assert len(watches) == 1
    watch = watches[0]
    assert watch.public_run_id == run_id
    assert watch.input_blob_sha256 is None
    assert watch.input_blob_size is None
    return high_water, watches


@contextmanager
def _observe_null_watch_drive(command):
    snapshot = command.runtime.snapshot
    read_blob = command.blobs.read
    ensure_started = command.runtime.ensure_started
    snapshot_calls: list[tuple[str, bool]] = []

    def observe_snapshot(observed_run_id: str, *, subgraphs: bool = False):
        snapshot_calls.append((observed_run_id, subgraphs))
        return snapshot(observed_run_id, subgraphs=subgraphs)

    def reject_blob_read(_reference):
        raise AssertionError("a null-input watch must never read a blob")

    def reject_start(_run_id, _values):
        raise AssertionError("a null-input watch must never start native execution")

    command.runtime.snapshot = observe_snapshot
    command.blobs.read = reject_blob_read
    command.runtime.ensure_started = reject_start
    try:
        yield snapshot_calls
    finally:
        command.runtime.snapshot = snapshot
        command.blobs.read = read_blob
        command.runtime.ensure_started = ensure_started


def test_start_watch_replays_only_before_first_checkpoint(tmp_path: Path) -> None:
    with active_native_manual_park(tmp_path) as (command, run_id, _project):
        binding = command.catalog.get(run_id)
        native_before = command.runtime.snapshot(run_id, subgraphs=True)
        assert native_before.checkpoint_id
        effects_before = command.effects.list_for_thread(binding.thread_id)

        high_water, watches_before = _replace_with_null_watch(command, run_id)
        watch = watches_before[0]
        snapshot = command.runtime.snapshot
        with _observe_null_watch_drive(command) as snapshot_calls:
            outcome = command._recovery_driver._drive_run_watch(watch)

        high_water_after = command.effects.max_run_drive_admission_seq()
        assert high_water_after == high_water
        watches_after = command.effects.list_run_drive_watches(
            after_admission_seq=0,
            high_water=high_water_after,
            limit=1,
        )
        native_after = snapshot(run_id, subgraphs=True)
        effects_after = command.effects.list_for_thread(binding.thread_id)
        assert {
            "snapshot_calls": snapshot_calls,
            "watch_unchanged": watches_after == watches_before,
            "native_unchanged": native_after == native_before,
            "effects_unchanged": effects_after == effects_before,
        } == {
            "snapshot_calls": [(run_id, True)],
            "watch_unchanged": True,
            "native_unchanged": True,
            "effects_unchanged": True,
        }
        assert type(outcome) is bool


def test_null_watch_without_checkpoint_remains_safely_blocked(
    tmp_path: Path,
) -> None:
    with active_native_command(tmp_path) as (command, project):
        ensure_started = command.runtime.ensure_started

        def crash_before_first_checkpoint(_run_id, _values):
            raise RuntimeError("crash before first checkpoint")

        command.runtime.ensure_started = crash_before_first_checkpoint
        try:
            with pytest.raises(RuntimeError, match="crash before first checkpoint"):
                command.start("native-parent-direct", {}, str(project))
        finally:
            command.runtime.ensure_started = ensure_started

        bindings = command.catalog.list(str(project.resolve()))
        assert len(bindings) == 1
        binding = bindings[0]
        run_id = binding.public_run_id

        command.runtime.bind(binding)
        snapshot = command.runtime.snapshot
        native_before = snapshot(run_id, subgraphs=True)
        assert not native_before.checkpoint_id
        effects_before = command.effects.list_for_thread(binding.thread_id)

        high_water, watches_before = _replace_with_null_watch(command, run_id)
        watch = watches_before[0]
        with _observe_null_watch_drive(command) as snapshot_calls:
            outcome = command._recovery_driver._drive_run_watch(watch)

        watches_after = command.effects.list_run_drive_watches(
            after_admission_seq=0,
            high_water=high_water,
            limit=1,
        )
        native_after = snapshot(run_id, subgraphs=True)
        effects_after = command.effects.list_for_thread(binding.thread_id)
        assert outcome is False
        assert watches_after == watches_before
        assert native_after == native_before
        assert effects_after == effects_before
        assert snapshot_calls in ([], [(run_id, True)])
