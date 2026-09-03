"""Task 11 RED contracts for concurrent native-effect reconciliation."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_descriptor,
)
from lockstep.runtime.native_models import NativeCoordinate, NativeInterrupt
from lockstep.runtime.providers.base import EffectRequest, TerminalSafetyObservation
from lockstep.runtime.status import project_status

from tests.runtime.effects.test_coordinator import NOW, _result, managed_descriptor


def _install_second_effect(system):
    coordinator, runtime, runner, _ledger, _store, first_coordinate = system
    second_coordinate = NativeCoordinate(
        first_coordinate.thread_id,
        first_coordinate.checkpoint_id,
        first_coordinate.checkpoint_ns,
        "task-2",
        "int-2",
    )
    second_raw = managed_descriptor(logical_id="implement-two")
    second_descriptor = parse_effect_descriptor(second_raw)
    runtime.current = replace(
        runtime.current,
        pending=(
            runtime.current.pending[0],
            NativeInterrupt(second_coordinate, {"lockstep_effect": second_raw}),
        ),
    )
    runtime.history_coordinates.add(second_coordinate)
    runtime.history_values[second_coordinate] = {"lockstep_effect": second_raw}
    coordinator._authority.authorize(
        EffectRequest.build(
            effect_id=derive_effect_id(second_coordinate, second_descriptor.digest),
            public_run_id="run-1",
            project_identity="project-1",
            definition_digest="a" * 64,
            coordinate=second_coordinate,
            descriptor_digest=second_descriptor.digest,
            effect_kind=second_descriptor.kind,
            runner_selector=second_descriptor.runner.selector,
            runner_binding_digest=runner.binding_digest,
            required_capabilities=second_descriptor.runner.required_capabilities,
            inputs=(("brief", runtime.current.values["brief"]),),
            writes=second_descriptor.writes,
            deadline_at=NOW + timedelta(seconds=second_descriptor.deadline_seconds),
        )
    )
    return first_coordinate, second_coordinate


def test_one_bounded_reconciliation_pass_advances_each_native_branch_once(
    system,
) -> None:
    """A first-running branch must not starve a sibling before it can launch."""
    coordinator, _runtime, runner, ledger, _store, _first = system
    first_coordinate, second_coordinate = _install_second_effect(system)

    assert [report.action for report in coordinator.reconcile_pending("run-1")] == [
        "prepared",
        "prepared",
    ]
    assert [report.action for report in coordinator.reconcile_pending("run-1")] == [
        "launch_claimed",
        "launch_claimed",
    ]
    assert [report.action for report in coordinator.reconcile_pending("run-1")] == [
        "running",
        "running",
    ]

    records = {
        record.coordinate: record for record in ledger.list_nonterminal()
    }
    assert records[first_coordinate].phase == "running"
    assert records[second_coordinate].phase == "running"
    assert {launch.effect_id for launch in runner.ensure_started_calls} == {
        records[first_coordinate].effect_id,
        records[second_coordinate].effect_id,
    }
    assert runner.spawn_count == 2


def test_two_sealed_branch_results_resume_as_one_exact_native_batch(system) -> None:
    """The coordinator may batch facts, but only LangGraph may close the join."""
    coordinator, runtime, runner, ledger, _store, _first = system
    first_coordinate, second_coordinate = _install_second_effect(system)
    for _ in range(3):
        coordinator.reconcile_pending("run-1")
    records = {
        record.coordinate: record for record in ledger.list_nonterminal()
    }
    launches = {item.effect_id: item for item in runner.ensure_started_calls}
    for coordinate in (first_coordinate, second_coordinate):
        record = records[coordinate]
        result = _result(
            record.effect_id,
            snapshot_ref="snapshot:" + ("1" if coordinate == first_coordinate else "2") * 64,
        )
        launch = launches[record.effect_id]
        runner.inspect_observations.append(runner.terminal(launch, result))
        runner.safety_observations.append(
            TerminalSafetyObservation.proven_for(
                launch,
                rollover_snapshot_ref=result.snapshot_ref,
                result_stable=True,
            )
        )

    assert [report.action for report in coordinator.reconcile_pending("run-1")] == [
        "sealed",
        "sealed",
    ]
    status = coordinator.deliver_ready("run-1")

    assert len(runtime.resume_calls) == 1
    _run_id, source, results = runtime.resume_calls[0]
    assert source == first_coordinate
    assert set(results) == {"int-1", "int-2"}
    assert ledger.get(records[first_coordinate].effect_id).phase == "delivered"
    assert ledger.get(records[second_coordinate].effect_id).phase == "delivered"
    assert status.status == "completed"


def test_partial_delivery_leaves_only_the_unsatisfied_native_interrupt(system) -> None:
    """Flat stale interrupt metadata must not resurrect a completed branch."""
    coordinator, runtime, _runner, ledger, store, _first = system
    first_coordinate, second_coordinate = _install_second_effect(system)
    from lockstep.runtime.leases import LeaseStore

    for _ in range(2):
        coordinator.reconcile_pending("run-1")
    records = {record.coordinate: record for record in ledger.list_nonterminal()}
    leases = LeaseStore(store, clock=lambda: NOW)
    first = records[first_coordinate]
    lease = leases.acquire("effect", first.effect_id, "seal-first", 30)
    try:
        first = ledger.mark_running(
            first.effect_id,
            expected_revision=first.revision,
            lease=lease,
            runner_binding_digest=first.runner_binding_digest,
        )
        first = ledger.seal(
            first.effect_id,
            _result(first.effect_id, snapshot_ref="snapshot:" + "1" * 64),
            expected_revision=first.revision,
            lease=lease,
            runner_binding_digest=first.runner_binding_digest,
        )
    finally:
        leases.release(lease)

    coordinator.deliver_ready("run-1", [first_coordinate.interrupt_id])

    assert [item.coordinate for item in runtime.current.pending] == [second_coordinate]
    assert ledger.get(first.effect_id).phase == "delivered"
    assert ledger.get(records[second_coordinate].effect_id).phase == "launching"




def test_status_aggregates_all_pending_effects_without_mutating_them(system) -> None:
    """First-interrupt projection hides sibling progress and can misstate ownership."""
    coordinator, runtime, runner, ledger, _store, _coordinate = system
    _install_second_effect(system)
    coordinator.reconcile_pending("run-1")
    before = tuple(ledger.list_nonterminal())

    status = project_status(
        runtime.binding("run-1"), runtime.current, object(), ledger
    ).to_dict()

    assert status["status"] == "running"
    assert status["owner"] == "engine"
    assert status["next_action"] == "scenario_wait"
    assert status["parallel_progress"] == {
        "pending": 2,
        "phases": {"prepared": 2},
        "operations": ["implement", "implement-two"],
        "deadlines": [
            (NOW + timedelta(seconds=300)).isoformat(),
            (NOW + timedelta(seconds=300)).isoformat(),
        ],
    }
    assert tuple(ledger.list_nonterminal()) == before
    assert runner.prepare_calls == []
    assert runtime.resume_calls == []


def test_stale_pending_sweep_accepts_only_exact_descended_batch_facts(
    system, monkeypatch
) -> None:
    """A concurrent native batch commit must not poison the completion pump."""
    coordinator, _runtime, runner, ledger, _store, _first = system
    first_coordinate, second_coordinate = _install_second_effect(system)
    for _ in range(3):
        coordinator.reconcile_pending("run-1")
    records = {record.coordinate: record for record in ledger.list_nonterminal()}
    launches = {item.effect_id: item for item in runner.ensure_started_calls}
    for coordinate in (first_coordinate, second_coordinate):
        record = records[coordinate]
        result = _result(
            record.effect_id,
            snapshot_ref="snapshot:" + ("3" if coordinate == first_coordinate else "4") * 64,
        )
        launch = launches[record.effect_id]
        runner.inspect_observations.append(runner.terminal(launch, result))
        runner.safety_observations.append(
            TerminalSafetyObservation.proven_for(
                launch,
                rollover_snapshot_ref=result.snapshot_ref,
                result_stable=True,
            )
        )
    coordinator.reconcile_pending("run-1")

    original = coordinator.reconcile_one
    raced = False

    def deliver_before_first_exact_reconcile(run_id, coordinate, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            coordinator.deliver_ready(run_id)
        return original(run_id, coordinate, **kwargs)

    monkeypatch.setattr(
        coordinator, "reconcile_one", deliver_before_first_exact_reconcile
    )

    reports = coordinator.reconcile_pending("run-1")

    assert [report.action for report in reports] == ["delivered", "delivered"]
    assert all(
        ledger.get(records[coordinate].effect_id).phase == "delivered"
        for coordinate in (first_coordinate, second_coordinate)
    )


def test_batch_commit_crash_recovers_each_descended_sealed_sibling(system) -> None:
    """Ledger ordering may not decide which exact post-commit fact can recover."""
    coordinator, runtime, runner, ledger, _store, _first = system
    first_coordinate, second_coordinate = _install_second_effect(system)
    for _ in range(3):
        coordinator.reconcile_pending("run-1")
    records = {record.coordinate: record for record in ledger.list_nonterminal()}
    launches = {item.effect_id: item for item in runner.ensure_started_calls}
    for coordinate in (first_coordinate, second_coordinate):
        record = records[coordinate]
        result = _result(
            record.effect_id,
            snapshot_ref="snapshot:" + ("5" if coordinate == first_coordinate else "6") * 64,
        )
        launch = launches[record.effect_id]
        runner.inspect_observations.append(runner.terminal(launch, result))
        runner.safety_observations.append(
            TerminalSafetyObservation.proven_for(
                launch,
                rollover_snapshot_ref=result.snapshot_ref,
                result_stable=True,
            )
        )
    coordinator.reconcile_pending("run-1")
    runtime.current = replace(runtime.current, pending=(), checkpoint_id="after-batch")

    reports = [
        coordinator.reconcile_one(
            "run-1",
            coordinate,
            expected_descriptor_digest=records[coordinate].descriptor_digest,
        )
        for coordinate in (second_coordinate, first_coordinate)
    ]

    assert [report.action for report in reports] == ["delivered", "delivered"]
    assert runtime.resume_calls == []
