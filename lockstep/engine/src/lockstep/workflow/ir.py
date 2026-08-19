"""Immutable parser IR for the Workflow DSL.

This deliberately represents structure only. Cross-block control-flow,
effect, fragment, and runtime rules are compiler responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias


@dataclass(frozen=True)
class StepIR:
    id: str | None
    step: str
    task: str
    exit: str
    writes: tuple[str, ...] = ()
    evidence: dict[str, Any] | None = None
    artifact: dict[str, Any] | None = None
    retry: dict[str, Any] | None = None
    on_failure: str | None = None
    on_error: str | None = None


@dataclass(frozen=True)
class VerifyIR:
    id: str | None
    command: str
    cwd: str | None = None
    timeout: int | None = None
    junit: dict[str, Any] | None = None
    writes: tuple[str, ...] = ()
    retry: dict[str, Any] | None = None
    on_failure: str | None = None
    on_error: str | None = None


@dataclass(frozen=True)
class DecideIR:
    id: str | None
    using: dict[str, Any]
    on_failure: str | None = None
    on_error: str | None = None


@dataclass(frozen=True)
class ChooseIR:
    id: str | None
    value: str
    cases: dict[str, tuple[BlockIR, ...]]
    default: tuple[BlockIR, ...] | None = None


@dataclass(frozen=True)
class RepeatIR:
    id: str | None
    limit: int
    until: str
    do: tuple[BlockIR, ...]
    exhausted: str


@dataclass(frozen=True)
class CallIR:
    id: str | None
    workflow: str
    runner: str
    timeout_minutes: int | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    on_failure: str | None = None
    on_error: str | None = None


@dataclass(frozen=True)
class AcceptIR:
    id: str | None
    artifact: str | None
    hash_from: str | None
    artifact_from: str | None
    verdict: Literal["PASS"]


@dataclass(frozen=True)
class ParallelIR:
    id: str | None
    join: Literal["all"]
    branches: dict[str, tuple[BlockIR, ...]]
    timeout_minutes: int | None = None
    on_failure: str | None = None
    on_error: str | None = None


@dataclass(frozen=True)
class GraphIR:
    id: str | None
    kind: Literal["inline", "include"]
    graph: dict[str, Any] | None = None
    path: str | None = None
    on: dict[str, str] | None = None


@dataclass(frozen=True)
class EscalateIR:
    id: str | None = None


BlockIR: TypeAlias = (
    StepIR | VerifyIR | DecideIR | ChooseIR | RepeatIR | CallIR | AcceptIR | ParallelIR | GraphIR | EscalateIR
)


@dataclass(frozen=True)
class WorkflowIR:
    version: Literal["1"]
    name: str
    description: str
    protect: tuple[str, ...]
    flow: tuple[BlockIR, ...]
