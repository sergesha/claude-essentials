"""Real legacy population for Task 12 B1 paged-backfill tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lockstep.runtime import sessions
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.providers.manual import ManualSubmission
from lockstep.workflow.compiler import compile_workflow
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import ResolvedCatalog, validate_semantics
from tests.runtime._run_drive_b1_harness import (
    active_native_command,
    prepared_native_reopen,
)


@dataclass(frozen=True)
class BackfillPopulation:
    state_dir: Path
    recipes_dir: Path
    project: Path
    malformed_ids: tuple[str, ...]
    terminal_id: str
    target_id: str
    target_thread_id: str
    runtime_context: object | None


def complete_backfill_with_driver(population: BackfillPopulation) -> None:
    for _page in range(2):
        with prepared_native_reopen(
            population.state_dir,
            population.recipes_dir,
            population.runtime_context,
        ) as command:
            command._recovery_driver._sweep_run_drive_watches(
                project_identity=str(population.project.resolve()),
                limit=128,
            )


def _install_decision_recipe(tmp_path: Path):
    source = tmp_path / "zzz-decision.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\n"
        "name: zzz-decision\n"
        "description: B1 paged backfill target\n"
        "protect: ['**']\n"
        "flow:\n"
        "  - step: edit\n"
        "    task: Edit the project\n"
        "    exit: Editing is complete\n"
        "  - decide:\n"
        "      id: risk\n"
        "      using:\n"
        "        type: changed-paths\n"
        "        since: start\n"
        "        cases: {high: [auth/**]}\n"
        "        default: low\n"
        "  - choose:\n"
        "      value: risk\n"
        "      cases:\n"
        "        high: [{escalate: {}}]\n"
        "        low: [{escalate: {}}]\n"
    )
    workflow = parse_workflow(load_workflow(source))
    catalog = ResolvedCatalog()
    compiled = compile_workflow(
        validate_semantics(workflow, catalog), catalog
    )
    recipes = tmp_path / "recipes"
    for relative_path, content in compiled.executable_files.items():
        target = recipes / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return compiled


def _snapshot_existing(command, run_id: str):
    command.runtime.bind(command.catalog.get(run_id))
    return command.runtime.snapshot(run_id, subgraphs=True)


def _complete_terminal(command, project: Path) -> RunBinding:
    started = command.start("native-parent-direct", {}, str(project))
    run_id = started["run_id"]
    session_id = "item13-terminal"
    assert sessions.touch(command.state_dir, run_id, session_id, 30) == "bound"
    command.scenario_done(
        run_id,
        "answer",
        {},
        session_id=session_id,
        project=str(project),
    )
    snapshot = _snapshot_existing(command, run_id)
    assert snapshot.checkpoint_id and snapshot.pending == snapshot.next == ()
    return command.catalog.get(run_id)


def _create_malformed_prefix(
    command, terminal: RunBinding, project: Path
) -> tuple[str, ...]:
    malformed_ids = tuple(f"aaa-malformed-{index:03d}" for index in range(128))
    for index, public_run_id in enumerate(malformed_ids):
        binding = command.catalog.create(
            RunBinding(
                public_run_id,
                f"aaa-malformed-thread-{index:03d}",
                terminal.recipe_digest,
                "f" * 64 if index == 0 else terminal.recipe_snapshot_ref,
                str(project.resolve()),
            )
        )
        if index == 0:
            continue
        command.runtime.bind(binding)
        snapshot = command.runtime.snapshot(public_run_id, subgraphs=True)
        assert snapshot.checkpoint_id == ""
        assert snapshot.values == {} and snapshot.pending == snapshot.next == ()
        command.runtime.unbind(public_run_id)
    return malformed_ids


def _park_decision(command, compiled, project: Path) -> RunBinding:
    started = command.start(
        "zzz-decision",
        {},
        str(project),
        compiler_provenance=compiled.compiler_provenance,
    )
    run_id = started["run_id"]
    manual = _snapshot_existing(command, run_id)
    assert len(manual.pending) == 1
    descriptor = parse_effect_descriptor(
        manual.pending[0].value["lockstep_effect"]
    )
    assert descriptor.kind == "manual"
    command.coordinator.submit_manual(
        run_id,
        manual.pending[0].coordinate,
        ManualSubmission.build("PASS", evidence={}),
    )
    decision = _snapshot_existing(command, run_id)
    assert len(decision.pending) == 1
    decision_descriptor = parse_effect_descriptor(
        decision.pending[0].value["lockstep_effect"]
    )
    assert decision_descriptor.__class__.__name__ == "DecisionDescriptor"
    return command.catalog.get(run_id)


def seed_backfill_population(tmp_path: Path) -> BackfillPopulation:
    with active_native_command(tmp_path) as (command, project):
        compiled = _install_decision_recipe(tmp_path)
        terminal = _complete_terminal(command, project)
        malformed_ids = _create_malformed_prefix(command, terminal, project)
        target = _park_decision(command, compiled, project)
        assert command._runtime_execution_context is None
        assert malformed_ids[-1] < terminal.public_run_id < target.public_run_id
        # The final v2 writer always admits watches.  Remove the two created
        # through public starts to model the historical pre-watch population
        # that this backfill integration fixture is specifically exercising.
        high_water = command.effects.max_run_drive_admission_seq()
        assert high_water is not None
        active_origin_ids = tuple(
            watch.public_run_id
            for watch in command.effects.list_run_drive_watches(
                after_admission_seq=0,
                high_water=high_water,
                limit=128,
            )
        )
        assert set(active_origin_ids) == {
            terminal.public_run_id,
            target.public_run_id,
        }
        command.effects.acknowledge_run_drive_watch(terminal.public_run_id)
        command.effects.acknowledge_run_drive_watch(target.public_run_id)
        assert command.effects.max_run_drive_admission_seq() is None
        return BackfillPopulation(
            command.state_dir,
            command.recipes_dir,
            project,
            malformed_ids,
            terminal.public_run_id,
            target.public_run_id,
            target.thread_id,
            command._runtime_execution_context,
        )
