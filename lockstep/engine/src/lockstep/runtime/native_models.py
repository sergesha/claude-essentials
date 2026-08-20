"""Native-runtime-neutral values exposed outside the yamlgraph adapter."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lockstep.recipe.authority import AuthorizedMaterialization


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
    task_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class NativeEvent:
    mode: str
    data: Any


class NativeAppPort(Protocol):
    def invoke(self, values: dict, *, thread_id: str) -> NativeSnapshot: ...

    def resume(
        self, *, thread_id: str, results_by_interrupt_id: Mapping[str, Any]
    ) -> NativeSnapshot: ...

    def stream(
        self, values_or_command: object, *, thread_id: str
    ) -> Iterable[NativeEvent]: ...

    def snapshot(self, *, thread_id: str, subgraphs: bool = False) -> NativeSnapshot: ...

    def history(self, *, thread_id: str) -> Iterable[NativeSnapshot]: ...

    def close(self) -> None: ...


class NativeAppFactory(Protocol):
    def __call__(
        self, recipe: AuthorizedMaterialization, db_path: Path | None = None
    ) -> NativeAppPort: ...
