"""Immutable parser IR for the Workflow DSL.

This deliberately represents structure only. Cross-block control-flow,
effect, fragment, and runtime rules are compiler responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping, TypeAlias


FrozenMapping: TypeAlias = Mapping[str, Any]


def freeze(value: Any) -> Any:
    """Recursively make parser-owned structured values safe to share."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class RetryIR:
    limit: int
    exhausted: str | None


@dataclass(frozen=True)
class WorkflowDefaultsIR:
    retry: RetryIR | None = None


@dataclass(frozen=True)
class StepIR:
    id: str | None
    step: str
    task: str
    exit: str
    writes: tuple[str, ...] = ()
    evidence: FrozenMapping | None = None
    artifact: FrozenMapping | None = None
    retry: RetryIR | None = None
    on_failure: str | None = None
    on_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", freeze(self.evidence) if self.evidence is not None else None)
        object.__setattr__(self, "artifact", freeze(self.artifact) if self.artifact is not None else None)


@dataclass(frozen=True)
class VerifyIR:
    id: str | None
    command: str
    cwd: str | None = None
    timeout: int | None = None
    junit: FrozenMapping | None = None
    writes: tuple[str, ...] = ()
    retry: RetryIR | None = None
    on_failure: str | None = None
    on_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "junit", freeze(self.junit) if self.junit is not None else None)


@dataclass(frozen=True)
class DecideIR:
    id: str | None
    using: FrozenMapping
    on_failure: str | None = None
    on_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "using", freeze(self.using))


@dataclass(frozen=True)
class ChooseIR:
    id: str | None
    value: str
    cases: Mapping[str, tuple[BlockIR, ...]]
    default: tuple[BlockIR, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cases", freeze(self.cases))


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
    artifacts: Mapping[str, str] = field(default_factory=dict)
    on_failure: str | None = None
    on_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", freeze(self.artifacts))


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
    branches: Mapping[str, tuple[BlockIR, ...]]
    timeout_minutes: int | None = None
    on_failure: str | None = None
    on_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "branches", freeze(self.branches))


@dataclass(frozen=True)
class GraphIR:
    id: str | None
    kind: Literal["inline", "include"]
    graph: FrozenMapping | None = None
    path: str | None = None
    on: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "graph", freeze(self.graph) if self.graph is not None else None)
        object.__setattr__(self, "on", freeze(self.on) if self.on is not None else None)


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
    defaults: WorkflowDefaultsIR = field(default_factory=WorkflowDefaultsIR)
