from __future__ import annotations

from types import SimpleNamespace

import pytest

from lockstep.runtime.service import LockstepError, LockstepService


class _Clock:
    def __init__(self, values: list[float]) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


@pytest.mark.parametrize("timeout", [True, False, 0, 61, 1.0, "1"])
def test_wait_accepts_only_integer_seconds_from_one_through_sixty(timeout) -> None:
    service = object.__new__(LockstepService)

    with pytest.raises(
        LockstepError, match="scenario wait timeout must be an integer from 1 to 60"
    ):
        service.scenario_wait("run-1", timeout, "/project")


@pytest.mark.parametrize("timeout", [1, 60])
def test_wait_includes_an_opaque_stable_revision_when_time_expires(timeout: int) -> None:
    service = object.__new__(LockstepService)
    observed = {
        "status": "running",
        "run_id": "run-1",
        "owner": "engine",
        "next_action": "scenario_wait",
        "gate_execution": {"operation_id": "operation-1", "phase": "running"},
    }
    service.scenario_status = lambda _run_id, _project: dict(observed)
    service._wait_clock = _Clock([0.0, float(timeout)])
    service._wait_sleep = lambda _seconds: pytest.fail("expired wait slept")

    result = service.scenario_wait("run-1", timeout, "/project")

    assert result == {
        **observed,
        "changed": False,
        "revision": LockstepService._status_revision(observed),
    }
    assert result["revision"].startswith("revision:")
    assert len(result["revision"]) == len("revision:") + 64


def test_wait_returns_the_new_revision_when_an_observation_changes() -> None:
    service = object.__new__(LockstepService)
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
    service.scenario_status = lambda _run_id, _project: dict(next(observations))
    service._wait_clock = _Clock([0.0, 0.0])
    service._wait_sleep = lambda seconds: None

    result = service.scenario_wait("run-1", 30, "/project")

    assert result == {
        **after,
        "changed": True,
        "revision": LockstepService._status_revision(after),
    }
    assert result["revision"] != LockstepService._status_revision(before)


def test_wait_is_observational_and_never_calls_a_mutation_port() -> None:
    service = object.__new__(LockstepService)
    observed = {
        "status": "running",
        "run_id": "run-1",
        "owner": "engine",
        "next_action": "scenario_wait",
    }
    calls = []

    def forbidden(name: str):
        def fail(*_args, **_kwargs):
            calls.append(name)
            pytest.fail(f"wait called mutation port {name}")

        return fail

    service.scenario_status = lambda _run_id, _project: dict(observed)
    service._wait_clock = _Clock([0.0, 1.0])
    service._wait_sleep = lambda _seconds: None
    service.runtime = SimpleNamespace(
        start=forbidden("runtime.start"),
        ensure_started=forbidden("runtime.ensure_started"),
        resume=forbidden("runtime.resume"),
        stream=forbidden("runtime.stream"),
        bind=forbidden("runtime.bind"),
    )
    service.coordinator = SimpleNamespace(
        reconcile_pending=forbidden("coordinator.reconcile_pending"),
        reconcile_consumed=forbidden("coordinator.reconcile_consumed"),
        submit_acceptance=forbidden("coordinator.submit_acceptance"),
    )
    service.catalog = SimpleNamespace(create=forbidden("catalog.create"))
    service.effects = SimpleNamespace(
        prepare=forbidden("effects.prepare"),
        seal=forbidden("effects.seal"),
    )
    service.manual = SimpleNamespace(submit=forbidden("manual.submit"))
    service.artifacts = SimpleNamespace(publish=forbidden("artifacts.publish"))

    result = service.scenario_wait("run-1", 1, "/project")

    assert result["changed"] is False
    assert calls == []
