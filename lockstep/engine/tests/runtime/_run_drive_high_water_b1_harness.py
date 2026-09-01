"""Real native population for Task 12 B1 sweep high-water tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lockstep.recipe.yamlgraph_adapter import open_native_app
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.providers.manual import ManualSubmission
from lockstep.runtime.service import preflight_recipe
from lockstep.runtime.snapshot_resolver import capture_authoritative_snapshot
from lockstep.runtime.storage import (
    LegacyRunDriveClassification,
    RuntimeSchemaMigrator,
)
from tests.runtime._run_drive_b1_harness import (
    _NATIVE_FIXTURES,
    prepared_native_reopen,
)
from tests.runtime._run_drive_backfill_b1_harness import _install_decision_recipe


@dataclass(frozen=True)
class HighWaterPopulation:
    state_dir: Path
    recipes_dir: Path
    project: Path
    park_ids: tuple[str, ...]
    target_id: str
    seed_watch_ids: tuple[str, ...]
    seed_high_water: int
    park_template: RunBinding
    runtime_context: object | None


def _install_sequential_recipe(recipes_dir: Path) -> None:
    recipes_dir.mkdir(parents=True, exist_ok=True)
    source = (_NATIVE_FIXTURES / "sequential_interrupts.recipe.yaml").read_bytes()
    profiled = source.replace(
        b"message: First?", b'message: {step: escalate, text: "First?"}'
    ).replace(
        b"message: Second?", b'message: {step: escalate, text: "Second?"}'
    )
    assert profiled != source
    (recipes_dir / "native-sequential-interrupts.recipe.yaml").write_bytes(profiled)


def _bind_run_start(command, binding: RunBinding) -> RunBinding:
    ref = capture_authoritative_snapshot(
        Path(binding.project_identity),
        command.snapshots,
        command.blobs,
        binding,
        previous=None,
        purpose="run-start",
    )
    with command.store.write_transaction() as connection:
        admitted = command.catalog.create_in_transaction(connection, binding)
        command.runtime_snapshot_facts.bind_run_start_in_transaction(
            connection, admitted, ref
        )
    return admitted


def _seed_worker_parks(command, project: Path) -> tuple[RunBinding, ...]:
    authorized = preflight_recipe(
        command.recipes_dir, "native-sequential-interrupts"
    )
    admitted = authorized.capture(command.bundle_store)
    materialized = admitted.materialize(command.bundle_store)
    app = open_native_app(materialized, command.checkpoint_path)
    bindings = []
    try:
        for index in range(129):
            binding = _bind_run_start(
                command,
                RunBinding(
                    f"aaa-park-{index:03d}",
                    f"aaa-park-thread-{index:03d}",
                    admitted.definition_sha256,
                    admitted.bundle.digest,
                    str(project.resolve()),
                ),
            )
            snapshot = app.invoke({}, thread_id=binding.thread_id)
            assert snapshot.checkpoint_id and len(snapshot.pending) == 1
            assert "lockstep_effect" not in snapshot.pending[0].value
            bindings.append(binding)
    finally:
        app.close()
    return tuple(bindings)


def _seed_decision(command, compiled, project: Path) -> RunBinding:
    authorized = preflight_recipe(
        command.recipes_dir,
        "zzz-decision",
        compiler_provenance=compiled.compiler_provenance,
    )
    admitted = authorized.capture(command.bundle_store)
    admitted.materialize(command.bundle_store)
    binding = _bind_run_start(
        command,
        RunBinding(
            "zzz-decision-target",
            "zzz-decision-target-thread",
            admitted.definition_sha256,
            admitted.bundle.digest,
            str(project.resolve()),
        ),
    )
    command.runtime.bind(binding)
    manual = command.runtime.ensure_started(binding.public_run_id, {})
    status = command._drive_engine_owned(
        binding.public_run_id, binding=binding, snapshot=manual
    )
    assert status.status == "awaiting" and status.owner == "worker"
    descriptor = parse_effect_descriptor(manual.pending[0].value["lockstep_effect"])
    assert descriptor.kind == "manual"
    command.coordinator.submit_manual(
        binding.public_run_id,
        manual.pending[0].coordinate,
        ManualSubmission.build("PASS", evidence={}),
    )
    decision = command.runtime.snapshot(binding.public_run_id, subgraphs=True)
    assert len(decision.pending) == 1
    assert (
        parse_effect_descriptor(decision.pending[0].value["lockstep_effect"])
        .__class__.__name__
        == "DecisionDescriptor"
    )
    return binding


def _seed_watch_population(command, bindings: tuple[RunBinding, ...]) -> int:
    assert command.effects.max_run_drive_admission_seq() is None
    migrator = RuntimeSchemaMigrator(command.store)
    records = tuple(
        LegacyRunDriveClassification(binding.public_run_id, "nonterminal")
        for binding in sorted(bindings, key=lambda item: item.public_run_id)
    )
    first = migrator.apply_run_drive_watch_page(
        expected_after_public_run_id=None,
        classified=records[:128],
        exhausted=False,
    )
    second = migrator.apply_run_drive_watch_page(
        expected_after_public_run_id=first.after_public_run_id,
        classified=records[128:],
        exhausted=True,
    )
    assert not first.completed and second.completed
    high_water = command.effects.max_run_drive_admission_seq()
    assert high_water is not None
    return high_water


def seed_high_water_population(tmp_path: Path) -> HighWaterPopulation:
    recipes_dir = tmp_path / "recipes"
    _install_sequential_recipe(recipes_dir)
    compiled = _install_decision_recipe(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    state_dir = tmp_path / "state"
    with prepared_native_reopen(state_dir, recipes_dir, None) as command:
        parks = _seed_worker_parks(command, project)
        target = _seed_decision(command, compiled, project)
        ordered = tuple((*parks, target))
        high_water = _seed_watch_population(command, ordered)
        watches = command.effects.list_run_drive_watches(
            after_admission_seq=0,
            high_water=high_water,
            limit=128,
        )
        assert tuple(item.public_run_id for item in watches) == tuple(
            item.public_run_id for item in parks[:128]
        )
        assert command._runtime_execution_context is None
        return HighWaterPopulation(
            state_dir=state_dir,
            recipes_dir=recipes_dir,
            project=project,
            park_ids=tuple(item.public_run_id for item in parks),
            target_id=target.public_run_id,
            seed_watch_ids=tuple(item.public_run_id for item in ordered),
            seed_high_water=high_water,
            park_template=parks[0],
            runtime_context=command._runtime_execution_context,
        )
