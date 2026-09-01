"""Native-runtime-neutral values exposed outside the yamlgraph adapter."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

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
    ancestor_checkpoints: tuple[tuple[str, str], ...] = ()
    # Canonical values from the exact checkpoint namespace which owns this
    # interrupt.  Nested direct subgraphs remain isolated; consumers must not
    # resolve protected selectors against an unrelated root snapshot.
    state_values: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class NativeInterruptOccurrence:
    """One exact interrupt occurrence projected from public native history."""

    coordinate: NativeCoordinate
    value: Any


@dataclass(frozen=True)
class NativeLineageProof:
    disposition: Literal["pending", "descended"]
    occurrence: NativeInterruptOccurrence


class NativeHistoryLimitExceeded(RuntimeError):
    """A bounded public native-history projection exceeded its work limit."""


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

    async def ainvoke(self, values: dict, *, thread_id: str) -> NativeSnapshot: ...

    def resume(
        self, *, thread_id: str, results_by_interrupt_id: Mapping[str, Any]
    ) -> NativeSnapshot: ...

    async def aresume(
        self, *, thread_id: str, results_by_interrupt_id: Mapping[str, Any]
    ) -> NativeSnapshot: ...

    def stream(
        self, values_or_command: object, *, thread_id: str
    ) -> Iterable[NativeEvent]: ...

    def snapshot(
        self, *, thread_id: str, subgraphs: bool = False
    ) -> NativeSnapshot: ...

    def history(self, *, thread_id: str) -> Iterable[NativeSnapshot]: ...

    def interrupt_history(
        self, *, thread_id: str, checkpoint_ns: str, snapshot_limit: int
    ) -> Iterable[NativeInterruptOccurrence]: ...

    def checkpoint_is_ancestor(
        self,
        *,
        thread_id: str,
        ancestor_checkpoint_ns: str,
        ancestor_checkpoint_id: str,
        descendant_checkpoint_ns: str,
        descendant_checkpoint_id: str,
        snapshot_limit: int,
    ) -> bool: ...

    def close(self) -> None: ...


class NativeAppFactory(Protocol):
    def __call__(
        self, recipe: AuthorizedMaterialization, db_path: Path | None = None
    ) -> NativeAppPort: ...
