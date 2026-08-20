from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

from lockstep.runtime.effects.descriptors import parse_effect_result
from lockstep.runtime.providers.base import RunnerObservation, TerminalSafetyObservation

from .test_coordinator import NOW, _advance_to_running, _result


def test_launching_recovery_adopts_same_attempt_and_never_spawns_twice(system) -> None:
    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    coordinator.reconcile("run-1")
    coordinator.reconcile("run-1")
    assert ledger.list_nonterminal()[0].phase == "launching"

    assert coordinator.reconcile("run-1").action == "running"
    assert coordinator.reconcile("run-1").action == "running"

    assert runner.spawn_count == 1
    assert len(runner.ensure_started_calls) == 1


def test_launching_ambiguity_is_sealed_indeterminate_and_never_retried(system) -> None:
    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    coordinator.reconcile("run-1")
    coordinator.reconcile("run-1")
    launch = runner.prepare_calls[-1]
    runner.start_observations.append(
        RunnerObservation(
            effect_id=ledger.list_nonterminal()[0].effect_id,
            request_digest=launch.request_digest,
            runner_binding_digest=launch.runner_binding_digest,
            state="indeterminate",
        )
    )

    report = coordinator.reconcile("run-1")
    assert report.action == "indeterminate"
    assert ledger.get(report.effect_id).fixed_error_code == "launch_indeterminate"
    assert coordinator.reconcile("run-1").action == "awaiting_delivery"
    assert runner.spawn_count == 1


def test_deadline_cancel_requires_matching_terminal_safety_proof(system) -> None:
    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    running, runner = _advance_to_running(system)
    launch = runner.ensure_started_calls[0]
    coordinator._clock = lambda: NOW + timedelta(hours=1)
    runner.safety_observations.extend(
        [
            TerminalSafetyObservation.pending_for(launch),
            TerminalSafetyObservation.proven_for(launch, result_stable=True),
        ]
    )

    assert coordinator.reconcile("run-1").action == "quiescence_pending"
    assert ledger.get(running.effect_id).phase == "running"
    assert coordinator.reconcile("run-1").action == "sealed"
    sealed = ledger.get(running.effect_id)
    assert sealed.fixed_error_code == "deadline_timeout"
    assert runner.cancel_calls == [running.effect_id, running.effect_id]
    assert runner.quiesce_calls == [running.effect_id, running.effect_id]


def test_concurrent_reconcilers_share_one_durable_launch_claim(system) -> None:
    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    coordinator.reconcile("run-1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: coordinator.reconcile("run-1"), range(2)))

    assert sum(report.action == "launch_claimed" for report in reports) == 1
    assert ledger.list_nonterminal()[0].phase in {"launching", "running"}
    while ledger.list_nonterminal()[0].phase == "launching":
        coordinator.reconcile("run-1")
    assert runner.spawn_count == 1


def test_partial_and_batch_delivery_use_only_current_exact_interrupts(system) -> None:
    from lockstep.runtime.effects.coordinator import EffectCoordinator
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor
    from lockstep.runtime.leases import LeaseStore
    from lockstep.runtime.native_models import NativeCoordinate, NativeInterrupt

    coordinator, runtime, _runner, ledger, store, first_coordinate = system
    first_descriptor = parse_effect_descriptor(runtime.current.pending[0].value["lockstep_effect"])
    first_id = derive_id = coordinator.reconcile("run-1").effect_id
    lease_store = LeaseStore(store, clock=lambda: NOW)
    first_lease = lease_store.acquire("effect", first_id, "seal-first", 30)
    first = ledger.get(first_id)
    first_result = _result(first_id, snapshot_ref="snapshot:" + "1" * 64)
    first = ledger.seal(
        first_id,
        first_result,
        expected_revision=first.revision,
        lease=first_lease,
        scope_descriptor=None,
    )
    lease_store.release(first_lease)

    second_coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-2", "int-2")
    second_descriptor = replace(first_descriptor, logical_id="review")
    # Reparse to bind the changed canonical descriptor rather than forge an internal object.
    second_value = dict(runtime.current.pending[0].value["lockstep_effect"])
    second_value["logical_id"] = "review"
    second_descriptor = parse_effect_descriptor(second_value)
    second_id = __import__(
        "lockstep.runtime.effects.descriptors", fromlist=["derive_effect_id"]
    ).derive_effect_id(second_coordinate, second_descriptor.digest)
    second_lease = lease_store.acquire("effect", second_id, "seal-second", 30)
    second = ledger.prepare(
        second_coordinate,
        second_descriptor,
        deadline_at=NOW + timedelta(seconds=300),
        runner_binding_digest="b" * 64,
        workspace_ref=f"workspace:{second_id}",
        lease=second_lease,
    )
    second_result = _result(second_id, snapshot_ref="snapshot:" + "2" * 64)
    second = ledger.seal(
        second_id,
        second_result,
        expected_revision=second.revision,
        lease=second_lease,
    )
    lease_store.release(second_lease)
    runtime.current = replace(
        runtime.current,
        pending=(
            runtime.current.pending[0],
            NativeInterrupt(second_coordinate, {"lockstep_effect": second_value}),
        ),
    )
    runtime.history_coordinates.add(second_coordinate)

    coordinator.deliver_ready("run-1", interrupt_ids=[first_coordinate.interrupt_id])
    assert ledger.get(first_id).phase == "delivered"
    assert ledger.get(second_id).phase == "sealed"
    assert runtime.current.pending[0].coordinate == second_coordinate

    coordinator.deliver_ready("run-1")
    assert ledger.get(second_id).phase == "delivered"
    assert runtime.current.pending == ()


def test_overdue_scan_is_bounded_and_nearest_wakeup_is_deterministic(system) -> None:
    coordinator, _runtime, _runner, _ledger, _store, _coordinate = system
    coordinator.reconcile("run-1")

    assert coordinator.next_wakeup_delay(NOW) == 1.0
    reports = coordinator.reconcile_due(NOW + timedelta(hours=1))
    assert len(reports) <= coordinator.MAX_DUE_PER_SCAN
    assert reports[0].run_id == "run-1"
