from __future__ import annotations

import pytest

from lockstep.runtime.service import LockstepError, LockstepService


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

