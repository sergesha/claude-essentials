"""Closed best-effort runtime observations.

Event delivery is intentionally non-authoritative: a sink rejection or crash
can only emit a warning after the underlying runtime fact already exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class RuntimeEvent:
    event_id: str
    event_type: str
    run_id: str
    aggregate_kind: str
    aggregate_id: str
    revision: str
    ordinal: int
    payload: Mapping[str, Any]
    occurred_at: datetime


@dataclass(frozen=True)
class EventDelivery:
    accepted: bool
    reason_code: str | None = None


@dataclass(frozen=True)
class EventDeliveryWarning:
    event_id: str
    reason_code: str


class EventSink(Protocol):
    def offer(self, event: RuntimeEvent) -> EventDelivery: ...


class EventDispatcher:
    def __init__(self, sink: EventSink, warn) -> None:
        self._sink = sink
        self._warn = warn

    def offer(self, event: RuntimeEvent) -> None:
        reason = "sink_failed"
        try:
            delivery = self._sink.offer(event)
            if not isinstance(delivery, EventDelivery):
                reason = "sink_contract_invalid"
            elif delivery.accepted:
                return
            else:
                reason = delivery.reason_code or "sink_rejected"
        except Exception:  # noqa: BLE001 - observations never control runtime facts
            reason = "sink_unavailable"
        self._warn(EventDeliveryWarning(event.event_id, reason))
