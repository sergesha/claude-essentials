from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.effects.descriptors import parse_effect_result
from lockstep.runtime.effects.ledger import EffectLedger
from lockstep.runtime.native_models import (
    NativeCoordinate,
    NativeInterrupt,
    NativeSnapshot,
)
from lockstep.runtime.storage import SQLiteStore
from tests.runtime.providers.fakes import FakeRunner

NOW = datetime(2026, 8, 20, 10, tzinfo=UTC)


def managed_descriptor(**changes) -> dict:
    value = {
        "schema": "lockstep.effect/v1",
        "kind": "managed",
        "logical_id": "implement",
        "runner": {
            "selector": "codex",
            "required_capabilities": ["workspace", "bounded_result"],
        },
        "inputs": {"brief": {"state_key": "brief"}},
        "writes": ["src/"],
        "artifacts": [],
        "deadline_seconds": 300,
        "scope_state_keys": [],
        "result_schema": "lockstep.effect-result/v1",
    }
    value.update(changes)
    return value


def scope_descriptor(**changes) -> dict:
    value = {
        "schema": "lockstep.effect/v1",
        "kind": "scope",
        "logical_id": "call-scope",
        "scope_kind": "call",
        "duration_seconds": 600,
        "runner_selector": "codex",
        "ancestor_deadline_state_keys": [],
        "result_state_key": "call_scope",
        "result_schema": "lockstep.scope-result/v1",
    }
    value.update(changes)
    return value


class FakeRuntime:
    def __init__(self, binding: RunBinding, snapshot: NativeSnapshot) -> None:
        self._binding = binding
        self.current = snapshot
        self.history_coordinates: set[NativeCoordinate] = {
            item.coordinate for item in snapshot.pending
        }
        self.resume_calls: list[tuple[str, NativeCoordinate, dict[str, object]]] = []
        self.resume_error: Exception | None = None

    def binding(self, run_id: str) -> RunBinding:
        assert run_id == self._binding.public_run_id
        return self._binding

    def snapshot(self, run_id: str, *, subgraphs: bool = False) -> NativeSnapshot:
        assert run_id == self._binding.public_run_id
        assert subgraphs is True
        return self.current

    def coordinate_lineage(self, run_id: str, source: NativeCoordinate) -> str:
        assert run_id == self._binding.public_run_id
        if any(item.coordinate == source for item in self.current.pending):
            return "pending"
        return "descended" if source in self.history_coordinates else "incompatible"

    def resume(self, run_id, source, results_by_interrupt_id):
        if self.resume_error is not None:
            raise self.resume_error
        copied = dict(results_by_interrupt_id)
        self.resume_calls.append((run_id, source, copied))
        supplied = set(copied)
        self.current = replace(
            self.current,
            pending=tuple(
                item
                for item in self.current.pending
                if item.coordinate.interrupt_id not in supplied
            ),
            checkpoint_id="after-resume",
        )
        return self.current


@pytest.fixture
def system(tmp_path):
    from lockstep.runtime.effects.coordinator import EffectCoordinator
    from lockstep.runtime.leases import LeaseStore

    store = SQLiteStore(tmp_path / "runtime.sqlite")
    catalog = RunCatalog(store, clock=lambda: NOW)
    binding = catalog.create(
        RunBinding("run-1", "thread-1", "a" * 64, "bundle:" + "c" * 64, "project-1")
    )
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    snapshot = NativeSnapshot(
        values={"brief": {"task": "implement"}},
        pending=(
            NativeInterrupt(coordinate, {"lockstep_effect": managed_descriptor()}),
        ),
        checkpoint_id="cp-1",
    )
    runtime = FakeRuntime(binding, snapshot)
    runner = FakeRunner()
    ledger = EffectLedger(store, clock=lambda: NOW)
    owners = count()
    coordinator = EffectCoordinator(
        runtime=runtime,
        catalog=catalog,
        ledger=ledger,
        leases=LeaseStore(store, clock=lambda: NOW),
        runners={"codex": runner},
        clock=lambda: NOW,
        owner_factory=lambda: f"coordinator-{next(owners)}",
    )
    yield coordinator, runtime, runner, ledger, store, coordinate
    store.close()


def _advance_to_running(system):
    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    assert coordinator.reconcile("run-1").action == "prepared"
    assert coordinator.reconcile("run-1").action == "launch_claimed"
    report = coordinator.reconcile("run-1")
    assert report.action == "running"
    return ledger.get(report.effect_id), runner


