from __future__ import annotations

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

        def reconcile(self, run_id):
            assert run_id == "run-1"
            self.calls += 1
            effects.record = SimpleNamespace(
                coordinate=coordinate,
                descriptor_digest=descriptor.digest,
                effect_kind="manual",
                phase="prepared",
            )
            return SimpleNamespace(action="prepared")

    service = object.__new__(LockstepService)
    service.effects = effects
    service.leases = ()
    service.coordinator = Coordinator()
    service.runtime = SimpleNamespace(snapshot=lambda *_args, **_kwargs: snapshot)

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

        def reconcile(self, _run_id):
            return SimpleNamespace(action=next(self.actions))

        def deliver_ready(self, _run_id):
            self.deliveries += 1
            state["snapshot"] = completed

    service = object.__new__(LockstepService)
    service.effects = ()
    service.leases = ()
    service.coordinator = Coordinator()
    service.runtime = SimpleNamespace(
        snapshot=lambda *_args, **_kwargs: state["snapshot"]
    )

    status = service._drive_engine_owned("run-1", binding=binding, snapshot=pending)

    assert status.status == "completed"
    assert service.coordinator.deliveries == 1


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
