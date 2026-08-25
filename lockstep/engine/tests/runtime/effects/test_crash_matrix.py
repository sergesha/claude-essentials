from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from lockstep.runtime.providers.base import RunnerObservation, TerminalSafetyObservation

from .test_coordinator import NOW, _advance_to_running, _result, system


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


def test_deadline_after_launch_claim_proves_absence_without_spawning(system) -> None:
    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    coordinator.reconcile("run-1")
    claimed = coordinator.reconcile("run-1")
    request = runner.prepare_calls[-1]
    coordinator._clock = lambda: NOW + timedelta(hours=1)
    runner.inspect_observations.append(
        RunnerObservation(
            effect_id=claimed.effect_id,
            request_digest=request.request_digest,
            runner_binding_digest=request.runner_binding_digest,
            state="absent",
        )
    )

    report = coordinator.reconcile("run-1")

    assert report.action == "sealed"
    assert ledger.get(claimed.effect_id).fixed_error_code == "deadline_timeout"
    assert runner.ensure_started_calls == []
    assert runner.spawn_count == 0


def test_deadline_cancel_requires_matching_terminal_safety_proof(system) -> None:
    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    running, runner = _advance_to_running(system)
    launch = runner.ensure_started_calls[0]
    coordinator._clock = lambda: NOW + timedelta(hours=1)
    runner.safety_observations.extend(
        [
            TerminalSafetyObservation.pending_for(launch),
            TerminalSafetyObservation.proven_for(
                launch,
                result_stable=True,
                rollover_snapshot_ref="snapshot:" + "f" * 64,
            ),
        ]
    )

    assert coordinator.reconcile("run-1").action == "quiescence_pending"
    assert ledger.get(running.effect_id).phase == "running"
    assert coordinator.reconcile("run-1").action == "sealed"
    sealed = ledger.get(running.effect_id)
    assert sealed.fixed_error_code == "deadline_timeout"
    assert runner.cancel_calls == [running.effect_id, running.effect_id]
    assert runner.quiesce_calls == [running.effect_id, running.effect_id]


def test_deadline_cancel_accepts_quarantined_rejected_output(system) -> None:
    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    running, runner = _advance_to_running(system)
    launch = runner.ensure_started_calls[0]
    coordinator._clock = lambda: NOW + timedelta(hours=1)
    runner.safety_observations.append(
        TerminalSafetyObservation.proven_for(
            launch,
            result_stable=True,
            workspace_quarantined=True,
        )
    )

    assert coordinator.reconcile("run-1").action == "sealed"
    assert ledger.get(running.effect_id).fixed_error_code == "deadline_timeout"


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


def test_busy_effect_lease_blocks_provider_prepare_before_durable_intent(
    system,
) -> None:
    from lockstep.runtime.effects.descriptors import (
        derive_effect_id,
        parse_effect_descriptor,
    )
    from lockstep.runtime.leases import LeaseStore

    coordinator, runtime, runner, ledger, store, coordinate = system
    descriptor = parse_effect_descriptor(
        runtime.current.pending[0].value["lockstep_effect"]
    )
    effect_id = derive_effect_id(coordinate, descriptor.digest)
    leases = LeaseStore(store, clock=lambda: NOW)
    held = leases.acquire("effect", effect_id, "other-coordinator", 30)
    try:
        report = coordinator.reconcile("run-1")
    finally:
        leases.release(held)

    assert report.action == "busy"
    assert ledger.list_nonterminal() == []
    assert runner.prepare_calls == []
    assert runner.ensure_started_calls == []


def test_lease_expiring_during_prepare_cannot_cross_ensure_started(system) -> None:
    from lockstep.runtime.leases import LeaseStore

    coordinator, _runtime, runner, ledger, store, _coordinate = system
    prepared = coordinator.reconcile("run-1")
    assert coordinator.reconcile("run-1").action == "launch_claimed"

    def advance_lease() -> None:
        later = LeaseStore(store, clock=lambda: NOW + timedelta(seconds=31))
        later.acquire("effect", prepared.effect_id, "new-owner", 30)

    runner.prepare_callbacks.append(advance_lease)
    report = coordinator.reconcile("run-1")

    assert report.action == "busy"
    assert ledger.get(prepared.effect_id).phase == "launching"
    assert len(runner.prepare_calls) == 2
    assert runner.ensure_started_calls == []
    assert runner.spawn_count == 0


