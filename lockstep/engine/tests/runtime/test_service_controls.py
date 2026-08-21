from __future__ import annotations

import threading
from collections import deque
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from lockstep.runtime import sessions
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_descriptor,
)
from lockstep.runtime.native_models import (
    NativeCoordinate,
    NativeInterrupt,
    NativeSnapshot,
)
from lockstep.runtime.owner_state import initialize_owner_state
from lockstep.runtime.service import LockstepError, LockstepService
from lockstep.runtime.status import ScenarioStatus


def test_scenario_status_is_an_explicit_read_only_public_control() -> None:
    service = object.__new__(LockstepService)
    service.status = lambda run_id, project: {
        "status": "running",
        "run_id": run_id,
        "owner": "engine",
        "next_action": "scenario_wait",
    }
    assert service.scenario_status("run-1", "/project") == {
        "status": "running",
        "run_id": "run-1",
        "owner": "engine",
        "next_action": "scenario_wait",
    }


def test_service_composes_project_resolved_artifact_publication_and_acceptance(
    tmp_path,
) -> None:
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    service = LockstepService(tmp_path / "state", recipes)
    try:
        assert service.artifacts is service.coordinator._artifacts
        one = service.coordinator._publisher_for(
            RunBinding("run-1", "thread-1", "a" * 64, "bundle", str(first))
        )
        two = service.coordinator._publisher_for(
            RunBinding("run-2", "thread-2", "b" * 64, "bundle", str(second))
        )
        assert one.binding_digest != two.binding_digest
        assert callable(service.scenario_accept_artifact)
    finally:
        service.close()

def test_engine_effect_queue_has_a_hard_admission_ceiling() -> None:
    service = object.__new__(LockstepService)
    service._active_effect_runs = set()
    service._queued_effect_runs = set()
    service._active_effect_queue = deque()
    service._active_effect_lock = threading.Lock()
    service._pump_wakeup = threading.Event()

    for index in range(service._MAX_ACTIVE_EFFECT_RUNS):
        service._activate_effect_run(f"run-{index}")

    service._activate_effect_run("one-too-many")

    assert len(service._active_effect_runs) == service._MAX_ACTIVE_EFFECT_RUNS
    assert len(service._active_effect_queue) == service._MAX_ACTIVE_EFFECT_RUNS
    assert "one-too-many" not in service._active_effect_runs


def test_startup_recovery_discovers_native_start_commit_before_ledger_prepare() -> None:
    from lockstep.runtime.blobs import BlobRef
    from lockstep.runtime.effects.ledger import EffectDispatchWatch

    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    driven = []
    bound = []
    unbound = []
    service = object.__new__(LockstepService)
    watch = EffectDispatchWatch(
        "run-1", BlobRef("b" * 64, 2), datetime(2026, 8, 20, tzinfo=UTC)
    )
    service.effects = SimpleNamespace(
        list_dispatch_watches=lambda **_kwargs: (watch,),
        list_recovery_threads=lambda **_kwargs: (),
    )
    service.catalog = SimpleNamespace(
        get=lambda _run_id: binding,
        find_by_thread=lambda _thread_id: pytest.fail("ledger unexpectedly populated"),
    )
    service.blobs = SimpleNamespace(read=lambda _ref: b"{}")
    service.runtime = SimpleNamespace(
        bind=bound.append,
        unbind=unbound.append,
        ensure_started=lambda _run_id, _values: SimpleNamespace(),
    )
    service._active_effect_runs = set()
    service._queued_effect_runs = set()
    service._active_effect_lock = threading.Lock()
    service._admission_recovery_lock = threading.RLock()
    service._recovery_thread_cursor = None

    def drive(run_id, **_kwargs):
        driven.append(run_id)
        service._deactivate_effect_run(run_id)

    service._drive_engine_owned = drive

    service._recover_engine_effects()

    assert bound == [binding]
    assert driven == ["run-1"]
    assert unbound == ["run-1"]


