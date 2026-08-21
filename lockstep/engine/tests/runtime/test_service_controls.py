from __future__ import annotations

import pytest

from lockstep.runtime import sessions
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.native_models import NativeCoordinate, NativeInterrupt
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


def test_service_exposes_no_status_mutation_api() -> None:
    forbidden = {
        "set_status",
        "update_status",
        "mark_completed",
        "mark_escalated",
        "mark_aborted",
    }
    assert forbidden.isdisjoint(vars(LockstepService))


def test_protected_manual_done_uses_coordinator_not_direct_native_resume(
    tmp_path,
) -> None:
    state = initialize_owner_state(tmp_path / "state")
    sessions.touch(state, "run-1", "session-1", 30)
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    interrupt = NativeInterrupt(
        coordinate,
        {
            "step": "edit",
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
        calls = []

        def submit_manual(self, run_id, source, submission):
            self.calls.append((run_id, source, submission.kind))
            return ScenarioStatus("completed", run_id, "engine", None)

    class Runtime:
        def resume(self, *_args, **_kwargs):
            raise AssertionError("protected manual result bypassed the coordinator")

    service = object.__new__(LockstepService)
    service.state_dir = state
    service.runtime = Runtime()
    service.coordinator = Coordinator()
    service._worker_interrupt = lambda *_args: (binding, interrupt)
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
