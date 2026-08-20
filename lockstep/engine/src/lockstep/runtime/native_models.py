"""Native-runtime-neutral values exposed outside the yamlgraph adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NativeCoordinate:
    thread_id: str
    checkpoint_id: str
    checkpoint_ns: str
    task_id: str
    interrupt_id: str


@dataclass(frozen=True)
class NativeInterrupt:
    coordinate: NativeCoordinate
    value: Any


@dataclass(frozen=True)
class NativeSnapshot:
    values: dict[str, Any]
    pending: tuple[NativeInterrupt, ...] = ()
    next: tuple[str, ...] = ()
    checkpoint_id: str = ""
    checkpoint_ns: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None


@dataclass(frozen=True)
class NativeEvent:
    mode: str
    data: Any
