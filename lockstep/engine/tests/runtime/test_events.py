from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace

import pytest

from lockstep.runtime.effects.ledger import EffectPhase
from lockstep.runtime.observation import project_events


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
    native = (
        SimpleNamespace(
            checkpoint_id="cp-1",
            checkpoint_ns="",
            created_at="2026-08-21T12:00:00+00:00",
            values={},
            pending=(),
            next=("verify",),
            task_errors=(),
        ),
    )
    effects = (
        SimpleNamespace(
            effect_id="effect-1",
            effect_kind="verify",
            phase=EffectPhase.SEALED,
            updated_at=datetime(2026, 8, 21, 12, 0, 1, tzinfo=UTC),
        ),
    )

    result = project_events(native, effects, limit=10_000)

    assert [item["source"] for item in result] == ["native", "effect"]
    assert result[0]["checkpoint_id"] == "cp-1"
    assert result[1]["effect_id"] == "effect-1"
    public_phase = result[1]["phase"]
    assert type(public_phase) is str
    assert (
        json.dumps(
            {"phase": public_phase}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        == b'{"phase":"sealed"}'
    )


def test_events_whitelist_fields_and_never_expose_state_or_effect_results() -> None:
    secret = "owner-secret-must-not-cross-observation-boundary"
    native = (
        SimpleNamespace(
            checkpoint_id="cp-1",
            checkpoint_ns="",
            created_at="2026-08-21T12:00:00+00:00",
            values={"prompt": secret, "token": secret},
            pending=(SimpleNamespace(value={"brief": secret}),),
            next=("verify",),
            task_errors=(RuntimeError(secret),),
        ),
    )
    effects = (
        SimpleNamespace(
            effect_id="effect-1",
            effect_kind="verify",
            phase="sealed",
            updated_at=datetime(2026, 8, 21, 12, 0, 1, tzinfo=UTC),
            result={"stdout": secret},
            request_digest=secret,
            fixed_error_code="manifest_invalid",
        ),
    )

    result = project_events(native, effects, limit=10_000)

    assert secret not in repr(result)
    assert result[1]["fixed_error_code"] == "manifest_invalid"
    assert set(result[0]) == {
        "source", "checkpoint_id", "checkpoint_ns", "created_at", "next",
        "pending_count", "error_count",
    }
    assert set(result[1]) == {
        "source", "effect_id", "effect_kind", "phase", "updated_at", "fixed_error_code",
    }


def test_events_fail_closed_when_observation_count_exceeds_public_bound() -> None:
    item = SimpleNamespace(
        checkpoint_id="cp", checkpoint_ns="", created_at=None, values={},
        pending=(), next=(), task_errors=(),
    )

    with pytest.raises(Exception, match="event observations exceed"):
        project_events((item,) * 10_001, (), limit=10_000)


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