def test_revocation_before_prepare_blocks_provider_contact(system) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied

    coordinator, _runtime, runner, _ledger, _store, _coordinate = system
    coordinator.reconcile("run-1")
    coordinator._authority.revoke(coordinator._authority.resolve_calls[-1])

    with pytest.raises(EffectAuthorityDenied, match="revoked"):
        coordinator.reconcile("run-1")

    assert runner.prepare_calls == []
    assert runner.ensure_started_calls == []


def test_revocation_during_prepare_is_serialized_before_ensure_started(system) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied

    coordinator, _runtime, runner, _ledger, _store, _coordinate = system
    coordinator.reconcile("run-1")
    coordinator.reconcile("run-1")
    request = runner.prepare_calls[-1]
    runner.prepare_callbacks.append(
        lambda: coordinator._authority.revoke(request.intent_digest)
    )

    with pytest.raises(EffectAuthorityDenied, match="revoked"):
        coordinator.reconcile("run-1")

    assert runner.ensure_started_calls == []
    assert runner.spawn_count == 0
    assert coordinator.reconcile("run-1").action == "authority_blocked"
    assert runner.inspect_calls
    assert runner.ensure_started_calls == []


def test_revocation_after_start_does_not_block_truthful_inspection(system) -> None:
    coordinator, _runtime, runner, _ledger, _store, _coordinate = system
    running, _runner = _advance_to_running(system)
    request = runner.prepare_calls[-1]
    coordinator._authority.revoke(request.intent_digest)

    report = coordinator.reconcile("run-1")

    assert report.action == "running"
    assert runner.inspect_calls == [running.effect_id]
    assert len(runner.ensure_started_calls) == 1


def test_input_mutation_after_durable_intent_rejects_before_provider_contact(
    system,
) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied

    coordinator, runtime, runner, ledger, _store, _coordinate = system
    prepared = coordinator.reconcile("run-1")
    runtime.current = replace(
        runtime.current,
        values={"brief": {"task": "mutated after authorization"}},
    )

    with pytest.raises(EffectAuthorityDenied, match="no exact"):
        coordinator.reconcile("run-1")

    assert ledger.get(prepared.effect_id).phase == "prepared"
    assert runner.prepare_calls == []
    assert runner.ensure_started_calls == []


def test_unknown_changed_intent_is_denied_without_ledger_or_provider_contact(
    system,
) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied

    coordinator, runtime, runner, ledger, _store, _coordinate = system
    runtime.current = replace(
        runtime.current,
        values={"brief": {"task": "not the explicitly granted input"}},
    )

    with pytest.raises(EffectAuthorityDenied, match="no exact"):
        coordinator.reconcile("run-1")

    assert ledger.list_nonterminal() == []
    assert runner.prepare_calls == []
    assert runner.ensure_started_calls == []


def test_native_source_change_during_prepare_cannot_cross_commitment_guard(
    system,
) -> None:
    from lockstep.runtime.graph_runtime import NativeCoordinateRejected

    coordinator, runtime, runner, ledger, _store, coordinate = system
    coordinator.reconcile("run-1")
    coordinator.reconcile("run-1")

    def replace_source() -> None:
        runtime.current = replace(
            runtime.current,
            pending=(
                replace(
                    runtime.current.pending[0],
                    coordinate=replace(coordinate, task_id="foreign-task"),
                ),
            ),
        )

    runner.prepare_callbacks.append(replace_source)
    with pytest.raises(NativeCoordinateRejected, match="exact current"):
        coordinator.reconcile("run-1")

    assert ledger.list_nonterminal()[0].phase == "launching"
    assert runner.ensure_started_calls == []
    assert runner.spawn_count == 0


def test_graph_input_change_during_prepare_cannot_cross_commitment_guard(
    system,
) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied

    coordinator, runtime, runner, ledger, _store, _coordinate = system
    coordinator.reconcile("run-1")
    coordinator.reconcile("run-1")
    runner.prepare_callbacks.append(
        lambda: setattr(
            runtime,
            "current",
            replace(
                runtime.current,
                values={"brief": {"task": "changed during provider preparation"}},
            ),
        )
    )

    with pytest.raises(EffectAuthorityDenied, match="no exact"):
        coordinator.reconcile("run-1")

    assert ledger.list_nonterminal()[0].phase == "launching"
    assert runner.ensure_started_calls == []
    assert runner.spawn_count == 0


def test_deadline_crossing_during_prepare_blocks_ensure_started(system) -> None:
    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    coordinator.reconcile("run-1")
    claimed = coordinator.reconcile("run-1")
    runner.prepare_callbacks.append(
        lambda: setattr(coordinator, "_clock", lambda: NOW + timedelta(hours=1))
    )

    report = coordinator.reconcile("run-1")

    assert report.action == "deadline_blocked"
    assert ledger.get(claimed.effect_id).phase == "launching"
    assert runner.ensure_started_calls == []
    assert runner.spawn_count == 0