def _result(effect_id: str, *, snapshot_ref: str | None = None):
    return parse_effect_result(
        {
            "schema": "lockstep.effect-result/v1",
            "effect_id": effect_id,
            "outcome": "PASS",
            "result_ref": "blob:" + "d" * 64,
            "artifact_refs": [],
            "snapshot_ref": snapshot_ref,
            "diff_ref": None,
            "fixed_error_code": None,
            "evidence_refs": [],
        }
    )


def test_parked_interrupt_creates_durable_intent_before_any_spawn(system) -> None:
    coordinator, _runtime, runner, ledger, _store, coordinate = system

    report = coordinator.reconcile("run-1")

    assert report.action == "prepared"
    assert runner.spawn_count == 0
    assert runner.ensure_started_calls == []
    record = ledger.get(report.effect_id)
    assert record.coordinate == coordinate
    assert record.phase == "prepared"
    assert record.workspace_ref is None
    assert runner.prepare_calls == []

    workspace = coordinator.reconcile("run-1")
    assert workspace.action == "launch_claimed"
    assert runner.prepare_calls[0].project_identity == "project-1"
    assert runner.prepare_calls[0].definition_digest == "a" * 64
    assert ledger.get(report.effect_id).workspace_ref is not None


def test_launch_is_claimed_before_single_idempotent_spawn(system) -> None:
    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    coordinator.reconcile("run-1")

    assert coordinator.reconcile("run-1").action == "launch_claimed"
    assert runner.spawn_count == 0
    assert ledger.list_nonterminal()[0].phase == "launching"

    assert coordinator.reconcile("run-1").action == "running"
    assert coordinator.reconcile("run-1").action == "running"
    assert runner.spawn_count == 1
    assert len(runner.ensure_started_calls) == 1
    assert runner.inspect_calls


def test_changed_workspace_after_durable_intent_cannot_cross_launch_claim(
    system,
) -> None:
    from lockstep.runtime.effects.coordinator import ProviderContractViolation

    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    prepared = coordinator.reconcile("run-1")
    assert coordinator.reconcile("run-1").action == "launch_claimed"
    runner.workspace_refs.append("workspace:foreign-generation")

    with pytest.raises(ProviderContractViolation, match="workspace"):
        coordinator.reconcile("run-1")

    assert ledger.get(prepared.effect_id).phase == "launching"
    assert runner.ensure_started_calls == []


def test_runner_binding_rotation_rejects_before_provider_contact(system) -> None:
    from lockstep.runtime.effects.coordinator import ProviderContractViolation

    coordinator, _runtime, _runner, ledger, _store, _coordinate = system
    prepared = coordinator.reconcile("run-1")
    rotated = FakeRunner(binding_digest="c" * 64)
    coordinator._runners["codex"] = rotated

    with pytest.raises(ProviderContractViolation, match="runner binding"):
        coordinator.reconcile("run-1")

    assert ledger.get(prepared.effect_id).phase == "prepared"
    assert rotated.prepare_calls == []
    assert rotated.ensure_started_calls == []


def test_incompatible_or_changed_native_lineage_grants_nothing(system) -> None:
    from lockstep.runtime.effects.coordinator import CoordinatorLineageError

    coordinator, runtime, runner, ledger, _store, coordinate = system
    coordinator.reconcile("run-1")
    prepare_calls = len(runner.prepare_calls)
    runtime.current = replace(
        runtime.current,
        pending=(
            NativeInterrupt(
                replace(coordinate, checkpoint_id="foreign"),
                runtime.current.pending[0].value,
            ),
        ),
    )
    runtime.history_coordinates.clear()

    with pytest.raises(CoordinatorLineageError, match="lineage"):
        coordinator.reconcile("run-1")
    assert len(runner.prepare_calls) == prepare_calls
    assert ledger.list_nonterminal()[0].phase == "prepared"


