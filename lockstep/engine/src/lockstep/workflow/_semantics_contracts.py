"""Pure output contracts produced by workflow semantic validation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping  # noqa: UP035

from ._semantics_catalog import ChildArtifactContract
from .ir import BlockIR, WorkflowIR, freeze

_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_TERMINAL_VALUES = ("pass", "fail", "error")


def _escape(pointer_part: str) -> str:
    """Encode one RFC 6901 JSON Pointer token."""
    return pointer_part.replace("~", "~0").replace("/", "~1")


class OutcomeProvenance(str, Enum):
    DECISION = "decision"
    VALIDATOR = "validator"
    CHILD = "child"
    PARALLEL = "parallel"


@dataclass(frozen=True)
class OutcomeSymbol:
    name: str
    values: tuple[str, ...]
    provenance: OutcomeProvenance


@dataclass(frozen=True)
class EffectContract:
    writes: tuple[str, ...] = ()

    def union(self, *others: EffectContract) -> EffectContract:
        ordered = list(self.writes)
        for other in others:
            for write in other.writes:
                if write not in ordered:
                    ordered.append(write)
        return EffectContract(tuple(ordered))


@dataclass(frozen=True)
class ArtifactContract:
    handle: str
    source: str
    destination: str


@dataclass(frozen=True)
class RetryContract:
    limit: int
    exhausted: str
    total_executions: int


@dataclass(frozen=True)
class RepeatSimulation:
    iterations: int
    outcome: str


@dataclass(frozen=True)
class RepeatControlContract:
    terminal_producer: str
    producer_cardinalities: tuple[int, ...]
    falls_through: bool = True


@dataclass(frozen=True)
class RepeatContract:
    id: str | None
    limit: int
    until: str
    exhausted: str
    effects: EffectContract
    body: FlowContract
    control: RepeatControlContract

    def simulate(self, terminal_outcomes: tuple[str, ...]) -> RepeatSimulation:
        for iteration, outcome in enumerate(terminal_outcomes[: self.limit], start=1):
            if outcome == "pass":
                return RepeatSimulation(iteration, "pass")
            if outcome == "error":
                return RepeatSimulation(iteration, "escalate")
            if outcome != "fail":
                raise ValueError(f"unsupported repeat terminal outcome: {outcome!r}")
            if iteration == self.limit:
                return RepeatSimulation(iteration, self.exhausted)
        raise ValueError("repeat simulation requires one outcome per entered iteration")


@dataclass(frozen=True)
class BlockContract:
    block: BlockIR
    effects: EffectContract
    retry: RetryContract | None = None
    branches: Mapping[str, FlowContract] = field(default_factory=dict)
    default: FlowContract | None = None
    reconverges: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "branches", freeze(self.branches))


@dataclass(frozen=True)
class FlowContract:
    blocks: tuple[BlockContract | RepeatContract, ...]
    effects: EffectContract


@dataclass(frozen=True)
class ValidatedWorkflow:
    workflow: WorkflowIR
    flow: FlowContract
    outcomes: Mapping[str, OutcomeSymbol]
    artifacts: Mapping[str, ArtifactContract]
    exports: Mapping[str, ChildArtifactContract]
    non_artifact_writes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcomes", freeze(self.outcomes))
        object.__setattr__(self, "artifacts", freeze(self.artifacts))
        object.__setattr__(self, "exports", freeze(self.exports))
        object.__setattr__(self, "non_artifact_writes", tuple(self.non_artifact_writes))