def test_partial_and_batch_delivery_use_only_current_exact_interrupts(system) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied
    from lockstep.runtime.effects.descriptors import (
        derive_effect_id,
        parse_effect_descriptor,
    )
    from lockstep.runtime.leases import LeaseStore
    from lockstep.runtime.native_models import NativeCoordinate, NativeInterrupt

    coordinator, runtime, _runner, ledger, store, first_coordinate = system
    first_id = coordinator.reconcile("run-1").effect_id
    lease_store = LeaseStore(store, clock=lambda: NOW)
    first_lease = lease_store.acquire("effect", first_id, "seal-first", 30)
    first = ledger.get(first_id)
    first_result = _result(first_id, snapshot_ref="snapshot:" + "1" * 64)
    first = ledger.mark_launching(
        first_id,
        expected_revision=first.revision,
        lease=first_lease,
        runner_binding_digest="b" * 64,
        launch_commitment_digest="f" * 64,
    )
    first = ledger.mark_running(
        first_id,
        expected_revision=first.revision,
        lease=first_lease,
        runner_binding_digest="b" * 64,
    )
    first = ledger.seal(
        first_id,
        first_result,
        expected_revision=first.revision,
        lease=first_lease,
        runner_binding_digest="b" * 64,
    )
    lease_store.release(first_lease)

    second_coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-2", "int-2")
    # Reparse to bind the changed canonical descriptor rather than forge an internal object.
    second_value = dict(runtime.current.pending[0].value["lockstep_effect"])
    second_value["logical_id"] = "review"
    second_descriptor = parse_effect_descriptor(second_value)
    second_id = derive_effect_id(second_coordinate, second_descriptor.digest)
    runtime.current = replace(
        runtime.current,
        pending=(
            runtime.current.pending[0],
            NativeInterrupt(second_coordinate, {"lockstep_effect": second_value}),
        ),
    )
    runtime.history_coordinates.add(second_coordinate)
    with pytest.raises(EffectAuthorityDenied, match="no exact"):
        coordinator.reconcile("run-1")
    coordinator._authority.authorize(coordinator._authority.resolve_intents[-1])
    second_report = coordinator.reconcile("run-1")
    assert second_report.effect_id == second_id
    assert second_report.action == "prepared"
    second_lease = lease_store.acquire("effect", second_id, "seal-second", 30)
    second = ledger.get(second_id)
    second = ledger.mark_launching(
        second_id,
        expected_revision=second.revision,
        lease=second_lease,
        runner_binding_digest="b" * 64,
        launch_commitment_digest="f" * 64,
    )
    second = ledger.mark_running(
        second_id,
        expected_revision=second.revision,
        lease=second_lease,
        runner_binding_digest="b" * 64,
    )
    second_result = _result(second_id, snapshot_ref="snapshot:" + "2" * 64)
    second = ledger.seal(
        second_id,
        second_result,
        expected_revision=second.revision,
        lease=second_lease,
        runner_binding_digest="b" * 64,
    )
    lease_store.release(second_lease)
    coordinator.deliver_ready("run-1", interrupt_ids=[first_coordinate.interrupt_id])
    assert ledger.get(first_id).phase == "delivered"
    assert ledger.get(second_id).phase == "sealed"
    assert runtime.current.pending[0].coordinate == second_coordinate

    coordinator.deliver_ready("run-1")
    assert ledger.get(second_id).phase == "delivered"
    assert runtime.current.pending == ()


def test_overdue_scan_is_bounded_and_nearest_wakeup_is_deterministic(system) -> None:
    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    coordinator.reconcile("run-1")
    wakeups = []

    def wake(delay: float) -> None:
        wakeups.append(delay)
        coordinator._clock = lambda: NOW + timedelta(hours=1)

    assert coordinator.next_wakeup_delay(NOW) == 1.0
    reports = coordinator.wait_and_reconcile_due(wake)
    assert wakeups == [1.0]
    assert len(reports) <= coordinator.MAX_DUE_PER_SCAN
    assert reports[0].run_id == "run-1"
    assert reports[0].action == "sealed"
    assert ledger.get(reports[0].effect_id).fixed_error_code == "deadline_timeout"
    assert runner.ensure_started_calls == []
    assert coordinator.reconcile_due(NOW + timedelta(hours=1)) == ()
    assert coordinator.next_wakeup_delay(NOW + timedelta(hours=1)) == 1.0