def test_dispatch_recovery_serializes_with_foreground_admission() -> None:
    service = object.__new__(LockstepService)
    service._admission_recovery_lock = threading.RLock()
    entered = threading.Event()
    finished = threading.Event()
    service._recover_start_admissions = entered.set
    service._recover_effect_batch = lambda: None

    with service._admission_recovery_lock:
        worker = threading.Thread(
            target=lambda: (service._recover_engine_effects(), finished.set())
        )
        worker.start()
        assert not entered.wait(0.05)
        assert not finished.is_set()

    worker.join(timeout=1)
    assert entered.is_set()
    assert finished.is_set()


def test_worker_resume_blocks_recovery_unbind_for_the_whole_composite(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = object.__new__(LockstepService)
    service._admission_recovery_lock = threading.RLock()
    service.state_dir = tmp_path
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    interrupt = NativeInterrupt(coordinate, {"step": "work"})
    foreground_late = threading.Event()
    release = threading.Event()
    recovery_unbound = threading.Event()
    failures: list[BaseException] = []
    service._bind_existing = lambda *_args: binding
    service._worker_interrupt = lambda *_args: (binding, interrupt)

    def resume(*_args, **_kwargs):
        foreground_late.set()
        assert release.wait(1)
        return NativeSnapshot(values={"lockstep_outcome": "PASS"}, checkpoint_id="cp-2")

    service.runtime = SimpleNamespace(
        resume=resume,
        unbind=lambda _run_id: recovery_unbound.set(),
    )
    service._recover_start_admissions = lambda: service.runtime.unbind("run-1")
    service._recover_effect_batch = lambda: None
    monkeypatch.setattr(sessions, "locked_owner", lambda *_args, **_kwargs: nullcontext())

    def foreground() -> None:
        try:
            service._resume_worker(
                "run-1", "work", {"outcome": "PASS"},
                session_id="session-1", project="/project",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    foreground_thread = threading.Thread(target=foreground)
    foreground_thread.start()
    assert foreground_late.wait(1)
    recovery_thread = threading.Thread(target=service._recover_engine_effects)
    recovery_thread.start()
    assert not recovery_unbound.wait(0.05)
    release.set()
    foreground_thread.join(timeout=1)
    recovery_thread.join(timeout=1)
    assert failures == []
    assert recovery_unbound.is_set()


def test_artifact_acceptance_blocks_recovery_unbind_through_drive(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.runtime.artifacts import ArtifactRef

    service = object.__new__(LockstepService)
    service._admission_recovery_lock = threading.RLock()
    service.state_dir = tmp_path
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    raw = {
        "schema": "lockstep.effect/v1",
        "kind": "accept",
        "logical_id": "accept-review",
        "artifact_handle": "review-call.review",
        "producer_result_state_key": "review_result",
        "declared_name": "review",
        "verdict": "PASS",
        "result_schema": "lockstep.acceptance-result/v1",
    }
    snapshot = NativeSnapshot(
        values={},
        pending=(NativeInterrupt(coordinate, {"lockstep_effect": raw}),),
        checkpoint_id="cp-1",
    )
    artifact_ref = ArtifactRef("b" * 64)
    foreground_late = threading.Event()
    release = threading.Event()
    recovery_unbound = threading.Event()
    failures: list[BaseException] = []
    service._bind_existing = lambda *_args: binding
    service.artifacts = SimpleNamespace(
        read=lambda _ref: SimpleNamespace(
            public_run_id="run-1",
            project_identity="/project",
            definition_digest="a" * 64,
            blob=SimpleNamespace(sha256="c" * 64),
        )
    )
    service.coordinator = SimpleNamespace(submit_acceptance=lambda *_args: None)

    def drive(*_args, **_kwargs):
        foreground_late.set()
        assert release.wait(1)
        return ScenarioStatus("completed", "run-1", "engine", None)

    service._drive_engine_owned = drive
    service.runtime = SimpleNamespace(
        snapshot=lambda *_args, **_kwargs: snapshot,
        unbind=lambda _run_id: recovery_unbound.set(),
    )
    service._recover_start_admissions = lambda: service.runtime.unbind("run-1")
    service._recover_effect_batch = lambda: None
    monkeypatch.setattr(sessions, "locked_owner", lambda *_args, **_kwargs: nullcontext())

    def foreground() -> None:
        try:
            service.scenario_accept_artifact(
                "run-1", "accept-review", str(artifact_ref), "consent-1", 1,
                session_id="session-1", project="/project",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    foreground_thread = threading.Thread(target=foreground)
    foreground_thread.start()
    assert foreground_late.wait(1)
    recovery_thread = threading.Thread(target=service._recover_engine_effects)
    recovery_thread.start()
    assert not recovery_unbound.wait(0.05)
    release.set()
    foreground_thread.join(timeout=1)
    recovery_thread.join(timeout=1)
    assert failures == []
    assert recovery_unbound.is_set()


def test_start_recovery_defers_before_native_commit_when_active_batch_is_full() -> None:
    from lockstep.runtime.blobs import BlobRef
    from lockstep.runtime.effects.ledger import EffectDispatchWatch

    binding = RunBinding("deferred", "thread-deferred", "a" * 64, "bundle", "/p")
    watch = EffectDispatchWatch(
        "deferred", BlobRef("b" * 64, 2), datetime(2026, 8, 20, tzinfo=UTC)
    )
    service = object.__new__(LockstepService)
    service.effects = SimpleNamespace(list_dispatch_watches=lambda **_kwargs: (watch,))
    service.catalog = SimpleNamespace(get=lambda _run_id: binding)
    service.blobs = SimpleNamespace(
        read=lambda _ref: pytest.fail("capacity rejection consumed start input")
    )
    service.runtime = SimpleNamespace(
        bind=lambda _binding: pytest.fail("capacity rejection bound native app"),
        ensure_started=lambda *_args: pytest.fail("capacity rejection invoked native"),
    )
    service._active_effect_runs = {
        f"run-{index}" for index in range(service._MAX_ACTIVE_EFFECT_RUNS)
    }
    service._queued_effect_runs = set(service._active_effect_runs)
    service._active_effect_lock = threading.Lock()
    service._pump_wakeup = threading.Event()

    service._recover_start_admissions()

    assert "deferred" not in service._active_effect_runs
    assert not service._pump_wakeup.is_set()


def test_effect_recovery_defers_before_reconcile_when_active_batch_is_full() -> None:
    coordinate = NativeCoordinate("thread-pinned", "cp-1", "", "task-1", "int-1")
    interrupt = NativeInterrupt(
        coordinate,
        {
            "lockstep_effect": {
                "schema": "lockstep.effect/v1",
                "kind": "pinned",
                "logical_id": "tests",
                "runner": {
                    "selector": "pinned",
                    "required_capabilities": [
                        "workspace",
                        "bounded_result",
                        "sandbox",
                    ],
                },
                "inputs": {
                    "command": {"state_key": "command"},
                    "snapshot": {"state_key": "snapshot"},
                },
                "writes": [],
                "artifacts": [],
                "deadline_seconds": 60,
                "scope_state_keys": [],
                "result_schema": "lockstep.effect-result/v1",
            }
        },
    )
    snapshot = NativeSnapshot(values={}, pending=(interrupt,), checkpoint_id="cp-1")
    binding = RunBinding("run-pinned", "thread-pinned", "a" * 64, "bundle", "/project")
    service = object.__new__(LockstepService)
    service.effects = SimpleNamespace(
        get=lambda _effect_id: (_ for _ in ()).throw(KeyError())
    )
    service.leases = ()
    service.runtime = SimpleNamespace(snapshot=lambda *_args, **_kwargs: snapshot)
    service.coordinator = SimpleNamespace(
        reconcile=lambda _run_id: pytest.fail("capacity deferral reconciled effect")
    )
    service._active_effect_runs = {
        f"run-{index}" for index in range(service._MAX_ACTIVE_EFFECT_RUNS)
    }
    service._active_effect_lock = threading.Lock()

    status = service._drive_engine_owned(
        "run-pinned", binding=binding, snapshot=snapshot
    )

    assert status.status == "running"
    assert "run-pinned" not in service._active_effect_runs


def test_effect_recovery_cursor_does_not_skip_a_capacity_deferred_run() -> None:
    binding = RunBinding("deferred", "thread-deferred", "a" * 64, "bundle", "/p")
    service = object.__new__(LockstepService)
    service.effects = SimpleNamespace(
        list_recovery_threads=lambda **_kwargs: ("thread-deferred",)
    )
    service.catalog = SimpleNamespace(find_by_thread=lambda _thread_id: binding)
    service.runtime = SimpleNamespace(
        bind=lambda _binding: pytest.fail("capacity-deferred effect was bound")
    )
    service._active_effect_runs = {
        f"run-{index}" for index in range(service._MAX_ACTIVE_EFFECT_RUNS)
    }
    service._active_effect_lock = threading.Lock()
    service._recovery_thread_cursor = None

    service._recover_effect_batch()

    assert service._recovery_thread_cursor is None
    assert "deferred" not in service._active_effect_runs


@pytest.mark.parametrize("timeout", [0, 61, True, "1"])
def test_scenario_wait_rejects_out_of_contract_timeout_without_polling(timeout) -> None:
    service = object.__new__(LockstepService)
    service.status = lambda *_args: pytest.fail("invalid wait polled status")

    with pytest.raises(LockstepError, match="1.*60|timeout"):
        service.scenario_wait("run-1", timeout, "/project")


def test_scenario_wait_reports_change_without_mutating_progress_ports() -> None:
    service = object.__new__(LockstepService)
    observations = iter(
        (
            {
                "status": "running",
                "run_id": "run-1",
                "owner": "engine",
                "next_action": "scenario_wait",
            },
            {
                "status": "completed",
                "run_id": "run-1",
                "owner": "engine",
                "next_action": None,
            },
        )
    )
    service.status = lambda *_args: next(observations)
    ticks = iter((0.0, 0.0, 0.1))
    service._wait_clock = lambda: next(ticks)
    service._wait_sleep = lambda _delay: None
    service.runtime = type(
        "NoMutationRuntime",
        (),
        {"resume": lambda *_args: pytest.fail("wait mutated checkpoint")},
    )()
    service.coordinator = type(
        "NoReconcile",
        (),
        {"reconcile": lambda *_args: pytest.fail("wait started reconciliation")},
    )()

    result = service.scenario_wait("run-1", 1, "/project")

    assert result["changed"] is True
    assert result["status"] == "completed"
    assert result["revision"].startswith("revision:")


def test_service_exposes_no_status_mutation_api() -> None:
    forbidden = {
        "set_status",
        "update_status",
        "mark_completed",
        "mark_escalated",
        "mark_aborted",
    }
    assert forbidden.isdisjoint(vars(LockstepService))


def test_protected_manual_step_uses_descriptor_logical_id() -> None:
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    raw = {
        "schema": "lockstep.effect/v1",
        "kind": "manual",
        "logical_id": "edit",
        "runner": None,
        "inputs": {},
        "writes": ["src/"],
        "artifacts": [],
        "deadline_seconds": None,
        "scope_state_keys": [],
        "result_schema": "lockstep.effect-result/v1",
    }
    interrupt = NativeInterrupt(coordinate, {"lockstep_effect": raw})
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    service = object.__new__(LockstepService)
    service._snapshot_status = lambda *_args: (
        binding,
        ScenarioStatus("awaiting", "run-1", "worker", "edit_then_scenario_done"),
    )
    service.runtime = SimpleNamespace(
        snapshot=lambda *_args, **_kwargs: NativeSnapshot(
            values={}, pending=(interrupt,), checkpoint_id="cp-1"
        )
    )

    assert service._worker_interrupt("run-1", "edit", "/project") == (
        binding,
        interrupt,
    )


def test_engine_progress_prepares_manual_handoff_before_returning_awaiting() -> None:
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    raw = {
        "schema": "lockstep.effect/v1",
        "kind": "manual",
        "logical_id": "edit",
        "runner": None,
        "inputs": {},
        "writes": ["src/"],
        "artifacts": [],
        "deadline_seconds": None,
        "scope_state_keys": [],
        "result_schema": "lockstep.effect-result/v1",
    }
    descriptor = parse_effect_descriptor(raw)
    interrupt = NativeInterrupt(coordinate, {"lockstep_effect": raw})
    snapshot = NativeSnapshot(values={}, pending=(interrupt,), checkpoint_id="cp-1")
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    effect_id = derive_effect_id(coordinate, descriptor.digest)

    class Effects:
        record = None

        def get(self, requested):
            assert requested == effect_id
            if self.record is None:
                raise KeyError(requested)
            return self.record

    effects = Effects()

    class Coordinator:
        calls = 0

        def reconcile_pending(self, run_id):
            assert run_id == "run-1"
            self.calls += 1
            effects.record = SimpleNamespace(
                coordinate=coordinate,
                descriptor_digest=descriptor.digest,
                effect_kind="manual",
                phase="prepared",
            )
            return (SimpleNamespace(action="prepared"),)

    service = object.__new__(LockstepService)
    service.effects = effects
    service.leases = ()
    service.coordinator = Coordinator()
    service.runtime = SimpleNamespace(snapshot=lambda *_args, **_kwargs: snapshot)
    service._deactivate_effect_run = lambda _run_id: None
    service._ack_start_if_observable = lambda *_args: None

    status = service._drive_engine_owned("run-1", binding=binding, snapshot=snapshot)

    assert status.status == "awaiting"
    assert status.owner == "worker"
    assert service.coordinator.calls == 1


def test_engine_progress_delivers_scope_result_without_status_mutation() -> None:
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    interrupt = NativeInterrupt(
        coordinate,
        {
            "lockstep_effect": {
                "schema": "lockstep.effect/v1",
                "kind": "scope",
                "logical_id": "child-scope",
                "scope_kind": "call",
                "duration_seconds": 60,
                "runner_selector": "codex",
                "ancestor_deadline_state_keys": [],
                "result_state_key": "child_scope_result",
                "result_schema": "lockstep.scope-result/v1",
            }
        },
    )
    pending = NativeSnapshot(values={}, pending=(interrupt,), checkpoint_id="cp-1")
    completed = NativeSnapshot(
        values={"lockstep_outcome": "PASS"}, checkpoint_id="cp-2"
    )
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    state = {"snapshot": pending}

    class Coordinator:
        def __init__(self):
            self.actions = iter(("sealed", "awaiting_delivery"))
            self.deliveries = 0

        def reconcile_pending(self, _run_id):
            return (SimpleNamespace(action=next(self.actions)),)

        def deliver_ready(self, _run_id):
            self.deliveries += 1
            state["snapshot"] = completed

        def reconcile_consumed(self, _run_id):
            return ()

    service = object.__new__(LockstepService)
    service.effects = ()
    service.leases = ()
    service.coordinator = Coordinator()
    service.runtime = SimpleNamespace(
        snapshot=lambda *_args, **_kwargs: state["snapshot"]
    )
    service._deactivate_effect_run = lambda _run_id: None
    service._ack_start_if_observable = lambda *_args: None

    status = service._drive_engine_owned("run-1", binding=binding, snapshot=pending)

    assert status.status == "completed"
    assert service.coordinator.deliveries == 1


def test_engine_progress_requeues_a_delivery_held_by_another_owner() -> None:
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    interrupt = NativeInterrupt(
        coordinate,
        {
            "lockstep_effect": {
                "schema": "lockstep.effect/v1",
                "kind": "scope",
                "logical_id": "child-scope",
                "scope_kind": "call",
                "duration_seconds": 60,
                "runner_selector": "codex",
                "ancestor_deadline_state_keys": [],
                "result_state_key": "child_scope_result",
                "result_schema": "lockstep.scope-result/v1",
            }
        },
    )
    pending = NativeSnapshot(values={}, pending=(interrupt,), checkpoint_id="cp-1")
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")

    class Coordinator:
        calls = 0

        def reconcile_pending(self, _run_id):
            self.calls += 1
            return (SimpleNamespace(action="awaiting_delivery"),)

        def deliver_ready(self, _run_id):
            return None

    activated = []
    service = object.__new__(LockstepService)
    service.effects = ()
    service.leases = ()
    service.coordinator = Coordinator()
    service.runtime = SimpleNamespace(snapshot=lambda *_args, **_kwargs: pending)
    service._activate_effect_run = activated.append

    status = service._drive_engine_owned("run-1", binding=binding, snapshot=pending)

    assert status.status == "running"
    assert service.coordinator.calls == 1
    assert activated == ["run-1"]


def test_engine_progress_recovers_capacity_bound_consumed_facts_in_one_sweep() -> None:
    """Cleanup capacity is independent of the ordinary progress decision budget."""
    completed = NativeSnapshot(
        values={"lockstep_outcome": "PASS"}, checkpoint_id="cp-2"
    )
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")

    class Coordinator:
        def __init__(self):
            self.calls = 0

        def reconcile_consumed(self, _run_id):
            self.calls += 1
            return tuple(
                SimpleNamespace(action="delivered") for _index in range(128)
            )

    coordinator = Coordinator()
    deactivated = []
    service = object.__new__(LockstepService)
    service.effects = ()
    service.leases = ()
    service.coordinator = coordinator
    service.runtime = SimpleNamespace(
        snapshot=lambda *_args, **_kwargs: completed
    )
    service._deactivate_effect_run = deactivated.append
    service._ack_start_if_observable = lambda *_args: None

    status = service._drive_engine_owned(
        "run-1", binding=binding, snapshot=completed
    )

    assert status.status == "completed"
    assert coordinator.calls == 1
    assert deactivated == ["run-1"]


def test_protected_manual_done_uses_coordinator_not_direct_native_resume(
    tmp_path,
) -> None:
    state = initialize_owner_state(tmp_path / "state")
    sessions.touch(state, "run-1", "session-1", 30)
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    interrupt = NativeInterrupt(
        coordinate,
        {
            "lockstep_effect": {
                "schema": "lockstep.effect/v1",
                "kind": "manual",
                "logical_id": "edit",
                "runner": None,
                "inputs": {},
                "writes": ["src/"],
                "artifacts": [],
                "deadline_seconds": None,
                "scope_state_keys": [],
                "result_schema": "lockstep.effect-result/v1",
            },
        },
    )
    binding = RunBinding(
        "run-1", "thread-1", "a" * 64, "bundle:" + "b" * 64, str(tmp_path)
    )

    class Coordinator:
        def __init__(self):
            self.calls = []

        def submit_manual(self, run_id, source, submission):
            self.calls.append((run_id, source, submission.kind))
            return ScenarioStatus("completed", run_id, "engine", None)

    class Runtime:
        def resume(self, *_args, **_kwargs):
            raise AssertionError("protected manual result bypassed the coordinator")

    class Leases:
        def __init__(self):
            self.calls = []

        def acquire(self, scope, key, owner, ttl):
            self.calls.append((scope, key, owner, ttl))
            return object()

        def release(self, _lease):
            return None

    service = object.__new__(LockstepService)
    service.state_dir = state
    service.runtime = Runtime()
    service.coordinator = Coordinator()
    service.leases = Leases()
    service._bind_existing = lambda *_args: binding
    service._worker_interrupt = lambda *_args: (binding, interrupt)
    service._drive_engine_owned = lambda *_args, **_kwargs: ScenarioStatus(
        "completed", "run-1", "engine", None
    )
    service._admission_recovery_lock = threading.RLock()
    service._closed = False

    completed = service.scenario_done(
        "run-1",
        "edit",
        {"reviewed": True},
        session_id="session-1",
        project=str(tmp_path),
    )

    assert completed["status"] == "completed"
    assert service.coordinator.calls == [("run-1", coordinate, "done")]
