from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import count
import threading
from threading import RLock

import pytest

from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_descriptor,
    parse_effect_result,
    parse_scope_result,
)
from lockstep.runtime.effects.ledger import EffectLedger
from lockstep.runtime.native_models import (
    NativeCoordinate,
    NativeInterrupt,
    NativeInterruptOccurrence,
    NativeLineageProof,
    NativeSnapshot,
)
from lockstep.runtime.providers.base import EffectRequest
from lockstep.runtime.storage import SQLiteStore
from tests.runtime.providers.fakes import FakeEffectAuthority, FakeRunner

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


def delivered_scope(
    system,
    *,
    state_key: str,
    scope_kind: str = "parallel",
    deadline: datetime | None = None,
    outcome: str = "PASS",
) -> dict:
    from lockstep.runtime.leases import LeaseStore

    _coordinator, runtime, _runner, ledger, store, member_coordinate = system
    coordinate = NativeCoordinate(
        member_coordinate.thread_id,
        f"scope-checkpoint-{state_key}",
        "",
        f"scope-task-{state_key}",
        f"scope-interrupt-{state_key}",
    )
    raw_descriptor = scope_descriptor(
        logical_id=f"{state_key}-producer",
        scope_kind=scope_kind,
        duration_seconds=None,
        runner_selector="codex" if scope_kind == "call" else None,
        result_state_key=state_key,
    )
    descriptor = parse_effect_descriptor(raw_descriptor)
    effect_id = derive_effect_id(coordinate, descriptor.digest)
    if outcome == "ERROR":
        raw_result = {
            "schema": "lockstep.scope-result/v1",
            "effect_id": effect_id,
            "outcome": "ERROR",
            "scope_kind": scope_kind,
            "scope_digest": descriptor.digest,
            "fixed_error_code": "scope_timeout",
        }
    else:
        raw_result = {
            "schema": "lockstep.scope-result/v1",
            "effect_id": effect_id,
            "outcome": "PASS",
            "scope_kind": scope_kind,
            "scope_digest": descriptor.digest,
            "absolute_deadline": None if deadline is None else deadline.isoformat(),
        }
        if scope_kind == "call":
            raw_result["runner_selector"] = "codex"
            raw_result["runner_binding_digest"] = "b" * 64
    result = parse_scope_result(raw_result)
    leases = LeaseStore(store, clock=lambda: NOW)
    lease = leases.acquire("effect", effect_id, f"scope-owner-{state_key}", 30)
    try:
        record = ledger.prepare(
            coordinate,
            descriptor,
            deadline_at=deadline,
            runner_binding_digest="b" * 64 if scope_kind == "call" else None,
            workspace_ref=None,
            lease=lease,
        )
        record = ledger.seal(
            effect_id,
            result,
            expected_revision=record.revision,
            lease=lease,
            scope_descriptor=descriptor,
        )
        ledger.mark_delivered(effect_id, expected_revision=record.revision, lease=lease)
    finally:
        leases.release(lease)
    interrupt = NativeInterrupt(coordinate, {"lockstep_effect": raw_descriptor})
    runtime.history_coordinates.add(coordinate)
    runtime.history_values[coordinate] = interrupt.value
    runtime.ancestry_pairs.add((coordinate, member_coordinate))
    return result.to_dict()


