from __future__ import annotations

import pytest
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.observation import status_revision
from lockstep.runtime.projection import RuntimeProjection


class _Clock:
    def __init__(self, values: list[float]) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


@pytest.mark.parametrize("timeout", [True, False, 0, 61, 1.0, "1"])
def test_wait_accepts_only_integer_seconds_from_one_through_sixty(timeout) -> None:
    projection = object.__new__(RuntimeProjection)

    with pytest.raises(
        LockstepError, match="scenario wait timeout must be an integer from 1 to 60"
    ):
        projection.wait("run-1", timeout, "/project")


@pytest.mark.parametrize("timeout", [1, 60])
def test_wait_includes_an_opaque_stable_revision_when_time_expires(timeout: int) -> None:
    projection = object.__new__(RuntimeProjection)
    observed = {
        "status": "running",
        "run_id": "run-1",
        "owner": "engine",
        "next_action": "scenario_wait",
        "gate_execution": {"operation_id": "operation-1", "phase": "running"},
    }
    projection.status = lambda _run_id, _project: dict(observed)
    projection._wait_clock = _Clock([0.0, float(timeout)])
    projection._wait_sleep = lambda _seconds: pytest.fail("expired wait slept")

    result = projection.wait("run-1", timeout, "/project")

    assert result == {
        **observed,
        "changed": False,
        "revision": status_revision(observed),
    }
    assert result["revision"].startswith("revision:")
    assert len(result["revision"]) == len("revision:") + 64


def test_wait_returns_the_new_revision_when_an_observation_changes() -> None:
    projection = object.__new__(RuntimeProjection)
    before = {
        "status": "running",
        "run_id": "run-1",
        "owner": "engine",
        "next_action": "scenario_wait",
        "parallel_progress": {"pending": 2, "phases": {"running": 2}},
    }
    after = {
        "status": "awaiting",
        "run_id": "run-1",
        "owner": "worker",
        "next_action": "edit_then_scenario_done",
        "step": "accept",
    }
    observations = iter([before, after])
    projection.status = lambda _run_id, _project: dict(next(observations))
    projection._wait_clock = _Clock([0.0, 0.0])
    projection._wait_sleep = lambda seconds: None

    result = projection.wait("run-1", 30, "/project")

    assert result == {
        **after,
        "changed": True,
        "revision": status_revision(after),
    }
    assert result["revision"] != status_revision(before)


def test_wait_is_observational_and_never_calls_a_mutation_port() -> None:
    projection = object.__new__(RuntimeProjection)
    observed = {
        "status": "running",
        "run_id": "run-1",
        "owner": "engine",
        "next_action": "scenario_wait",
    }
    projection.status = lambda _run_id, _project: dict(observed)
    projection._wait_clock = _Clock([0.0, 1.0])
    projection._wait_sleep = lambda _seconds: None

    result = projection.wait("run-1", 1, "/project")

    assert result["changed"] is False
