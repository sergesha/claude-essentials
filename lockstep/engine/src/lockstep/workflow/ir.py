"""Immutable parser IR for the Workflow DSL.

This deliberately represents structure only. Cross-block control-flow,
effect, fragment, and runtime rules are compiler responsibilities.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, TypeAlias

FrozenMapping: TypeAlias = Mapping[str, Any]


def freeze(value: Any) -> Any:
    """Recursively make parser-owned structured values safe to share."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class SourceLocation:
    """Immutable source coordinate retained by the parser without YAML coupling."""

    line: int
    column: int


@dataclass(frozen=True)
class RetryIR:
    limit: int
    exhausted: str | None


@dataclass(frozen=True)
class WorkflowDefaultsIR:
    retry: RetryIR | None = None


@dataclass(frozen=True)
class MarkdownArtifactIR:
    sections: tuple[str, ...]


@dataclass(frozen=True)
class ExportedArtifactIR:
    handle: str
    path: str
    markdown: MarkdownArtifactIR


@dataclass(frozen=True)
class StepIR:
    id: str | None
    step: str
    task: str
    exit: str
    writes: tuple[str, ...] = ()
    evidence: FrozenMapping | None = None
    artifact: ExportedArtifactIR | None = None
    retry: RetryIR | None = None
    on_failure: str | None = None
    on_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", freeze(self.evidence) if self.evidence is not None else None)


@dataclass(frozen=True)
class VerifyIR:
    id: str | None
    command: str
    cwd: str | None = None
    timeout: int | None = None
    retry: RetryIR | None = None
    on_failure: str | None = None
    on_error: str | None = None

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
    artifact_from: str
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
    authored_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "graph", freeze(self.graph) if self.graph is not None else None)
        object.__setattr__(self, "on", freeze(self.on) if self.on is not None else None)
        object.__setattr__(self, "authored_on", tuple(self.authored_on))


_FRAGMENT_IR_TOKEN = object()
_FRAGMENT_STATE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_FRAGMENT_NODE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_FRAGMENT_STATE_TYPES = frozenset({
    "str", "string", "int", "integer", "float", "bool", "boolean",
    "list", "dict", "any",
})


def _fragment_sections(
    document: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any], tuple[Any, ...], Mapping[str, Any]]:
    allowed = {"fragment", "state", "nodes", "edges", "loop_limits", "loop_exits"}
    if set(document) - allowed:
        raise ValueError("fragment document contains unknown fields")
    if not all(key in document for key in ("fragment", "nodes", "edges")):
        raise ValueError("fragment document is missing its closed graph fields")
    fragment = document["fragment"]
    nodes = document["nodes"]
    edges = document["edges"]
    state = document.get("state", {})
    if not isinstance(fragment, Mapping) or not isinstance(nodes, Mapping):
        raise TypeError("fragment metadata and nodes must be mappings")
    if not isinstance(edges, (list, tuple)):
        raise TypeError("fragment edges must be a sequence")
    if not isinstance(state, Mapping):
        raise TypeError("fragment state must be a mapping")
    return fragment, nodes, tuple(edges), state


def _validate_fragment_exits(fragment: Mapping[str, Any]) -> None:
    exits = fragment["exits"]
    if (
        not isinstance(fragment["entry"], str)
        or not isinstance(exits, Mapping)
        or not exits
        or "pass" not in exits
        or set(exits) - {"pass", "fail", "error"}
        or any(not isinstance(value, str) for value in exits.values())
        or len(set(exits.values())) != len(exits)
    ):
        raise ValueError("fragment entry/exits are not closed")


def _validate_fragment_effects(effects: Any) -> None:
    if (
        not isinstance(effects, Mapping)
        or set(effects) != {"mode", "writes"}
        or effects["mode"] not in {"read-only", "declared-writes"}
        or not isinstance(effects["writes"], (list, tuple))
        or any(not isinstance(item, str) for item in effects["writes"])
    ):
        raise ValueError("fragment effects are not closed")
    if effects["mode"] == "read-only" and effects["writes"]:
        raise ValueError("read-only fragment writes must be empty")
    if effects["mode"] == "declared-writes" and not effects["writes"]:
        raise ValueError("declared-writes fragment must declare writes")


