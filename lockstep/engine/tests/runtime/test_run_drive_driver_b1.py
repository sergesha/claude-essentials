"""Task 12 B1 run-drive policy REDs against real native checkpoints."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from lockstep.runtime import sessions
from lockstep.runtime.read_resources import RuntimeReadResources
from lockstep.runtime.storage import (
    LegacyRunDriveClassification,
    RuntimeSchemaMigrator,
)
from tests.runtime._run_drive_b1_harness import (
    active_native_command,
    active_native_manual_park,
    prepared_native_reopen,
)


class _NativeCommittedBeforeEffectDelivery(RuntimeError):
    pass


class _PreDeleteCrash(RuntimeError):
    pass


def _snapshot_existing(command, run_id: str):
    command.runtime.bind(command.catalog.get(run_id))
    return command.runtime.snapshot(run_id, subgraphs=True)


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


def _terminal_residue(command, run_id: str, project: Path, effect_id: str):
    session_id = "item12-session"
    assert sessions.touch(command.state_dir, run_id, session_id, 30) == "bound"
    mark_delivered = command.effects.mark_delivered
    mark_calls = []

    def crash_after_native_commit(observed_effect_id, **_kwargs):
        mark_calls.append(observed_effect_id)
        raise _NativeCommittedBeforeEffectDelivery

    command.effects.mark_delivered = crash_after_native_commit
    try:
        with pytest.raises(_NativeCommittedBeforeEffectDelivery):
            command.scenario_done(
                run_id,
                "answer",
                {"answer": "yes"},
                session_id=session_id,
                project=str(project),
            )
    finally:
        command.effects.mark_delivered = mark_delivered

    terminal = _snapshot_existing(command, run_id)
    assert terminal.checkpoint_id
    assert terminal.pending == terminal.next == ()
    assert mark_calls == [effect_id]
    assert command.effects.get(effect_id).phase == "sealed"
    return terminal


def _drive_to_predelete_cut(command, watch, effect_id: str):
    reconcile_consumed = command.coordinator.reconcile_consumed
    acknowledge = command.effects.acknowledge_run_drive_watch
    observed = {"reconcile": [], "ack": [], "failure": None}

    def observe_reconcile(run_id: str):
        reports = reconcile_consumed(run_id)
        observed["reconcile"].append(
            tuple((item.effect_id, item.action, item.phase) for item in reports)
        )
        return reports

    def crash_before_delete(run_id: str):
        high_water = command.effects.max_run_drive_admission_seq()
        current = command.effects.list_run_drive_watches(
            after_admission_seq=0,
            high_water=high_water,
            limit=1,
        )
        observed["ack"].append(
            (run_id, command.effects.get(effect_id).phase, current)
        )
        raise _PreDeleteCrash("pre-delete")

    command.coordinator.reconcile_consumed = observe_reconcile
    command.effects.acknowledge_run_drive_watch = crash_before_delete
    try:
        try:
            command._recovery_driver._drive_run_watch(watch)
        except _PreDeleteCrash as exc:
            observed["failure"] = str(exc)
    finally:
        command.coordinator.reconcile_consumed = reconcile_consumed
        command.effects.acknowledge_run_drive_watch = acknowledge
    return observed


def _observe_busy_drive(command, watch):
    reconcile_consumed = command.coordinator.reconcile_consumed
    acknowledge = command.effects.acknowledge_run_drive_watch
    observed = {"reconcile": [], "ack": []}

    def observe_reconcile(run_id: str):
        reports = reconcile_consumed(run_id)
        observed["reconcile"].append(
            tuple((item.effect_id, item.action, item.phase) for item in reports)
        )
        return reports

    def prevent_delete(run_id: str):
        observed["ack"].append(run_id)

    command.coordinator.reconcile_consumed = observe_reconcile
    command.effects.acknowledge_run_drive_watch = prevent_delete
    try:
        command._recovery_driver._drive_run_watch(watch)
    finally:
        command.coordinator.reconcile_consumed = reconcile_consumed
        command.effects.acknowledge_run_drive_watch = acknowledge
    return observed


def _retry_terminal_watch(command, watch, effect_id: str):
    reconcile_consumed = command.coordinator.reconcile_consumed
    acknowledge = command.effects.acknowledge_run_drive_watch
    observed = {"reconcile": [], "ack": [], "outcome": None}

    def observe_reconcile(run_id: str):
        reports = reconcile_consumed(run_id)
        observed["reconcile"].append(
            tuple((item.effect_id, item.action, item.phase) for item in reports)
        )
        return reports

    def observe_delete(run_id: str):
        observed["ack"].append(
            (run_id, command.effects.get(effect_id).phase)
        )
        return acknowledge(run_id)

    command.coordinator.reconcile_consumed = observe_reconcile
    command.effects.acknowledge_run_drive_watch = observe_delete
    try:
        observed["outcome"] = command._recovery_driver._drive_run_watch(watch)
    finally:
        command.coordinator.reconcile_consumed = reconcile_consumed
        command.effects.acknowledge_run_drive_watch = acknowledge
    return observed


def test_start_watch_replays_only_before_first_checkpoint(tmp_path: Path) -> None:
    with active_native_manual_park(tmp_path) as (command, run_id, _project):
        binding = command.catalog.get(run_id)
        native_before = _snapshot_existing(command, run_id)
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
            "snapshot_calls": [(run_id, True), (run_id, True)],
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


def test_non_null_watch_replays_input_once_before_first_checkpoint(
    tmp_path: Path,
) -> None:
    with active_native_command(tmp_path) as (command, project):
        state_dir = command.state_dir
        recipes_dir = command.recipes_dir
        runtime_context = command._runtime_execution_context
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

    reads = []
    starts = []
    outcomes = []
    retained = []
    persisted = []
    for _attempt in range(2):
        with prepared_native_reopen(
            state_dir, recipes_dir, runtime_context
        ) as reopened:
            high_water = reopened.effects.max_run_drive_admission_seq()
            assert high_water is not None
            watches = reopened.effects.list_run_drive_watches(
                after_admission_seq=0,
                high_water=high_water,
                limit=1,
            )
            assert len(watches) == 1
            watch = watches[0]
            retained.append(watch)
            assert watch.input_blob_sha256 is not None
            assert watch.input_blob_size is not None

            read_blob = reopened.blobs.read
            ensure_started = reopened.runtime.ensure_started

            def observe_read(reference):
                reads.append(reference)
                return read_blob(reference)

            def observe_start(run_id, values):
                starts.append((run_id, values))
                return ensure_started(run_id, values)

            reopened.blobs.read = observe_read
            reopened.runtime.ensure_started = observe_start
            try:
                outcomes.append(
                    reopened._recovery_driver._drive_run_watch(watch)
                )
            finally:
                reopened.blobs.read = read_blob
                reopened.runtime.ensure_started = ensure_started

        with RuntimeReadResources(state_dir).native_app(binding) as app:
            persisted.append(
                app.snapshot(thread_id=binding.thread_id, subgraphs=True)
            )

    assert retained[0] == retained[1]
    assert len(reads) == 1
    assert reads[0].sha256 == retained[0].input_blob_sha256
    assert reads[0].size == retained[0].input_blob_size
    assert starts == [(binding.public_run_id, {})]
    assert persisted[0].checkpoint_id
    assert persisted[0] == persisted[1]
    assert outcomes == [True, False]


def test_terminal_removal_crash_cuts(tmp_path: Path) -> None:
    with active_native_manual_park(tmp_path) as (command, run_id, project):
        state_dir = command.state_dir
        recipes_dir = command.recipes_dir
        runtime_context = command._runtime_execution_context
        binding = command.catalog.get(run_id)
        records = command.effects.list_for_thread(binding.thread_id)
        assert len(records) == 1
        effect = records[0]
        assert (effect.effect_kind, effect.phase) == ("manual", "prepared")
        high_water, watches_before = _replace_with_null_watch(command, run_id)
        watch = watches_before[0]
        terminal = _terminal_residue(
            command, run_id, project, effect.effect_id
        )

        held = command.leases.acquire(
            "effect", effect.effect_id, "item12-contender", 30
        )
        try:
            busy = command.coordinator.reconcile_consumed(run_id)
            busy_drive = _observe_busy_drive(command, watch)
            busy_effect = command.effects.get(effect.effect_id).phase
            busy_watches = command.effects.list_run_drive_watches(
                after_admission_seq=0, high_water=high_water, limit=1
            )
            busy_native = _snapshot_existing(command, run_id)
        finally:
            assert command.leases.release(held)
        assert [(item.effect_id, item.action, item.phase) for item in busy] == [
            (effect.effect_id, "busy", "sealed")
        ]
        assert command.effects.get(effect.effect_id).phase == "sealed"
        assert command.effects.list_run_drive_watches(
            after_admission_seq=0, high_water=high_water, limit=1
        ) == watches_before
        assert _snapshot_existing(command, run_id) == terminal

        first = _drive_to_predelete_cut(command, watch, effect.effect_id)
        effect_after_cut = command.effects.get(effect.effect_id).phase
        watches_after_cut = command.effects.list_run_drive_watches(
            after_admission_seq=0, high_water=high_water, limit=1
        )

    with prepared_native_reopen(
        state_dir, recipes_dir, runtime_context
    ) as reopened:
        retained = reopened.effects.list_run_drive_watches(
            after_admission_seq=0, high_water=high_water, limit=1
        )
        assert len(retained) == 1
        retry = _retry_terminal_watch(reopened, retained[0], effect.effect_id)
        final_effect = reopened.effects.get(effect.effect_id).phase
        final_watches = reopened.effects.list_run_drive_watches(
            after_admission_seq=0, high_water=high_water, limit=1
        )

    with RuntimeReadResources(state_dir).native_app(binding) as app:
        persisted_native = app.snapshot(
            thread_id=binding.thread_id, subgraphs=True
        )

    assert {
        "predelete_failure": first["failure"],
        "busy_drive_reconcile": busy_drive["reconcile"],
        "busy_drive_ack": busy_drive["ack"],
        "busy_state_unchanged": (
            busy_effect == "sealed"
            and busy_watches == watches_before
            and busy_native == terminal
        ),
        "predelete_reconcile": first["reconcile"],
        "predelete_ack": first["ack"],
        "effect_after_cut": effect_after_cut,
        "watch_retained_at_cut": watches_after_cut == watches_before,
        "retry_reconcile": retry["reconcile"],
        "retry_ack": retry["ack"],
        "retry_outcome": retry["outcome"],
        "final_effect": final_effect,
        "final_watch_absent": final_watches == (),
        "native_unchanged": persisted_native == terminal,
    } == {
        "predelete_failure": "pre-delete",
        "busy_drive_reconcile": [
            ((effect.effect_id, "busy", "sealed"),)
        ],
        "busy_drive_ack": [],
        "busy_state_unchanged": True,
        "predelete_reconcile": [
            ((effect.effect_id, "delivered", "delivered"),)
        ],
        "predelete_ack": [(run_id, "delivered", watches_before)],
        "effect_after_cut": "delivered",
        "watch_retained_at_cut": True,
        "retry_reconcile": [()],
        "retry_ack": [(run_id, "delivered")],
        "retry_outcome": False,
        "final_effect": "delivered",
        "final_watch_absent": True,
        "native_unchanged": True,
    }