class FakeRuntime:
    def __init__(self, binding: RunBinding, snapshot: NativeSnapshot) -> None:
        self._binding = binding
        self.current = snapshot
        self.history_coordinates: set[NativeCoordinate] = {
            item.coordinate for item in snapshot.pending
        }
        self.history_values = {item.coordinate: item.value for item in snapshot.pending}
        self.ancestry_pairs: set[tuple[NativeCoordinate, NativeCoordinate]] = set()
        self.resume_calls: list[tuple[str, NativeCoordinate, dict[str, object]]] = []
        self.resume_error: Exception | None = None
        self.commitment_callbacks = []
        self._decision_lock = RLock()
        self.decision_guard_depth = 0
        self.decision_guard_entries = 0

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

    def interrupt_lineage(
        self, run_id: str, source: NativeCoordinate
    ) -> NativeLineageProof | None:
        assert run_id == self._binding.public_run_id
        current = tuple(
            item for item in self.current.pending if item.coordinate == source
        )
        if len(current) == 1:
            item = current[0]
            return NativeLineageProof(
                "pending", NativeInterruptOccurrence(item.coordinate, item.value)
            )
        if source not in self.history_coordinates:
            return None
        value = self.history_values[source]
        return NativeLineageProof("descended", NativeInterruptOccurrence(source, value))

    def checkpoint_is_ancestor(self, run_id, ancestor, descendant):
        assert run_id == self._binding.public_run_id
        return (ancestor, descendant.coordinate) in self.ancestry_pairs

    def resume(self, run_id, source, results_by_interrupt_id):
        if self.resume_error is not None:
            raise self.resume_error
        copied = dict(results_by_interrupt_id)
        self.resume_calls.append((run_id, source, copied))
        supplied = set(copied)
        for item in self.current.pending:
            if item.coordinate.interrupt_id in supplied:
                self.history_coordinates.add(item.coordinate)
                self.history_values[item.coordinate] = item.value
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

    @contextmanager
    def decision_guard(self, run_id):
        assert run_id == self._binding.public_run_id
        with self._decision_lock:
            self.decision_guard_entries += 1
            self.decision_guard_depth += 1
            try:
                yield
            finally:
                self.decision_guard_depth -= 1

    @contextmanager
    def commitment_guard(self, run_id, source):
        from lockstep.runtime.graph_runtime import NativeCommitment

        if self.commitment_callbacks:
            self.commitment_callbacks.pop(0)()
        matches = tuple(
            item for item in self.current.pending if item.coordinate == source
        )
        if len(matches) != 1:
            from lockstep.runtime.graph_runtime import NativeCoordinateRejected

            raise NativeCoordinateRejected(
                "commitment source is not the exact current interrupt"
            )
        yield NativeCommitment(self._binding, self.current, matches[0])


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
    authority = FakeEffectAuthority(clock=lambda: NOW)
    descriptor = parse_effect_descriptor(managed_descriptor())
    authority.authorize(
        EffectRequest.build(
            effect_id=derive_effect_id(coordinate, descriptor.digest),
            public_run_id=binding.public_run_id,
            project_identity=binding.project_identity,
            definition_digest=binding.recipe_digest,
            coordinate=coordinate,
            descriptor_digest=descriptor.digest,
            effect_kind=descriptor.kind,
            runner_selector=descriptor.runner.selector,
            runner_binding_digest=runner.binding_digest,
            required_capabilities=descriptor.runner.required_capabilities,
            inputs=(("brief", snapshot.values["brief"]),),
            writes=descriptor.writes,
            deadline_at=NOW + timedelta(seconds=descriptor.deadline_seconds),
        )
    )
    ledger = EffectLedger(store, clock=lambda: NOW)
    owners = count()
    coordinator = EffectCoordinator(
        runtime=runtime,
        catalog=catalog,
        ledger=ledger,
        leases=LeaseStore(store, clock=lambda: NOW),
        runners={"codex": runner},
        authority=authority,
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
    assert record.workspace_ref == f"workspace:{report.effect_id}"
    assert runner.prepare_calls == []

    workspace = coordinator.reconcile("run-1")
    assert workspace.action == "launch_claimed"
    assert runner.prepare_calls[0].project_identity == "project-1"
    assert runner.prepare_calls[0].definition_digest == "a" * 64
    assert ledger.get(report.effect_id).workspace_ref is not None


def test_artifact_declarations_are_not_erased_before_authority(system) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied

    coordinator, runtime, runner, _ledger, _store, coordinate = system
    raw = managed_descriptor(
        artifacts=[{
            "name": "review", "source_path": "src/review.md",
            "media_type": "text/markdown", "required": True,
        }]
    )
    runtime.current = replace(
        runtime.current,
        pending=(NativeInterrupt(coordinate, {"lockstep_effect": raw}),),
    )

    with pytest.raises(EffectAuthorityDenied, match="exact effect grant"):
        coordinator.reconcile("run-1")
    assert runner.prepare_calls == []


def test_definitive_prepare_rejection_is_durably_sealed(system) -> None:
    from lockstep.runtime.providers.base import DefinitiveProviderFailure

    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    prepared = coordinator.reconcile("run-1")
    rejection = parse_effect_result(
        {
            "schema": "lockstep.effect-result/v1",
            "effect_id": prepared.effect_id,
            "outcome": "ERROR",
            "result_ref": None,
            "artifact_refs": [],
            "snapshot_ref": None,
            "diff_ref": None,
            "fixed_error_code": "prelaunch_failed",
            "evidence_refs": [],
        }
    )

    def reject(_request):
        raise DefinitiveProviderFailure(rejection)

    runner.prepare = reject

    sealed = coordinator.reconcile("run-1")
    assert sealed.action == "sealed"
    assert ledger.get(prepared.effect_id).result == rejection


def test_definitive_prepare_rejection_cannot_smuggle_result_refs(system) -> None:
    from lockstep.runtime.effects.coordinator import ProviderContractViolation
    from lockstep.runtime.providers.base import DefinitiveProviderFailure

    coordinator, _runtime, runner, _ledger, _store, _coordinate = system
    prepared = coordinator.reconcile("run-1")
    rejection = parse_effect_result(
        {
            "schema": "lockstep.effect-result/v1",
            "effect_id": prepared.effect_id,
            "outcome": "ERROR",
            "result_ref": "blob:" + "d" * 64,
            "artifact_refs": [],
            "snapshot_ref": None,
            "diff_ref": None,
            "fixed_error_code": "prelaunch_failed",
            "evidence_refs": [],
        }
    )
    runner.prepare = lambda _request: (_ for _ in ()).throw(
        DefinitiveProviderFailure(rejection)
    )

    with pytest.raises(ProviderContractViolation, match="closed ERROR"):
        coordinator.reconcile("run-1")


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


def test_runner_binding_rotation_recovers_existing_intent_by_durable_binding(
    system,
) -> None:
    coordinator, _runtime, original, ledger, _store, _coordinate = system
    prepared = coordinator.reconcile("run-1")
    rotated = FakeRunner(binding_digest="c" * 64)
    coordinator._runners["codex"] = rotated

    report = coordinator.reconcile("run-1")

    assert report.action == "launch_claimed"
    assert ledger.get(prepared.effect_id).phase == "launching"
    assert len(original.prepare_calls) == 1
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
    outer = delivered_scope(system, state_key="outer", deadline=ancestor_deadline)
    runtime.current = NativeSnapshot(
        values={"outer": outer},
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
    call_scope = delivered_scope(
        system,
        state_key="call_scope",
        scope_kind="call",
        deadline=NOW + timedelta(seconds=120),
    )
    runtime.current = NativeSnapshot(
        values={
            "call_scope": call_scope,
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
        authority=_coordinator._authority,
        clock=lambda: NOW,
        owner_factory=lambda: "restarted-coordinator",
    )

    with pytest.raises(ProviderContractViolation, match="scope runner binding"):
        restarted.reconcile("run-1")

    assert ledger.list_nonterminal() == []
    assert rotated_runner.prepare_calls == []
    assert rotated_runner.ensure_started_calls == []


def test_member_request_commits_matching_graph_owned_scope_bindings(system) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied

    coordinator, runtime, runner, _ledger, _store, coordinate = system
    call_scope = delivered_scope(
        system,
        state_key="call_scope",
        scope_kind="call",
        deadline=NOW + timedelta(seconds=120),
    )
    runtime.current = NativeSnapshot(
        values={
            "call_scope": call_scope,
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

    with pytest.raises(EffectAuthorityDenied, match="no exact"):
        coordinator.reconcile("run-1")
    coordinator._authority.authorize(coordinator._authority.resolve_intents[-1])
    assert coordinator.reconcile("run-1").action == "prepared"
    assert coordinator.reconcile("run-1").action == "launch_claimed"

    request = runner.prepare_calls[0]
    assert request.deadline_at == NOW + timedelta(seconds=120)
    assert len(request.scope_bindings) == 1
    assert request.scope_bindings[0].scope_digest == call_scope["scope_digest"]
    assert request.scope_bindings[0].runner_binding_digest == "b" * 64
    assert request.scope_bindings[0].state_key == "call_scope"
    assert request.scope_bindings[0].producer_effect_id == call_scope["effect_id"]


def test_raw_scope_state_without_delivered_ledger_producer_grants_nothing(
    system,
) -> None:
    from lockstep.runtime.effects.coordinator import CoordinatorLineageError

    coordinator, runtime, runner, ledger, _store, coordinate = system
    runtime.current = NativeSnapshot(
        values={
            "outer": {
                "schema": "lockstep.scope-result/v1",
                "effect_id": "unproven-scope-effect",
                "outcome": "PASS",
                "scope_kind": "parallel",
                "scope_digest": "e" * 64,
                "absolute_deadline": (NOW + timedelta(seconds=120)).isoformat(),
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

    with pytest.raises(CoordinatorLineageError, match="ledger-proven scope producer"):
        coordinator.reconcile("run-1")

    assert ledger.list_nonterminal() == []
    assert runner.prepare_calls == []


def test_delivered_sibling_scope_is_not_accepted_as_consumer_ancestor(system) -> None:
    from lockstep.runtime.effects.coordinator import CoordinatorLineageError

    coordinator, runtime, runner, ledger, _store, coordinate = system
    sibling = delivered_scope(system, state_key="outer")
    runtime.ancestry_pairs.clear()
    runtime.current = NativeSnapshot(
        values={"outer": sibling, "brief": "work"},
        pending=(
            NativeInterrupt(
                coordinate,
                {"lockstep_effect": managed_descriptor(scope_state_keys=["outer"])},
            ),
        ),
        checkpoint_id="cp-1",
    )

    with pytest.raises(CoordinatorLineageError, match="not an ancestor"):
        coordinator.reconcile("run-1")

    assert all(record.effect_kind == "scope" for record in ledger.list_nonterminal())
    assert runner.prepare_calls == []


def test_effect_request_rejects_aggregate_input_resource_bomb() -> None:
    from lockstep.runtime.native_models import NativeCoordinate
    from lockstep.runtime.payload_limits import PayloadLimitExceeded
    from lockstep.runtime.providers.base import EffectRequest

    coordinate = NativeCoordinate("thread", "checkpoint", "", "task", "interrupt")
    inputs = tuple((f"input-{index}", {"value": index}) for index in range(3000))

    with pytest.raises(PayloadLimitExceeded):
        EffectRequest.build(
            effect_id="effect",
            public_run_id="run",
            project_identity="project",
            definition_digest="a" * 64,
            coordinate=coordinate,
            descriptor_digest="b" * 64,
            effect_kind="managed",
            runner_selector="codex",
            runner_binding_digest="c" * 64,
            required_capabilities=("workspace",),
            inputs=inputs,
            writes=("src/",),
            deadline_at=NOW,
        )


def test_effect_coordinator_has_no_implicit_allow_authority(system) -> None:
    from lockstep.runtime.effects.coordinator import EffectCoordinator
    from lockstep.runtime.leases import LeaseStore

    _coordinator, runtime, _runner, ledger, store, _coordinate = system
    with pytest.raises(TypeError, match="authority"):
        EffectCoordinator(
            runtime=runtime,
            catalog=RunCatalog(store, clock=lambda: NOW),
            ledger=ledger,
            leases=LeaseStore(store, clock=lambda: NOW),
            runners={"codex": FakeRunner()},
            clock=lambda: NOW,
        )


def test_remote_reconciliation_adapter_is_rejected_before_authority_or_contact(
    system,
) -> None:
    from lockstep.runtime.effects.coordinator import ProviderContractViolation

    coordinator, _runtime, runner, ledger, _store, _coordinate = system
    runner.reconciliation_boundary = "remote_provider"

    with pytest.raises(ProviderContractViolation, match="local durable handle"):
        coordinator.reconcile("run-1")

    assert ledger.list_nonterminal() == []
    assert runner.prepare_calls == []


def test_already_expired_scope_member_seals_without_spawn(system) -> None:
    coordinator, runtime, runner, ledger, _store, coordinate = system
    outer = delivered_scope(
        system, state_key="outer", deadline=NOW - timedelta(seconds=1)
    )
    runtime.current = NativeSnapshot(
        values={
            "outer": outer,
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
    outer = delivered_scope(system, state_key="outer", outcome="ERROR")
    runtime.current = NativeSnapshot(
        values={
            "outer": outer,
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


def test_rejected_managed_output_seals_only_with_quarantine_proof(system) -> None:
    from lockstep.runtime.providers.base import TerminalSafetyObservation

    running, runner = _advance_to_running(system)
    launch = runner.ensure_started_calls[0]
    result = parse_effect_result(
        {
            "schema": "lockstep.effect-result/v1",
            "effect_id": running.effect_id,
            "outcome": "ERROR",
            "result_ref": None,
            "artifact_refs": [],
            "snapshot_ref": None,
            "diff_ref": None,
            "fixed_error_code": "writes_invalid",
            "evidence_refs": [],
        }
    )
    runner.inspect_observations.append(runner.terminal(launch, result))
    runner.safety_observations.append(
        TerminalSafetyObservation.proven_for(
            launch,
            result_stable=True,
            workspace_quarantined=True,
        )
    )

    assert system[0].reconcile("run-1").action == "sealed"
    assert system[3].get(running.effect_id).result == result


def test_quarantine_proof_cannot_seal_managed_pass_without_snapshot(system) -> None:
    from lockstep.runtime.effects.coordinator import ProviderContractViolation
    from lockstep.runtime.providers.base import TerminalSafetyObservation

    running, runner = _advance_to_running(system)
    launch = runner.ensure_started_calls[0]
    result = _result(running.effect_id)
    runner.inspect_observations.append(runner.terminal(launch, result))
    runner.safety_observations.append(
        TerminalSafetyObservation.proven_for(
            launch,
            result_stable=True,
            workspace_quarantined=True,
        )
    )

    with pytest.raises(ProviderContractViolation, match="PASS/FAIL"):
        system[0].reconcile("run-1")


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


def test_concurrent_delivery_owner_cannot_repeat_native_resume(system) -> None:
    from lockstep.runtime.leases import LeaseStore
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

    competing_leases = LeaseStore(system[4], clock=lambda: NOW)
    competing = competing_leases.acquire(
        "effect", running.effect_id, "competing-delivery", 30
    )
    try:
        status = system[0].deliver_ready("run-1")
    finally:
        competing_leases.release(competing)

    assert status.status == "running"
    assert status.owner == "engine"
    assert system[1].resume_calls == []
    system[0].deliver_ready("run-1")
    assert len(system[1].resume_calls) == 1
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


def test_post_commit_same_coordinate_with_changed_descriptor_cannot_deliver(
    system,
) -> None:
    from lockstep.runtime.effects.coordinator import CoordinatorLineageError
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
    system[1].current = replace(system[1].current, pending=(), checkpoint_id="after")
    system[1].history_values[system[5]] = {
        "lockstep_effect": managed_descriptor(logical_id="foreign")
    }

    with pytest.raises(CoordinatorLineageError, match="descriptor differs"):
        system[0].reconcile("run-1")

    assert system[3].get(running.effect_id).phase == "sealed"
    assert system[1].resume_calls == []


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


def test_artifact_admission_seals_registry_refs_before_native_visibility(
    system, tmp_path
) -> None:
    """Catches provider refs, unbound snapshots, or refs visible before delivery."""
    from lockstep.runtime.artifacts import ArtifactRegistry
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.effects.authority import EffectAuthorityDenied
    from lockstep.runtime.effects.coordinator import EffectCoordinator
    from lockstep.runtime.leases import LeaseStore
    from lockstep.runtime.project_snapshots import ProjectSnapshotStore
    from lockstep.runtime.providers.base import TerminalSafetyObservation

    old, runtime, runner, ledger, store, coordinate = system
    raw = managed_descriptor(
        writes=["review.md"],
        artifacts=[
            {
                "name": "review",
                "source_path": "review.md",
                "media_type": "text/markdown",
                "required": True,
            }
        ],
    )
    runtime.current = NativeSnapshot(
        values={"brief": {"task": "review"}},
        pending=(NativeInterrupt(coordinate, {"lockstep_effect": raw}),),
        checkpoint_id="cp-1",
    )
    runtime.history_values[coordinate] = runtime.current.pending[0].value

    with pytest.raises(EffectAuthorityDenied, match="exact effect grant"):
        old.reconcile("run-1")
    intent = old._authority.resolve_intents[-1]
    grant = old._authority.authorize(intent)
    request = intent.bind_grant(grant)

    owner = tmp_path / "artifact-owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    registry = ArtifactRegistry(owner, blobs, snapshots)
    snapshot_ref = snapshots.capture(
        {"review.md": blobs.put(b"reviewed\n")},
        declared_paths=("review.md",),
        provenance={
            "source": "managed-workspace-rollover",
            "request_digest": request.request_digest,
            "workspace_ref": request.workspace_ref,
        },
    )
    coordinator = EffectCoordinator(
        runtime=runtime,
        catalog=old._catalog,
        ledger=ledger,
        leases=LeaseStore(store, clock=lambda: NOW),
        runners={"codex": runner},
        authority=old._authority,
        artifacts=registry,
        clock=lambda: NOW,
        owner_factory=lambda: "artifact-coordinator",
    )

    assert coordinator.reconcile("run-1").action == "prepared"
    assert coordinator.reconcile("run-1").action == "launch_claimed"
    running = coordinator.reconcile("run-1")
    launch = runner.ensure_started_calls[-1]
    result = _result(
        running.effect_id, snapshot_ref=f"snapshot:{snapshot_ref.digest}"
    )
    runner.inspect_observations.append(runner.terminal(launch, result))
    runner.safety_observations.append(
        TerminalSafetyObservation.proven_for(
            launch,
            rollover_snapshot_ref=result.snapshot_ref,
            result_stable=True,
        )
    )

    assert coordinator.reconcile("run-1").action == "sealed"
    sealed = ledger.get(running.effect_id).result
    assert sealed is not None
    assert len(sealed.artifact_refs) == 1
    record = registry.read(sealed.artifact_refs[0])
    assert record.producer_coordinate == coordinate
    assert record.descriptor_digest == parse_effect_descriptor(raw).digest
    assert runtime.current.values == {"brief": {"task": "review"}}
    assert runtime.resume_calls == []

    coordinator.deliver_ready("run-1")
    delivered = runtime.resume_calls[-1][2][coordinate.interrupt_id]
    assert delivered["artifact_refs"] == list(sealed.artifact_refs)


def _acceptance_system(system, tmp_path, monkeypatch, *, tokens=("accept-token",)):
    from lockstep.runtime.artifacts import ArtifactDeclaration, ArtifactRegistry
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.effects.coordinator import EffectCoordinator
    from lockstep.runtime.effects.owner_consent import OwnerConsentAuthority
    from lockstep.runtime.leases import LeaseStore
    from lockstep.runtime.project_snapshots import ProjectSnapshotStore

    old, runtime, _runner, ledger, store, _coordinate = system
    leases = LeaseStore(store, clock=lambda: NOW)
    owner = tmp_path / "acceptance-owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    registry = ArtifactRegistry(owner, blobs, snapshots)
    producer_coordinate = NativeCoordinate(
        "thread-1", "producer-cp", "", "producer-task", "producer-int"
    )
    producer_descriptor = parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "manual",
            "logical_id": "producer",
            "runner": None,
            "inputs": {},
            "writes": ["review.md"],
            "artifacts": [
                {
                    "name": "review",
                    "source_path": "review.md",
                    "media_type": "text/markdown",
                    "required": True,
                }
            ],
            "deadline_seconds": None,
            "scope_state_keys": [],
            "result_schema": "lockstep.effect-result/v1",
        }
    )
    producer_id = derive_effect_id(producer_coordinate, producer_descriptor.digest)
    snapshot_ref = snapshots.capture(
        {"review.md": blobs.put(b"APPROVED\n")},
        declared_paths=("review.md",),
        provenance={
            "source": "managed-workspace-rollover",
            "request_digest": "f" * 64,
            "workspace_ref": "workspace:producer",
        },
    )
    artifact_ref = registry.register_set(
        public_run_id="run-1",
        project_identity="project-1",
        definition_digest="a" * 64,
        producer_effect_id=producer_id,
        producer_request_digest="f" * 64,
        workspace_ref="workspace:producer",
        producer_coordinate=producer_coordinate,
        descriptor_digest=producer_descriptor.digest,
        snapshot_ref=snapshot_ref,
        declarations=(
            ArtifactDeclaration("review", "review.md", "text/markdown", True),
        ),
    )[0]
    producer_result = parse_effect_result(
        {
            "schema": "lockstep.effect-result/v1",
            "effect_id": producer_id,
            "outcome": "PASS",
            "result_ref": "blob:" + "1" * 64,
            "artifact_refs": [str(artifact_ref)],
            "snapshot_ref": f"snapshot:{snapshot_ref.digest}",
            "diff_ref": None,
            "fixed_error_code": None,
            "evidence_refs": [],
        }
    )
    producer_lease = leases.acquire("effect", producer_id, "producer-owner", 30)
    producer = ledger.prepare(
        producer_coordinate,
        producer_descriptor,
        deadline_at=None,
        runner_binding_digest=None,
        workspace_ref=None,
        lease=producer_lease,
    )
    producer = ledger.seal(
        producer_id,
        producer_result,
        expected_revision=producer.revision,
        lease=producer_lease,
    )
    ledger.mark_delivered(
        producer_id, expected_revision=producer.revision, lease=producer_lease
    )
    leases.release(producer_lease)

    acceptance_coordinate = NativeCoordinate(
        "thread-1", "accept-cp", "", "accept-task", "accept-int"
    )
    raw_acceptance = {
        "schema": "lockstep.effect/v1",
        "kind": "accept",
        "logical_id": "accept-review",
        "artifact_handle": "call.review",
        "producer_result_state_key": "producer_result",
        "declared_name": "review",
        "destination": ".lockstep/review.md",
        "transformation": "identity",
        "audience": "local-project",
        "verdict": "PASS",
        "result_schema": "lockstep.acceptance-result/v1",
    }
    runtime.current = NativeSnapshot(
        values={"producer_result": producer_result.to_dict()},
        pending=(
            NativeInterrupt(
                acceptance_coordinate,
                {"lockstep_effect": raw_acceptance},
            ),
        ),
        checkpoint_id="accept-cp",
    )
    runtime.history_coordinates.update(
        {producer_coordinate, acceptance_coordinate}
    )
    runtime.history_values[acceptance_coordinate] = runtime.current.pending[0].value
    runtime.ancestry_pairs.add((producer_coordinate, acceptance_coordinate))
    token_values = iter(tokens)
    ref_values = count(1)
    authority = OwnerConsentAuthority(
        store,
        delegate=old._authority,
        clock=lambda: NOW,
        token_factory=lambda: next(token_values),
        consent_ref_factory=lambda: f"consent:accept-{next(ref_values)}",
    )
    coordinator = EffectCoordinator(
        runtime=runtime,
        catalog=old._catalog,
        ledger=ledger,
        leases=leases,
        runners={},
        authority=authority,
        artifacts=registry,
        clock=lambda: NOW,
        owner_factory=lambda: "acceptance-coordinator",
    )
    return (
        coordinator,
        authority,
        runtime,
        ledger,
        store,
        acceptance_coordinate,
        raw_acceptance,
        producer_result,
        artifact_ref,
        registry,
        blobs,
    )


def test_acceptance_preview_issue_and_token_redemption_are_exact_and_idempotent(
    system, tmp_path, monkeypatch
) -> None:
    (
        coordinator,
        authority,
        runtime,
        ledger,
        store,
        coordinate,
        _raw,
        _producer_result,
        _artifact_ref,
        _registry,
        _blobs,
    ) = _acceptance_system(system, tmp_path, monkeypatch)

    assert coordinator.reconcile("run-1").action == "prepared"
    preview = coordinator.preview_acceptance("run-1", coordinate)
    with store.read_connection() as connection:
        assert connection.scalar(
            __import__("sqlalchemy").select(__import__("sqlalchemy").func.count()).select_from(
                store.tables.publication_consents
            )
        ) == 0
    issued = coordinator.issue_acceptance_consent(
        "run-1", coordinate, preview.digest
    )
    assert issued.commitment_digest == preview.digest
    assert ledger.get(preview.effect_id).phase == "prepared"
    assert runtime.resume_calls == []

    status = coordinator.submit_acceptance("run-1", coordinate, issued.token)
    assert status.status == "completed"
    delivered = ledger.get(preview.effect_id)
    assert delivered.phase == "delivered"
    assert delivered.result == authority.redeem(issued.token, preview)
    assert len(runtime.resume_calls) == 1

    retry = coordinator.submit_acceptance("run-1", coordinate, issued.token)
    assert retry == status
    assert len(runtime.resume_calls) == 1


def test_acceptance_submission_serializes_redeem_through_exact_delivery(
    system, tmp_path, monkeypatch
) -> None:
    """Catches recovery consuming the sealed accept before selected delivery."""

    (
        coordinator,
        _authority,
        runtime,
        ledger,
        _store,
        coordinate,
        _raw,
        _producer_result,
        _artifact_ref,
        _registry,
        _blobs,
    ) = _acceptance_system(system, tmp_path, monkeypatch)
    assert coordinator.reconcile("run-1").action == "prepared"
    preview = coordinator.preview_acceptance("run-1", coordinate)
    issued = coordinator.issue_acceptance_consent(
        "run-1", coordinate, preview.digest
    )
    deliver_ready = coordinator.deliver_ready
    commit_acceptance = coordinator._commit_acceptance_submission
    guarded_phases = []

    def commit_inside_transaction(**kwargs):
        result = commit_acceptance(**kwargs)
        guarded_phases.append(("sealed", runtime.decision_guard_depth))
        return result

    def deliver_with_competing_recovery(run_id, interrupt_ids=None):
        guarded_phases.append(("delivery", runtime.decision_guard_depth))
        if interrupt_ids is not None and runtime.decision_guard_depth == 0:
            deliver_ready(run_id)
        return deliver_ready(run_id, interrupt_ids)

    before_entries = runtime.decision_guard_entries
    monkeypatch.setattr(
        coordinator, "_commit_acceptance_submission", commit_inside_transaction
    )
    monkeypatch.setattr(coordinator, "deliver_ready", deliver_with_competing_recovery)

    status = coordinator.submit_acceptance("run-1", coordinate, issued.token)

    assert status.status == "completed"
    assert ledger.get(preview.effect_id).phase == "delivered"
    assert len(runtime.resume_calls) == 1
    assert runtime.decision_guard_entries == before_entries + 1
    assert guarded_phases == [("sealed", 1), ("delivery", 1)]


def test_acceptance_issue_and_submit_share_invocation_then_effect_lock_order(
    system, tmp_path, monkeypatch
) -> None:
    """Catches issuance taking the effect lease before native serialization."""

    from lockstep.runtime.effects.authority import EffectAuthorityDenied

    (
        coordinator,
        _authority,
        runtime,
        ledger,
        _store,
        coordinate,
        _raw,
        _producer_result,
        _artifact_ref,
        _registry,
        _blobs,
    ) = _acceptance_system(
        system, tmp_path, monkeypatch, tokens=("accept-token", "second-token")
    )
    assert coordinator.reconcile("run-1").action == "prepared"
    preview = coordinator.preview_acceptance("run-1", coordinate)
    first = coordinator.issue_acceptance_consent(
        "run-1", coordinate, preview.digest
    )
    effect_acquired = threading.Event()
    submit_attempted = threading.Event()
    submit_entered = threading.Event()
    decision_local = threading.local()
    acquire = coordinator._acquire
    commitment_guard = runtime.commitment_guard

    @contextmanager
    def signaling_decision_guard(run_id):
        is_submit = threading.current_thread().name == "accept-submit"
        if is_submit:
            submit_attempted.set()
        with runtime._decision_lock:
            if is_submit:
                submit_entered.set()
            decision_local.depth = getattr(decision_local, "depth", 0) + 1
            try:
                yield
            finally:
                decision_local.depth -= 1

    @contextmanager
    def serialized_commitment_guard(run_id, source):
        if getattr(decision_local, "depth", 0):
            with commitment_guard(run_id, source) as guarded:
                yield guarded
            return
        with runtime._decision_lock, commitment_guard(run_id, source) as guarded:
            yield guarded

    def acquire_with_issue_barrier(effect_id):
        lease = acquire(effect_id)
        if threading.current_thread().name == "accept-issue":
            effect_acquired.set()
            assert submit_attempted.wait(2)
            if not getattr(decision_local, "depth", 0):
                assert submit_entered.wait(2)
        return lease

    monkeypatch.setattr(runtime, "decision_guard", signaling_decision_guard)
    monkeypatch.setattr(runtime, "commitment_guard", serialized_commitment_guard)
    monkeypatch.setattr(coordinator, "_acquire", acquire_with_issue_barrier)

    def issue():
        threading.current_thread().name = "accept-issue"
        return coordinator.issue_acceptance_consent(
            "run-1", coordinate, preview.digest
        )

    def submit():
        threading.current_thread().name = "accept-submit"
        return coordinator.submit_acceptance("run-1", coordinate, first.token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        issued = pool.submit(issue)
        assert effect_acquired.wait(2)
        submitted = pool.submit(submit)
        with pytest.raises(EffectAuthorityDenied, match="already issued"):
            issued.result(timeout=2)
        status = submitted.result(timeout=2)

    assert status.status == "completed"
    assert ledger.get(preview.effect_id).phase == "delivered"


def test_acceptance_receipt_commit_before_ledger_seal_retries_exactly_once(
    system, tmp_path, monkeypatch
) -> None:
    (
        coordinator,
        authority,
        runtime,
        ledger,
        _store,
        coordinate,
        _raw,
        _producer_result,
        _artifact_ref,
        _registry,
        _blobs,
    ) = _acceptance_system(system, tmp_path, monkeypatch)
    assert coordinator.reconcile("run-1").action == "prepared"
    preview = coordinator.preview_acceptance("run-1", coordinate)
    issued = coordinator.issue_acceptance_consent("run-1", coordinate, preview.digest)
    real_seal = ledger.seal
    calls = []

    def crash_once(*args, **kwargs):
        calls.append(args[0])
        if len(calls) == 1:
            raise RuntimeError("receipt committed before seal")
        return real_seal(*args, **kwargs)

    monkeypatch.setattr(ledger, "seal", crash_once)
    with pytest.raises(RuntimeError, match="receipt committed"):
        coordinator.submit_acceptance("run-1", coordinate, issued.token)
    stored = authority.inspect_token(issued.token)
    assert stored.receipt_digest is not None
    assert ledger.get(preview.effect_id).phase == "prepared"
    assert runtime.resume_calls == []

    completed = coordinator.submit_acceptance("run-1", coordinate, issued.token)
    assert completed.status == "completed"
    assert calls == [preview.effect_id, preview.effect_id]
    assert ledger.get(preview.effect_id).result == authority.redeem(
        issued.token, preview
    )


def test_token_for_another_pending_accept_never_mutates_ledger_or_native(
    system, tmp_path, monkeypatch
) -> None:
    (
        coordinator,
        _authority,
        runtime,
        ledger,
        _store,
        coordinate,
        raw,
        producer_result,
        _artifact_ref,
        _registry,
        _blobs,
    ) = _acceptance_system(system, tmp_path, monkeypatch)
    assert coordinator.reconcile("run-1").action == "prepared"
    first = coordinator.preview_acceptance("run-1", coordinate)
    issued = coordinator.issue_acceptance_consent("run-1", coordinate, first.digest)

    other_coordinate = NativeCoordinate(
        "thread-1", "other-cp", "", "other-task", "other-int"
    )
    other_raw = {
        **raw,
        "logical_id": "accept-other",
        "destination": ".lockstep/other.md",
    }
    other_descriptor = parse_effect_descriptor(other_raw)
    other_id = derive_effect_id(other_coordinate, other_descriptor.digest)
    other_lease = coordinator._leases.acquire(
        "effect", other_id, "other-owner", 30
    )
    other_record = ledger.prepare(
        other_coordinate,
        other_descriptor,
        deadline_at=None,
        runner_binding_digest=None,
        workspace_ref=None,
        lease=other_lease,
    )
    coordinator._leases.release(other_lease)
    runtime.current = NativeSnapshot(
        values={"producer_result": producer_result.to_dict()},
        pending=(NativeInterrupt(other_coordinate, {"lockstep_effect": other_raw}),),
        checkpoint_id="other-cp",
    )
    runtime.history_coordinates.add(other_coordinate)
    runtime.history_values[other_coordinate] = runtime.current.pending[0].value
    producer_coordinate = ledger.get(producer_result.effect_id).coordinate
    runtime.ancestry_pairs.add((producer_coordinate, other_coordinate))

    from lockstep.runtime.effects.authority import EffectAuthorityDenied

    with pytest.raises(EffectAuthorityDenied, match="invalid or stale"):
        coordinator.submit_acceptance("run-1", other_coordinate, issued.token)
    assert ledger.get(other_id) == other_record
    assert runtime.resume_calls == []


def test_publish_uses_existing_authority_commitment_and_project_lease_without_runner(
    system,
) -> None:
    """Catches a second authority, runner-shaped publish, or unleased mutation."""
    import inspect

    from lockstep.runtime.effects.coordinator import EffectCoordinator
    from lockstep.runtime.leases import LEASE_SCOPES

    parameters = inspect.signature(EffectCoordinator).parameters
    assert "authority" in parameters
    assert "leases" in parameters
    assert "publication" in LEASE_SCOPES
    assert "publication_authority" not in parameters
    assert "publication_leases" not in parameters
    assert "publisher" in parameters, (
        "EffectCoordinator must own the ProjectPublisher port so publication "
        "reuses its authority, commitment, effect-ledger, and delivery boundary"
    )


def _publication_system(system, tmp_path, monkeypatch):
    from lockstep.runtime.effects.coordinator import EffectCoordinator
    from lockstep.runtime.publication import ProjectPublisher

    (
        acceptance_coordinator,
        authority,
        runtime,
        ledger,
        _store,
        acceptance_coordinate,
        _raw_acceptance,
        producer_result,
        _artifact_ref,
        registry,
        blobs,
    ) = _acceptance_system(system, tmp_path, monkeypatch)
    assert acceptance_coordinator.reconcile("run-1").action == "prepared"
    preview = acceptance_coordinator.preview_acceptance(
        "run-1", acceptance_coordinate
    )
    issued = acceptance_coordinator.issue_acceptance_consent(
        "run-1", acceptance_coordinate, preview.digest
    )
    acceptance_coordinator.submit_acceptance(
        "run-1", acceptance_coordinate, issued.token
    )
    acceptance_result = ledger.get(preview.effect_id).result
    assert acceptance_result is not None

    publish_coordinate = NativeCoordinate(
        "thread-1", "publish-cp", "", "publish-task", "publish-int"
    )
    raw_publish = {
        "schema": "lockstep.effect/v1",
        "kind": "publish",
        "logical_id": "publish-review",
        "items": [
            {
                "qualified_handle": "call.review",
                "producer_result_state_key": "producer_result",
                "declared_name": "review",
                "acceptance_result_state_key": "acceptance_result",
                "destination": ".lockstep/review.md",
                "transformation": "identity",
                "audience": "local-project",
            }
        ],
        "result_schema": "lockstep.effect-result/v1",
    }
    project = tmp_path / "publication-project"
    (project / ".lockstep").mkdir(parents=True)
    publisher = ProjectPublisher(
        tmp_path / "publication-journal", project, registry, blobs
    )
    runtime.current = NativeSnapshot(
        values={
            "producer_result": producer_result.to_dict(),
            "acceptance_result": acceptance_result.to_dict(),
        },
        pending=(
            NativeInterrupt(
                publish_coordinate,
                {"lockstep_effect": raw_publish},
            ),
        ),
        checkpoint_id="publish-cp",
    )
    runtime.history_coordinates.add(publish_coordinate)
    runtime.history_values[publish_coordinate] = runtime.current.pending[0].value
    runtime.ancestry_pairs.update(
        {
            (ledger.get(producer_result.effect_id).coordinate, publish_coordinate),
            (acceptance_coordinate, publish_coordinate),
        }
    )
    coordinator = EffectCoordinator(
        runtime=runtime,
        catalog=acceptance_coordinator._catalog,
        ledger=ledger,
        leases=acceptance_coordinator._leases,
        runners={},
        authority=authority,
        artifacts=registry,
        publisher=publisher,
        clock=lambda: NOW,
        owner_factory=lambda: "publisher-owner",
    )
    return coordinator, authority, runtime, ledger, publisher, project, publish_coordinate, raw_publish


def test_revoke_inside_native_guard_before_first_publication_commit_denies_without_mutation(
    system, tmp_path, monkeypatch
) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied

    (
        coordinator,
        authority,
        runtime,
        ledger,
        publisher,
        project,
        coordinate,
        raw_publish,
    ) = _publication_system(system, tmp_path, monkeypatch)
    assert coordinator.reconcile("run-1").action == "prepared"
    assert coordinator.reconcile("run-1").action == "publication_claimed"
    effect_id = derive_effect_id(
        coordinate, parse_effect_descriptor(raw_publish).digest
    )
    runtime.commitment_callbacks.append(lambda: authority.revoke("project-1"))

    with pytest.raises(EffectAuthorityDenied, match="invalid|stale"):
        coordinator.reconcile("run-1")

    record = ledger.get(effect_id)
    prepared = publisher.prepared_for(effect_id, record.request_digest)
    assert prepared is not None and prepared[1] == "prepared"
    assert record.phase == "launching"
    assert not (project / ".lockstep/review.md").exists()


def test_applying_journal_crash_before_first_replacement_recovers_after_revoke_without_reauthorization(
    system, tmp_path, monkeypatch
) -> None:
    (
        coordinator,
        authority,
        _runtime,
        ledger,
        publisher,
        project,
        coordinate,
        raw_publish,
    ) = _publication_system(system, tmp_path, monkeypatch)
    commit_calls = []
    real_commitment = authority.commitment

    @contextmanager
    def capture_commitment(grant, request, launch):
        commit_calls.append(grant.digest)
        with real_commitment(grant, request, launch):
            yield

    monkeypatch.setattr(authority, "commitment", capture_commitment)
    assert coordinator.reconcile("run-1").action == "prepared"
    assert coordinator.reconcile("run-1").action == "publication_claimed"
    real_advance = publisher._advance_plan
    monkeypatch.setattr(
        publisher,
        "_advance_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("applying durable before first replacement")
        ),
    )
    with pytest.raises(RuntimeError, match="before first replacement"):
        coordinator.reconcile("run-1")
    effect_id = derive_effect_id(
        coordinate, parse_effect_descriptor(raw_publish).digest
    )
    record = ledger.get(effect_id)
    prepared = publisher.prepared_for(effect_id, record.request_digest)
    assert prepared is not None and prepared[1] == "applying"
    assert not (project / ".lockstep/review.md").exists()
    assert len(commit_calls) == 1

    authority.revoke("project-1")
    monkeypatch.setattr(publisher, "_advance_plan", real_advance)
    actions = [coordinator.reconcile("run-1").action for _ in range(4)]
    assert "sealed" in actions
    assert (project / ".lockstep/review.md").read_bytes() == b"APPROVED\n"
    assert len(commit_calls) == 1


def test_publication_revocation_boundary_and_apply_first_recovery(
    system, tmp_path, monkeypatch
) -> None:
    from lockstep.runtime.artifacts import ArtifactDeclaration, ArtifactRegistry
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.effects.coordinator import EffectCoordinator
    from lockstep.runtime.effects.owner_consent import (
        OwnerConsentAuthority,
        PublicationConsentCommitment,
    )
    from lockstep.runtime.leases import LeaseStore
    from lockstep.runtime.project_snapshots import ProjectSnapshotStore
    from lockstep.runtime.publication import ProjectPublisher
    import lockstep.runtime.publication as publication

    old, runtime, _runner, ledger, store, _coordinate = system
    leases = LeaseStore(store, clock=lambda: NOW)
    owner = tmp_path / "publication-owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    registry = ArtifactRegistry(owner, blobs, snapshots)

    producer_coordinate = NativeCoordinate(
        "thread-1", "producer-cp", "", "producer-task", "producer-int"
    )
    producer_descriptor = parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "manual",
            "logical_id": "producer",
            "runner": None,
            "inputs": {},
            "writes": ["review.md"],
            "artifacts": [
                {
                    "name": "review",
                    "source_path": "review.md",
                    "media_type": "text/markdown",
                    "required": True,
                }
            ],
            "deadline_seconds": None,
            "scope_state_keys": [],
            "result_schema": "lockstep.effect-result/v1",
        }
    )
    producer_id = derive_effect_id(producer_coordinate, producer_descriptor.digest)
    snapshot_ref = snapshots.capture(
        {"review.md": blobs.put(b"APPROVED\n")},
        declared_paths=("review.md",),
        provenance={
            "source": "managed-workspace-rollover",
            "request_digest": "f" * 64,
            "workspace_ref": "workspace:producer",
        },
    )
    artifact_ref = registry.register_set(
        public_run_id="run-1",
        project_identity="project-1",
        definition_digest="a" * 64,
        producer_effect_id=producer_id,
        producer_request_digest="f" * 64,
        workspace_ref="workspace:producer",
        producer_coordinate=producer_coordinate,
        descriptor_digest=producer_descriptor.digest,
        snapshot_ref=snapshot_ref,
        declarations=(
            ArtifactDeclaration("review", "review.md", "text/markdown", True),
        ),
    )[0]
    producer_result = parse_effect_result(
        {
            "schema": "lockstep.effect-result/v1",
            "effect_id": producer_id,
            "outcome": "PASS",
            "result_ref": "blob:" + "1" * 64,
            "artifact_refs": [str(artifact_ref)],
            "snapshot_ref": f"snapshot:{snapshot_ref.digest}",
            "diff_ref": None,
            "fixed_error_code": None,
            "evidence_refs": [],
        }
    )
    producer_lease = leases.acquire("effect", producer_id, "producer-owner", 30)
    producer = ledger.prepare(
        producer_coordinate,
        producer_descriptor,
        deadline_at=None,
        runner_binding_digest=None,
        workspace_ref=None,
        lease=producer_lease,
    )
    producer = ledger.seal(
        producer_id,
        producer_result,
        expected_revision=producer.revision,
        lease=producer_lease,
    )
    ledger.mark_delivered(
        producer_id, expected_revision=producer.revision, lease=producer_lease
    )
    leases.release(producer_lease)

    acceptance_coordinate = NativeCoordinate(
        "thread-1", "accept-cp", "", "accept-task", "accept-int"
    )
    acceptance_descriptor = parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "accept",
            "logical_id": "accept-review",
            "artifact_handle": "call.review",
            "producer_result_state_key": "producer_result",
            "declared_name": "review",
            "destination": ".lockstep/review.md",
            "transformation": "identity",
            "audience": "local-project",
            "verdict": "PASS",
            "result_schema": "lockstep.acceptance-result/v1",
        }
    )
    acceptance_id = derive_effect_id(
        acceptance_coordinate, acceptance_descriptor.digest
    )
    authority = OwnerConsentAuthority(
        store,
        delegate=old._authority,
        clock=lambda: NOW,
        token_factory=lambda: "publication-owner-token",
        consent_ref_factory=lambda: "consent:publication-owner",
    )
    consent_commitment = PublicationConsentCommitment.build(
        binding=old._catalog.get("run-1"),
        source=acceptance_coordinate,
        effect_id=acceptance_id,
        descriptor=acceptance_descriptor,
        producer_effect_id=producer_id,
        artifact_ref=str(artifact_ref),
        artifact_digest=registry.read(artifact_ref).blob.sha256,
    )
    issued = authority.issue(consent_commitment)
    acceptance_result = authority.redeem(
        issued.token, consent_commitment
    )
    acceptance_lease = leases.acquire(
        "effect", acceptance_id, "acceptance-owner", 30
    )
    acceptance = ledger.prepare(
        acceptance_coordinate,
        acceptance_descriptor,
        deadline_at=None,
        runner_binding_digest=None,
        workspace_ref=None,
        lease=acceptance_lease,
    )
    acceptance = ledger.seal(
        acceptance_id,
        acceptance_result,
        expected_revision=acceptance.revision,
        lease=acceptance_lease,
    )
    ledger.mark_delivered(
        acceptance_id,
        expected_revision=acceptance.revision,
        lease=acceptance_lease,
    )
    leases.release(acceptance_lease)

    publish_coordinate = NativeCoordinate(
        "thread-1", "publish-cp", "", "publish-task", "publish-int"
    )
    raw_publish = {
        "schema": "lockstep.effect/v1",
        "kind": "publish",
        "logical_id": "publish-review",
        "items": [
            {
                "qualified_handle": "call.review",
                "producer_result_state_key": "producer_result",
                "declared_name": "review",
                "acceptance_result_state_key": "acceptance_result",
                "destination": ".lockstep/review.md",
                "transformation": "identity",
                "audience": "local-project",
            }
        ],
        "result_schema": "lockstep.effect-result/v1",
    }
    project = tmp_path / "project"
    (project / ".lockstep").mkdir(parents=True)
    publisher = ProjectPublisher(owner, project, registry, blobs)
    runtime.current = NativeSnapshot(
        values={
            "producer_result": producer_result.to_dict(),
            "acceptance_result": acceptance_result.to_dict(),
        },
        pending=(
            NativeInterrupt(
                publish_coordinate,
                {"lockstep_effect": raw_publish},
            ),
        ),
        checkpoint_id="publish-cp",
    )
    runtime.history_coordinates.update(
        {producer_coordinate, acceptance_coordinate, publish_coordinate}
    )
    runtime.history_values[publish_coordinate] = runtime.current.pending[0].value
    runtime.ancestry_pairs.update(
        {
            (producer_coordinate, publish_coordinate),
            (acceptance_coordinate, publish_coordinate),
        }
    )
    coordinator = EffectCoordinator(
        runtime=runtime,
        catalog=old._catalog,
        ledger=ledger,
        leases=leases,
        runners={},
        authority=authority,
        artifacts=registry,
        publisher=publisher,
        clock=lambda: NOW,
        owner_factory=lambda: "publisher-owner",
    )

    resolved = []
    real_resolve = authority.resolve

    def capture_resolve(intent):
        resolved.append(intent)
        return real_resolve(intent)

    monkeypatch.setattr(authority, "resolve", capture_resolve)
    commit_calls = []
    real_commitment = authority.commitment

    @contextmanager
    def capture_commitment(grant, request, launch):
        commit_calls.append(grant.digest)
        with real_commitment(grant, request, launch):
            yield

    monkeypatch.setattr(authority, "commitment", capture_commitment)
    assert coordinator.reconcile("run-1").action == "prepared"
    item = dict(resolved[-1].inputs)["item-0"]
    assert item == {
        "artifact_ref": str(artifact_ref),
        "artifact_blob": {
            "sha256": registry.read(artifact_ref).blob.sha256,
            "size": registry.read(artifact_ref).blob.size,
        },
        "destination": ".lockstep/review.md",
        "transformation": "identity",
        "audience": "local-project",
        "consent_ref": acceptance_result.consent_ref,
        "approval_generation": 1,
        "receipt_digest": acceptance_result.receipt_digest,
    }
    assert coordinator.reconcile("run-1").action == "publication_claimed"
    assert not (project / ".lockstep/review.md").exists()

    monkeypatch.setattr(
        publication,
        "_after_replacement",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("apply crash")),
    )
    with pytest.raises(RuntimeError, match="apply crash"):
        for _ in range(4):
            coordinator.reconcile("run-1")
    assert (project / ".lockstep/review.md").read_bytes() == b"APPROVED\n"
    assert len(commit_calls) == 1
    authority.revoke("project-1")
    monkeypatch.setattr(publication, "_after_replacement", lambda *_args: None)

    actions = [coordinator.reconcile("run-1").action for _ in range(2)]
    assert actions[-1] == "sealed"
    assert (project / ".lockstep/review.md").read_bytes() == b"APPROVED\n"
    assert len(commit_calls) == 1
    assert coordinator.reconcile("run-1").action == "awaiting_delivery"