def test_provider_scope_result_and_binding_mismatch_cannot_seal(system) -> None:
    from lockstep.runtime.effects.coordinator import ProviderContractViolation
    from lockstep.runtime.effects.descriptors import build_scope_result
    from lockstep.runtime.providers.base import RunnerObservation

    running, runner = _advance_to_running(system)
    launch = runner.ensure_started_calls[0]
    forged_scope = build_scope_result(
        effect_id=running.effect_id,
        scope_digest="e" * 64,
        scope_kind="parallel",
        now=NOW,
        duration_seconds=None,
        ancestors=(),
    )
    runner.inspect_observations.append(
        RunnerObservation(
            effect_id=running.effect_id,
            request_digest=launch.request_digest,
            runner_binding_digest=launch.runner_binding_digest,
            state="terminal",
            result=forged_scope,
        )
    )

    with pytest.raises(ProviderContractViolation):
        system[0].reconcile("run-1")
    assert system[3].get(running.effect_id).phase == "running"

    runner.inspect_observations.append(
        runner.mismatch(runner.terminal(launch, _result(running.effect_id)))
    )
    with pytest.raises(ProviderContractViolation):
        system[0].reconcile("run-1")
    assert system[3].get(running.effect_id).phase == "running"


def test_oversized_provider_launch_is_rejected_before_launch_claim(system) -> None:
    from lockstep.runtime.effects.coordinator import ProviderContractViolation

    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    prepared = coordinator.reconcile("run-1")
    original_prepare = runner.prepare

    def oversized_launch(request):
        return replace(original_prepare(request), launch_ref="x" * 4097)

    runner.prepare = oversized_launch
    with pytest.raises(ProviderContractViolation, match="launch_ref"):
        coordinator.reconcile("run-1")

    assert ledger.get(prepared.effect_id).phase == "prepared"
    assert runner.ensure_started_calls == []


def test_oversized_provider_result_cannot_reach_ledger(system) -> None:
    from lockstep.runtime.effects.coordinator import ProviderContractViolation

    running, runner = _advance_to_running(system)
    launch = runner.ensure_started_calls[0]
    oversized = replace(_result(running.effect_id), result_ref="x" * 4097)
    runner.inspect_observations.append(runner.terminal(launch, oversized))

    with pytest.raises(ProviderContractViolation, match="closed bounded"):
        system[0].reconcile("run-1")

    assert system[3].get(running.effect_id).phase == "running"


def test_scope_is_engine_owned_and_inherits_earliest_deadline_without_runner(
    system,
) -> None:
    coordinator, runtime, runner, ledger, _store, coordinate = system
    ancestor_deadline = NOW + timedelta(seconds=120)
    runtime.current = NativeSnapshot(
        values={
            "outer": {
                "schema": "lockstep.scope-result/v1",
                "effect_id": "outer-effect",
                "outcome": "PASS",
                "scope_kind": "parallel",
                "scope_digest": "e" * 64,
                "absolute_deadline": ancestor_deadline.isoformat(),
            }
        },
        pending=(
            NativeInterrupt(
                coordinate,
                {
                    "lockstep_effect": scope_descriptor(
                        ancestor_deadline_state_keys=["outer"]
                    )
                },
            ),
        ),
        checkpoint_id="cp-1",
    )

    prepared = coordinator.reconcile("run-1")
    sealed = coordinator.reconcile("run-1")

    assert prepared.action == "prepared"
    assert sealed.action == "sealed"
    assert ledger.get(sealed.effect_id).result.absolute_deadline == ancestor_deadline
    assert runner.prepare_calls == []
    assert runner.ensure_started_calls == []


def test_scope_expiring_after_prepare_seals_timeout_error(system) -> None:
    coordinator, runtime, runner, ledger, _store, coordinate = system
    runtime.current = NativeSnapshot(
        values={},
        pending=(
            NativeInterrupt(
                coordinate,
                {"lockstep_effect": scope_descriptor(duration_seconds=1)},
            ),
        ),
        checkpoint_id="cp-1",
    )

    prepared = coordinator.reconcile("run-1")
    coordinator._clock = lambda: NOW + timedelta(seconds=2)
    sealed = coordinator.reconcile("run-1")

    assert prepared.action == "prepared"
    assert sealed.action == "sealed"
    result = ledger.get(sealed.effect_id).result
    assert result.outcome == "ERROR"
    assert result.fixed_error_code == "scope_timeout"
    assert result.absolute_deadline is None
    assert result.runner_binding_digest is None
    assert runner.prepare_calls == []
    assert runner.ensure_started_calls == []


