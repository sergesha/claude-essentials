from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace

import pytest

from lockstep.runtime.service import LockstepService


def _events_module():
    return import_module("lockstep.runtime.events")


def test_runtime_event_has_the_closed_redacted_observation_shape() -> None:
    events = _events_module()
    event = events.RuntimeEvent(
        event_id="event-1",
        event_type="status.observed",
        run_id="run-1",
        aggregate_kind="run",
        aggregate_id="run-1",
        revision="revision:" + "a" * 64,
        ordinal=0,
        payload={"status": "running", "owner": "engine"},
        occurred_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    )

    assert asdict(event) == {
        "event_id": "event-1",
        "event_type": "status.observed",
        "run_id": "run-1",
        "aggregate_kind": "run",
        "aggregate_id": "run-1",
        "revision": "revision:" + "a" * 64,
        "ordinal": 0,
        "payload": {"status": "running", "owner": "engine"},
        "occurred_at": datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    }


def test_events_merge_native_and_effect_observations_without_invoking_graph() -> None:
    service = object.__new__(LockstepService)
    forbidden_calls = []

    def forbidden(name: str):
        def fail(*_args, **_kwargs):
            forbidden_calls.append(name)
            pytest.fail(f"events called authority-bearing port {name}")

        return fail

    service.runtime = SimpleNamespace(
        history=lambda _run_id: (
            SimpleNamespace(
                checkpoint_id="cp-1",
                checkpoint_ns="",
                created_at="2026-08-21T12:00:00+00:00",
                values={},
                pending=(),
                next=("verify",),
                task_errors=(),
            ),
        ),
        start=forbidden("runtime.start"),
        ensure_started=forbidden("runtime.ensure_started"),
        resume=forbidden("runtime.resume"),
        stream=forbidden("runtime.stream"),
    )
    service.effects = SimpleNamespace(
        list_for_thread=lambda _thread_id: (
            SimpleNamespace(
                effect_id="effect-1",
                effect_kind="verify",
                phase="sealed",
                updated_at=datetime(2026, 8, 21, 12, 0, 1, tzinfo=UTC),
            ),
        ),
        prepare=forbidden("effects.prepare"),
        seal=forbidden("effects.seal"),
    )
    service.catalog = SimpleNamespace(
        get=lambda _run_id: SimpleNamespace(
            public_run_id="run-1", thread_id="thread-1", project_identity="/project"
        )
    )
    service.coordinator = SimpleNamespace(
        reconcile_pending=forbidden("coordinator.reconcile_pending")
    )

    result = service.scenario_events("run-1", "/project")

    assert [item["source"] for item in result] == ["native", "effect"]
    assert result[0]["checkpoint_id"] == "cp-1"
    assert result[1]["effect_id"] == "effect-1"
    assert forbidden_calls == []


@pytest.mark.parametrize("mode", ["reject", "raise-before", "accept-then-raise"])
def test_event_delivery_failure_is_non_authoritative(mode: str) -> None:
    events = _events_module()
    committed = {"status": "completed", "revision": 7}

    class Sink:
        def offer(self, _event):
            if mode == "reject":
                return events.EventDelivery(accepted=False, reason_code="sink_rejected")
            if mode == "accept-then-raise":
                self.accepted = True
            raise RuntimeError("sink unavailable")

    warnings = []
    dispatcher = events.EventDispatcher(Sink(), warnings.append)
    event = events.RuntimeEvent(
        event_id="event-1",
        event_type="run.terminal",
        run_id="run-1",
        aggregate_kind="run",
        aggregate_id="run-1",
        revision="revision:" + "b" * 64,
        ordinal=0,
        payload={"status": "completed"},
        occurred_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    )

    dispatcher.offer(event)

    assert committed == {"status": "completed", "revision": 7}
    assert len(warnings) == 1
    assert warnings[0].event_id == "event-1"