def _validate_fragment_metadata(fragment: Mapping[str, Any]) -> None:
    if set(fragment) != {"entry", "exits", "effects"}:
        raise ValueError("fragment metadata must be closed")
    _validate_fragment_exits(fragment)
    _validate_fragment_effects(fragment["effects"])


def _validate_fragment_state(state: Mapping[str, Any]) -> None:
    for name, state_type in state.items():
        if not isinstance(name, str) or not _FRAGMENT_STATE_NAME.fullmatch(name):
            raise ValueError("fragment state names must be safe flat identifiers")
        if state_type not in _FRAGMENT_STATE_TYPES:
            raise ValueError("fragment state type is unsupported")


def _fragment_interrupt_descriptor(node: Mapping[str, Any]) -> Mapping[str, Any]:
    message = node.get("message")
    descriptor = message.get("lockstep_effect") if isinstance(message, Mapping) else None
    if (
        not isinstance(message, Mapping)
        or not isinstance(descriptor, Mapping)
        or descriptor.get("schema") != "lockstep.effect/v1"
        or not isinstance(node.get("state_key"), str)
        or not _FRAGMENT_STATE_NAME.fullmatch(node["state_key"])
        or not isinstance(node.get("resume_key"), str)
        or not _FRAGMENT_STATE_NAME.fullmatch(node["resume_key"])
        or type(node.get("idempotent")) is not bool
    ):
        raise ValueError("fragment interrupt requires a protected message and closed keys")
    return descriptor


def _validate_fragment_interrupt_channels(
    node: Mapping[str, Any],
    descriptor: Mapping[str, Any],
    state: Mapping[str, Any],
) -> None:
    if descriptor.get("kind") == "scope" and (
        not isinstance(descriptor.get("result_state_key"), str)
        or descriptor["result_state_key"] != node["resume_key"]
    ):
        raise ValueError("fragment scope result_state_key must equal interrupt resume_key")
    if (
        node["state_key"] not in state
        or state[node["state_key"]] != "dict"
        or node["resume_key"] not in state
        or state[node["resume_key"]] != "dict"
    ):
        raise ValueError(
            "fragment interrupt request and result channels must be declared dict state"
        )


def _validate_fragment_interrupt_selectors(
    descriptor: Mapping[str, Any], state: Mapping[str, Any]
) -> None:
    for selector_field in ("scope_state_keys", "ancestor_deadline_state_keys"):
        selectors = descriptor.get(selector_field, ())
        if not isinstance(selectors, (list, tuple)) or any(
            not isinstance(key, str) or key not in state or state[key] != "dict"
            for key in selectors
        ):
            raise ValueError("fragment scope selectors must reference declared dict state")


def _validate_fragment_interrupt(
    node: Mapping[str, Any], state: Mapping[str, Any]
) -> None:
    descriptor = _fragment_interrupt_descriptor(node)
    _validate_fragment_interrupt_channels(node, descriptor, state)
    _validate_fragment_interrupt_selectors(descriptor, state)


def _validate_fragment_nodes(
    fragment: Mapping[str, Any],
    nodes: Mapping[str, Any],
    state: Mapping[str, Any],
) -> None:
    if not nodes or any(
        not isinstance(name, str) or not _FRAGMENT_NODE_NAME.fullmatch(name)
        for name in nodes
    ):
        raise ValueError("fragment node names must be safe non-empty identifiers")
    exits = fragment["exits"]
    if fragment["entry"] not in nodes or any(value not in nodes for value in exits.values()):
        raise ValueError("fragment entry/exits must reference declared nodes")
    for node in nodes.values():
        if not isinstance(node, Mapping) or node.get("type") not in {
            "passthrough",
            "interrupt",
        }:
            raise ValueError("fragment node type is outside the closed allowlist")
        allowed = (
            {"type", "output"}
            if node.get("type") == "passthrough"
            else {"type", "message", "state_key", "resume_key", "idempotent"}
        )
        if set(node) - allowed:
            raise ValueError("fragment node contains unknown fields")
        if node["type"] == "passthrough":
            output = node.get("output", {})
            if not isinstance(output, Mapping) or any(
                not isinstance(key, str) or key not in state for key in output
            ):
                raise ValueError("fragment passthrough output must map declared state")
        else:
            _validate_fragment_interrupt(node, state)


