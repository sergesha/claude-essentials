"""Task 12 B1 RED for a fixed sweep population under concurrent admission."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.effects.ledger import EffectLedger, RunDriveWatch
from lockstep.runtime.read_resources import RuntimeReadResources
from lockstep.runtime.recovery_driver import RecoveryDriver
from lockstep.runtime.snapshot_resolver import (
    RuntimeSnapshotFacts,
    capture_authoritative_snapshot,
)
from lockstep.runtime.storage import SQLiteStore
from tests.runtime._run_drive_b1_harness import prepared_native_reopen
from tests.runtime._run_drive_high_water_b1_harness import (
    HighWaterPopulation,
    seed_high_water_population,
)


@dataclass
class _ConcurrentAdmissions:
    store: SQLiteStore
    catalog: RunCatalog
    ledger: EffectLedger
    facts: RuntimeSnapshotFacts
    input_blob: object
    bindings_and_refs: tuple[tuple[RunBinding, object], ...]
    admitted_watches: tuple[RunDriveWatch, ...] = ()

    def admit(self) -> None:
        before = self.ledger.max_run_drive_admission_seq()
        assert before is not None
        for binding, ref in self.bindings_and_refs:
            self.ledger.admit_start(
                self.catalog,
                binding,
                self.input_blob,
                on_admit=lambda connection, admitted, ref=ref: (
                    self.facts.bind_run_start_in_transaction(
                        connection, admitted, ref
                    )
                ),
            )
        high_water = self.ledger.max_run_drive_admission_seq()
        assert high_water is not None
        self.admitted_watches = self.ledger.list_run_drive_watches(
            after_admission_seq=before,
            high_water=high_water,
            limit=len(self.bindings_and_refs),
        )
        assert tuple(watch.public_run_id for watch in self.admitted_watches) == tuple(
            binding.public_run_id for binding, _ref in self.bindings_and_refs
        )


def _prepare_concurrent_admissions(population, command) -> _ConcurrentAdmissions:
    template = population.park_template
    bindings = tuple(
        RunBinding(
            f"new-admission-{index:03d}",
            f"new-admission-thread-{index:03d}",
            template.recipe_digest,
            template.recipe_snapshot_ref,
            template.project_identity,
        )
        for index in range(2)
    )
    refs = tuple(
        capture_authoritative_snapshot(
            population.project,
            command.snapshots,
            command.blobs,
            binding,
            previous=None,
            purpose="run-start",
        )
        for binding in bindings
    )
    second_store = SQLiteStore(population.state_dir / "runtime.sqlite")
    assert second_store.engine is not command.store.engine
    return _ConcurrentAdmissions(
        store=second_store,
        catalog=RunCatalog(second_store),
        ledger=EffectLedger(second_store),
        facts=RuntimeSnapshotFacts(second_store),
        input_blob=command.blobs.put(b"{}"),
        bindings_and_refs=tuple(zip(bindings, refs, strict=True)),
    )


def _install_sweep_trace(monkeypatch, command, admissions, trace, phase) -> None:
    real_max = command.effects.max_run_drive_admission_seq
    real_list = command.effects.list_run_drive_watches
    real_drive = RecoveryDriver._drive_run_watch

    def capture_max():
        captured = real_max()
        trace["max"].append((phase["value"], captured))
        if phase["value"] == 1 and not admissions.admitted_watches:
            admissions.admit()
        return captured

    def capture_list(*, after_admission_seq, high_water, limit):
        watches = real_list(
            after_admission_seq=after_admission_seq,
            high_water=high_water,
            limit=limit,
        )
        trace["list"].append(
            (phase["value"], after_admission_seq, high_water, limit, watches)
        )
        return watches

    def capture_drive(self, watch):
        outcome = real_drive(self, watch)
        trace["drive"].append((phase["value"], watch, outcome))
        return outcome

    monkeypatch.setattr(
        command.effects, "max_run_drive_admission_seq", capture_max
    )
    monkeypatch.setattr(command.effects, "list_run_drive_watches", capture_list)
    monkeypatch.setattr(RecoveryDriver, "_drive_run_watch", capture_drive)


def _run_two_sweeps(population, monkeypatch):
    trace = {"max": [], "list": [], "drive": [], "result": []}
    phase = {"value": 1}
    admissions = None
    with prepared_native_reopen(
        population.state_dir,
        population.recipes_dir,
        population.runtime_context,
    ) as command:
        admissions = _prepare_concurrent_admissions(population, command)
        try:
            with monkeypatch.context() as patcher:
                _install_sweep_trace(
                    patcher, command, admissions, trace, phase
                )
                for current_phase in (1, 2):
                    phase["value"] = current_phase
                    recovered = command._recovery_driver._sweep_run_drive_watches(
                        project_identity=str(population.project.resolve()),
                        limit=128,
                    )
                    trace["result"].append((current_phase, recovered))
        finally:
            admissions.store.close()
    return trace, admissions


def _native_shape(population, public_run_id: str):
    resources = RuntimeReadResources(population.state_dir)
    binding = resources.binding_for(
        public_run_id, str(population.project.resolve())
    )
    if binding is None:
        return None
    with resources.native_app(binding) as app:
        snapshot = app.snapshot(thread_id=binding.thread_id, subgraphs=True)
    return (
        bool(snapshot.checkpoint_id),
        bool(snapshot.pending),
        not snapshot.pending and not snapshot.next,
    )


def _observed_contract(population, trace, admissions):
    additions = tuple(
        binding.public_run_id for binding, _ref in admissions.bindings_and_refs
    )
    first_lists = tuple(item for item in trace["list"] if item[0] == 1)
    second_lists = tuple(item for item in trace["list"] if item[0] == 2)
    first_returned = tuple(
        watch for item in first_lists for watch in item[4]
    )
    second_returned = tuple(
        watch for item in second_lists for watch in item[4]
    )
    first_driven = tuple(item[1] for item in trace["drive"] if item[0] == 1)
    second_driven = tuple(item[1] for item in trace["drive"] if item[0] == 2)
    admitted_sequences = tuple(
        watch.admission_seq for watch in admissions.admitted_watches
    )
    return {
        "max_calls": tuple(trace["max"]),
        "admitted_sequences": admitted_sequences,
        "first_high_water_fixed": bool(first_lists)
        and all(item[2] == population.seed_high_water for item in first_lists),
        "first_population_bounded": {
            watch.public_run_id for watch in first_returned
        }
        == set(population.seed_watch_ids)
        and all(watch.admission_seq <= population.seed_high_water for watch in first_returned),
        "first_excludes_additions": not set(additions)
        & {watch.public_run_id for watch in first_returned},
        "first_drives_past_129_parks": set(population.park_ids).issubset(
            {watch.public_run_id for watch in first_driven}
        )
        and population.target_id
        in {watch.public_run_id for watch in first_driven},
        "first_result_reaches_target": bool(trace["result"])
        and population.target_id in trace["result"][0][1],
        "target_terminal": _native_shape(population, population.target_id)
        == (True, False, True),
        "second_high_water_fixed": bool(second_lists)
        and all(
            item[2] == population.seed_high_water + 2 for item in second_lists
        ),
        "second_lists_additions": set(additions).issubset(
            {watch.public_run_id for watch in second_returned}
        ),
        "second_drives_additions": set(additions).issubset(
            {watch.public_run_id for watch in second_driven}
        ),
        "additions_are_real_parks": tuple(
            _native_shape(population, public_run_id) for public_run_id in additions
        )
        == ((True, True, False), (True, True, False)),
    }


def test_sweep_high_water_excludes_concurrent_admissions(
    tmp_path: Path, monkeypatch
) -> None:
    population: HighWaterPopulation = seed_high_water_population(tmp_path)
    trace, admissions = _run_two_sweeps(population, monkeypatch)

    assert _observed_contract(population, trace, admissions) == {
        "max_calls": ((1, 130), (2, 132)),
        "admitted_sequences": (131, 132),
        "first_high_water_fixed": True,
        "first_population_bounded": True,
        "first_excludes_additions": True,
        "first_drives_past_129_parks": True,
        "first_result_reaches_target": True,
        "target_terminal": True,
        "second_high_water_fixed": True,
        "second_lists_additions": True,
        "second_drives_additions": True,
        "additions_are_real_parks": True,
    }