def test_call_scope_binding_rotation_rejects_member_before_provider_contact(
    system,
) -> None:
    from lockstep.runtime.effects.coordinator import (
        EffectCoordinator,
        ProviderContractViolation,
    )
    from lockstep.runtime.leases import LeaseStore

    _coordinator, runtime, _runner, ledger, store, coordinate = system
    runtime.current = NativeSnapshot(
        values={
            "call_scope": {
                "schema": "lockstep.scope-result/v1",
                "effect_id": "call-scope-effect",
                "outcome": "PASS",
                "scope_kind": "call",
                "scope_digest": "e" * 64,
                "absolute_deadline": (NOW + timedelta(seconds=120)).isoformat(),
                "runner_selector": "codex",
                "runner_binding_digest": "b" * 64,
            },
            "brief": "work",
        },
        pending=(
            NativeInterrupt(
                coordinate,
                {
                    "lockstep_effect": managed_descriptor(
                        scope_state_keys=["call_scope"]
                    )
                },
            ),
        ),
        checkpoint_id="cp-1",
    )
    rotated_runner = FakeRunner(binding_digest="c" * 64)
    restarted = EffectCoordinator(
        runtime=runtime,
        catalog=RunCatalog(store, clock=lambda: NOW),
        ledger=ledger,
        leases=LeaseStore(store, clock=lambda: NOW),
        runners={"codex": rotated_runner},
        clock=lambda: NOW,
        owner_factory=lambda: "restarted-coordinator",
    )

    with pytest.raises(ProviderContractViolation, match="scope runner binding"):
        restarted.reconcile("run-1")

    assert ledger.list_nonterminal() == []
    assert rotated_runner.prepare_calls == []
    assert rotated_runner.ensure_started_calls == []


def test_member_request_commits_matching_graph_owned_scope_bindings(system) -> None:
    coordinator, runtime, runner, _ledger, _store, coordinate = system
    runtime.current = NativeSnapshot(
        values={
            "call_scope": {
                "schema": "lockstep.scope-result/v1",
                "effect_id": "call-scope-effect",
                "outcome": "PASS",
                "scope_kind": "call",
                "scope_digest": "e" * 64,
                "absolute_deadline": (NOW + timedelta(seconds=120)).isoformat(),
                "runner_selector": "codex",
                "runner_binding_digest": "b" * 64,
            },
            "brief": "work",
        },
        pending=(
            NativeInterrupt(
                coordinate,
                {
                    "lockstep_effect": managed_descriptor(
                        scope_state_keys=["call_scope"]
                    )
                },
            ),
        ),
        checkpoint_id="cp-1",
    )

    assert coordinator.reconcile("run-1").action == "prepared"
    assert coordinator.reconcile("run-1").action == "launch_claimed"

    request = runner.prepare_calls[0]
    assert request.deadline_at == NOW + timedelta(seconds=120)
    assert len(request.scope_bindings) == 1
    assert request.scope_bindings[0].scope_digest == "e" * 64
    assert request.scope_bindings[0].runner_binding_digest == "b" * 64


def test_already_expired_scope_member_seals_without_spawn(system) -> None:
    coordinator, runtime, runner, ledger, _store, coordinate = system
    runtime.current = NativeSnapshot(
        values={
            "outer": {
                "schema": "lockstep.scope-result/v1",
                "effect_id": "outer-effect",
                "outcome": "PASS",
                "scope_kind": "parallel",
                "scope_digest": "e" * 64,
                "absolute_deadline": (NOW - timedelta(seconds=1)).isoformat(),
            },
            "brief": "work",
        },
        pending=(
            NativeInterrupt(
                coordinate,
                {"lockstep_effect": managed_descriptor(scope_state_keys=["outer"])},
            ),
        ),
        checkpoint_id="cp-1",
    )

    assert coordinator.reconcile("run-1").action == "prepared"
    sealed = coordinator.reconcile("run-1")

    assert sealed.action == "sealed"
    assert ledger.get(sealed.effect_id).fixed_error_code == "deadline_timeout"
    assert runner.spawn_count == 0
    assert runner.ensure_started_calls == []
    assert runner.prepare_calls == []


def test_timed_out_ancestor_scope_member_seals_without_provider_contact(system) -> None:
    coordinator, runtime, runner, ledger, _store, coordinate = system
    runtime.current = NativeSnapshot(
        values={
            "outer": {
                "schema": "lockstep.scope-result/v1",
                "effect_id": "outer-effect",
                "outcome": "ERROR",
                "scope_kind": "parallel",
                "scope_digest": "e" * 64,
                "fixed_error_code": "scope_timeout",
            },
            "brief": "work",
        },
        pending=(
            NativeInterrupt(
                coordinate,
                {"lockstep_effect": managed_descriptor(scope_state_keys=["outer"])},
            ),
        ),
        checkpoint_id="cp-1",
    )

    coordinator.reconcile("run-1")
    sealed = coordinator.reconcile("run-1")

    assert sealed.action == "sealed"
    assert ledger.get(sealed.effect_id).fixed_error_code == "deadline_timeout"
    assert runner.prepare_calls == []
    assert runner.ensure_started_calls == []