def _validate_fragment_edges(edges: tuple[Any, ...]) -> None:
    for edge in edges:
        if (
            not isinstance(edge, Mapping)
            or set(edge) - {"from", "to", "condition"}
            or not isinstance(edge.get("to"), str)
            or ("condition" in edge and not isinstance(edge["condition"], str))
        ):
            raise ValueError("fragment edge is not closed")
        source = edge.get("from")
        if isinstance(source, (list, tuple)):
            if (
                not source or any(not isinstance(item, str) or not item for item in source)
                or len(set(source)) != len(source) or "condition" in edge
            ):
                raise ValueError("fragment join requires distinct sources and no condition")
        elif not isinstance(source, str):
            raise ValueError("fragment edge is not closed")


def _validate_fragment_loops(
    document: Mapping[str, Any],
    nodes: Mapping[str, Any],
    exits: Mapping[str, str],
) -> None:
    loop_limits = document.get("loop_limits", {})
    loop_exits = document.get("loop_exits", {})
    if not isinstance(loop_limits, Mapping) or not isinstance(loop_exits, Mapping):
        raise TypeError("fragment loop metadata must be mappings")
    if set(loop_limits) != set(loop_exits):
        raise ValueError("fragment loop limits and exits must name the same nodes")
    for name, limit in loop_limits.items():
        if (
            name not in nodes
            or type(limit) is not int
            or limit < 1
            or not isinstance(loop_exits[name], str)
            or loop_exits[name] not in exits.values()
        ):
            raise ValueError("fragment loop metadata is invalid")


def _validate_fragment_scalar_tree(value: Any) -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("fragment mapping keys must be strings")
        for child in value.values():
            _validate_fragment_scalar_tree(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _validate_fragment_scalar_tree(child)
        return
    raise TypeError("fragment IR may contain only closed data values")


def _validate_fragment_document(document: Mapping[str, Any]) -> None:
    if not isinstance(document, Mapping):
        raise TypeError("fragment document must be a mapping")
    fragment, nodes, edges, state = _fragment_sections(document)
    _validate_fragment_metadata(fragment)
    _validate_fragment_state(state)
    _validate_fragment_nodes(fragment, nodes, state)
    _validate_fragment_edges(edges)
    _validate_fragment_loops(document, nodes, fragment["exits"])
    _validate_fragment_scalar_tree(document)


@dataclass(frozen=True, init=False)
class FragmentIR:
    """Closed, parser-owned graph fragment supplied through ResolvedCatalog."""

    document: FrozenMapping

    def __init__(self, document: Mapping[str, Any], *, _token: object | None = None) -> None:
        if _token is not _FRAGMENT_IR_TOKEN:
            raise TypeError("FragmentIR must be constructed by FragmentIR.parse")
        object.__setattr__(self, "document", freeze(document))

    @classmethod
    def parse(cls, document: Mapping[str, Any]) -> FragmentIR:
        _validate_fragment_document(document)
        return cls(document, _token=_FRAGMENT_IR_TOKEN)


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
    source_path: Path | None = None
    source_marks: Mapping[str, SourceLocation] = field(default_factory=dict)
    source_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_marks", freeze(self.source_marks))

    def location_for(self, pointer: str) -> SourceLocation | None:
        current = pointer
        while current not in self.source_marks and current:
            current = current.rsplit("/", 1)[0]
        return self.source_marks.get(current) or self.source_marks.get("")