def test_terminal_result_waits_for_quiescence_and_managed_rollover(system) -> None:
    from lockstep.runtime.providers.base import TerminalSafetyObservation

    running, runner = _advance_to_running(system)
    launch = runner.ensure_started_calls[0]
    result = _result(running.effect_id, snapshot_ref="snapshot:" + "e" * 64)
    runner.inspect_observations.extend(
        [runner.terminal(launch, result), runner.terminal(launch, result)]
    )
    runner.safety_observations.extend(
        [
            TerminalSafetyObservation.pending_for(launch),
            TerminalSafetyObservation.proven_for(
                launch,
                rollover_snapshot_ref=result.snapshot_ref,
                result_stable=True,
            ),
        ]
    )

    assert system[0].reconcile("run-1").action == "quiescence_pending"
    assert system[3].get(running.effect_id).phase == "running"
    assert system[0].reconcile("run-1").action == "sealed"
    assert system[3].get(running.effect_id).result == result


def test_sealed_result_is_visible_only_after_native_resume_commit(system) -> None:
    from lockstep.runtime.providers.base import TerminalSafetyObservation

    running, runner = _advance_to_running(system)
    launch = runner.ensure_started_calls[0]
    result = _result(running.effect_id, snapshot_ref="snapshot:" + "e" * 64)
    runner.inspect_observations.append(runner.terminal(launch, result))
    runner.safety_observations.append(
        TerminalSafetyObservation.proven_for(
            launch, rollover_snapshot_ref=result.snapshot_ref, result_stable=True
        )
    )
    assert system[0].reconcile("run-1").action == "sealed"
    assert system[1].resume_calls == []

    system[1].resume_error = RuntimeError("crash before native commit")
    with pytest.raises(RuntimeError, match="native commit"):
        system[0].deliver_ready("run-1")
    assert system[3].get(running.effect_id).phase == "sealed"

    system[1].resume_error = None
    status = system[0].deliver_ready("run-1")
    assert status.run_id == "run-1"
    assert system[1].resume_calls[0][2] == {system[5].interrupt_id: result.to_dict()}
    assert system[3].get(running.effect_id).phase == "delivered"


def test_post_commit_crash_marks_delivered_from_public_lineage_without_resume(
    system,
) -> None:
    from lockstep.runtime.providers.base import TerminalSafetyObservation

    running, runner = _advance_to_running(system)
    launch = runner.ensure_started_calls[0]
    result = _result(running.effect_id, snapshot_ref="snapshot:" + "e" * 64)
    runner.inspect_observations.append(runner.terminal(launch, result))
    runner.safety_observations.append(
        TerminalSafetyObservation.proven_for(
            launch, rollover_snapshot_ref=result.snapshot_ref, result_stable=True
        )
    )
    system[0].reconcile("run-1")
    system[1].current = replace(system[1].current, pending=(), checkpoint_id="after")

    report = system[0].reconcile("run-1")

    assert report.action == "delivered"
    assert system[1].resume_calls == []
    assert system[3].get(running.effect_id).phase == "delivered"


def test_unknown_plain_interrupt_never_creates_effect_authority(system) -> None:
    coordinator, runtime, runner, ledger, _store, coordinate = system
    runtime.current = replace(
        runtime.current,
        pending=(NativeInterrupt(coordinate, {"question": "continue?"}),),
    )
    assert coordinator.reconcile("run-1").action == "no_effect"
    assert ledger.list_nonterminal() == []
    assert runner.prepare_calls == []


def test_unknown_delivery_selector_is_rejected_without_native_resume(system) -> None:
    from lockstep.runtime.effects.coordinator import CoordinatorLineageError

    coordinator, runtime, _runner, ledger, _store, _coordinate = system
    prepared = coordinator.reconcile("run-1")

    with pytest.raises(CoordinatorLineageError, match="requested interrupt"):
        coordinator.deliver_ready("run-1", interrupt_ids=["unknown"])

    assert runtime.resume_calls == []
    assert ledger.get(prepared.effect_id).phase == "prepared"
