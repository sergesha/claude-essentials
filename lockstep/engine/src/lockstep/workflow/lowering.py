"""Pure lowering from validated Workflow DSL contracts to yamlgraph data."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath
import re
import shlex
from typing import Any, Literal

import yaml

from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.effects.models import (
    AcceptDescriptor,
    DecisionDescriptor,
    EffectDescriptor,
    ScopeDescriptor,
)

from .canonical import canonical_json, canonical_yaml, plain
from .ir import (
    AcceptIR,
    CallIR,
    ChooseIR,
    DecideIR,
    EscalateIR,
    GraphIR,
    FragmentIR,
    ParallelIR,
    StepIR,
    VerifyIR,
)
from .semantics import (
    BlockContract,
    FlowContract,
    RepeatContract,
    ValidatedWorkflow,
    WorkflowCatalog,
)


@dataclass(frozen=True)
class LoweredGeneratedFile:
    relative_path: str
    content: bytes
    sha256: str
    logical_name: str
    use_pointer: str
    definition_sha256: str


@dataclass(frozen=True)
class LoweredDependency:
    kind: str
    logical_name: str
    use_pointer: str
    definition_sha256: str
    compiled_sha256: str
    generated_root: str | None


def _stable_id(pointer: str, kind: str, role: str) -> str:
    digest = hashlib.sha256(
        b"lockstep.workflow-node/v1\0"
        + pointer.encode("utf-8") + b"\0" + kind.encode("ascii") + b"\0" + role.encode("ascii")
    ).hexdigest()[:12]
    stem = pointer.rsplit("/", 1)[-1] or "root"
    return f"{kind}-{stem}-{role}-{digest}"


def _fragment_state_namespace(namespace: str) -> str:
    digest = hashlib.sha256(
        b"lockstep.fragment-state-namespace/v1\0" + namespace.encode("utf-8")
    ).hexdigest()
    return digest


def _specialized_state_key(namespace: str, key: str) -> str:
    candidate = f"{namespace}_{key}"
    if len(candidate.encode("utf-8")) <= 128 and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", candidate
    ):
        return candidate
    digest = hashlib.sha256(
        b"lockstep.specialized-state-key/v1\0"
        + namespace.encode("ascii")
        + b"\0"
        + key.encode("utf-8")
    ).hexdigest()
    return f"child_{digest}"


def _edge_targets(edge: dict[str, Any]) -> tuple[Any, ...]:
    targets = edge.get("to")
    return tuple(targets) if isinstance(targets, list) else (targets,)


def _specialized_fragment_digest(
    original: dict[str, Any],
    specialized: dict[str, Any],
    expected_digest: str,
    call_namespace: str,
) -> str | None:
    """Recover and re-digest an exact compiler-owned fragment projection."""
    original_nodes = original.get("nodes", {})
    original_state = original.get("state", {})
    original_edges = original.get("edges", [])
    if not isinstance(original_nodes, dict) or not isinstance(original_state, dict):
        return None
    candidates: set[str] = set()
    for node_name in original_nodes:
        if not isinstance(node_name, str):
            continue
        dots = [index for index, character in enumerate(node_name) if character == "."]
        candidates.update(node_name[:index] for index in dots)
    for fragment_namespace in sorted(candidates):
        node_prefix = fragment_namespace + "."
        state_prefix = f"fragment_{_fragment_state_namespace(fragment_namespace)}_"
        state_names = {
            key for key in original_state
            if isinstance(key, str) and key.startswith(state_prefix)
        }
        node_names = {
            key for key in original_nodes
            if isinstance(key, str) and key.startswith(node_prefix)
        }
        projection = {
            "state": {key: original_state[key] for key in original_state if key in state_names},
            "nodes": {key: original_nodes[key] for key in original_nodes if key in node_names},
            "edges": [
                edge for edge in original_edges
                if isinstance(edge, dict)
                and (
                    edge.get("from") in node_names
                    or any(target in node_names for target in _edge_targets(edge))
                )
            ],
        }
        if hashlib.sha256(canonical_yaml(projection)).hexdigest() != expected_digest:
            continue
        specialized_state = specialized.get("state", {})
        specialized_nodes = specialized.get("nodes", {})
        specialized_edges = specialized.get("edges", [])
        mapped_state = {
            _specialized_state_key(call_namespace, key) for key in state_names
        }
        mapped_nodes = {f"{call_namespace}.{key}" for key in node_names}
        transformed = {
            "state": {
                key: specialized_state[key]
                for key in specialized_state
                if key in mapped_state
            },
            "nodes": {
                key: specialized_nodes[key]
                for key in specialized_nodes
                if key in mapped_nodes
            },
            "edges": [
                edge for edge in specialized_edges
                if isinstance(edge, dict)
                and (
                    edge.get("from") in mapped_nodes
                    or any(target in mapped_nodes for target in _edge_targets(edge))
                )
            ],
        }
        return hashlib.sha256(canonical_yaml(transformed)).hexdigest()
    return None


_CONDITION_NAME = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:state\.)?([A-Za-z_][A-Za-z0-9_]*)(\.[A-Za-z_][A-Za-z0-9_]*)*"
)


def _condition_segments(value: str) -> list[tuple[bool, str]]:
    """Split a yamlgraph condition into quoted and expression segments."""
    result: list[tuple[bool, str]] = []
    start = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if quote is None:
            if character in {"'", '"'}:
                if index > start:
                    result.append((False, value[start:index]))
                quote = character
                start = index
            continue
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == quote:
            result.append((True, value[start:index + 1]))
            quote = None
            start = index + 1
    if start < len(value):
        result.append((quote is not None, value[start:]))
    return result


def _rewrite_condition_references(
    value: str,
    mapping: dict[str, str],
    *,
    reject_unknown: bool = False,
) -> str:
    """Rewrite only parsed state paths outside quoted literal spans."""
    keywords = {"and", "or", "not", "true", "false", "null", "none"}
    unknown: set[str] = set()

    def rewrite(match: re.Match[str]) -> str:
        token = match.group(0)
        state_prefix = "state." if token.startswith("state.") else ""
        path = token.removeprefix("state.")
        root, separator, tail = path.partition(".")
        replacement = mapping.get(root)
        if replacement is None:
            if root.lower() not in keywords:
                unknown.add(root)
            return token
        return state_prefix + replacement + (separator + tail if separator else "")

    rewritten = "".join(
        segment if quoted else _CONDITION_NAME.sub(rewrite, segment)
        for quoted, segment in _condition_segments(value)
    )
    if reject_unknown and unknown:
        raise ValueError(
            f"fragment condition references unknown state: {sorted(unknown)}"
        )
    return rewritten


def _split_condition_keyword(value: str, keyword: str) -> list[str] | None:
    pieces: list[str] = []
    current: list[str] = []
    needle = f" {keyword} "
    segments = _condition_segments(value)
    for quoted, segment in segments:
        if quoted:
            current.append(segment)
            continue
        while needle in segment:
            before, segment = segment.split(needle, 1)
            current.append(before)
            pieces.append("".join(current))
            current = []
        current.append(segment)
    pieces.append("".join(current))
    return pieces if len(pieces) > 1 else None


def _condition_may_match_outcome(
    condition: str | None, result_key: str, outcome: str
) -> bool:
    """Conservative abstract evaluation for one protected result outcome."""
    if condition is None:
        return True
    or_parts = _split_condition_keyword(condition, "or")
    if or_parts is not None:
        return any(
            _condition_may_match_outcome(part, result_key, outcome)
            for part in or_parts
        )
    and_parts = _split_condition_keyword(condition, "and")
    if and_parts is not None:
        return all(
            _condition_may_match_outcome(part, result_key, outcome)
            for part in and_parts
        )
    comparison = re.fullmatch(
        rf"\s*(?:state\.)?{re.escape(result_key)}\.outcome\s*(==|!=)\s*"
        r"(['\"])(PASS|FAIL|ERROR)\2\s*",
        condition,
    )
    if comparison is None:
        return True
    operator, _quote, expected = comparison.groups()
    return (outcome == expected) if operator == "==" else (outcome != expected)


def lower_accept_descriptor(
    logical_id: str,
    artifact_handle: str,
    producer_result_state_key: str,
    declared_name: str,
    destination: str,
    transformation: Literal["identity"] = "identity",
    audience: Literal["local-project"] = "local-project",
) -> dict[str, Any]:
    descriptor = {
        "schema": "lockstep.effect/v1",
        "kind": "accept",
        "logical_id": logical_id,
        "artifact_handle": artifact_handle,
        "producer_result_state_key": producer_result_state_key,
        "declared_name": declared_name,
        "destination": destination,
        "transformation": transformation,
        "audience": audience,
        "verdict": "PASS",
        "result_schema": "lockstep.acceptance-result/v1",
    }
    parse_effect_descriptor(descriptor)
    return descriptor


def lower_publish_descriptor(
    logical_id: str,
    *,
    artifact_handle: str,
    producer_result_state_key: str,
    declared_name: str,
    acceptance_result_state_key: str,
    destination: str,
) -> dict[str, Any]:
    descriptor = {
        "schema": "lockstep.effect/v1",
        "kind": "publish",
        "logical_id": logical_id,
        "items": [
            {
                "qualified_handle": artifact_handle,
                "producer_result_state_key": producer_result_state_key,
                "declared_name": declared_name,
                "acceptance_result_state_key": acceptance_result_state_key,
                "destination": destination,
                "transformation": "identity",
                "audience": "local-project",
            }
        ],
        "result_schema": "lockstep.effect-result/v1",
    }
    parse_effect_descriptor(descriptor)
    return descriptor


def _rewrite_child_state_template(
    value: Any,
    key_map: dict[str, str],
) -> Any:
    if isinstance(value, dict):
        return {
            key: _rewrite_child_state_template(item, key_map)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_child_state_template(item, key_map) for item in value]
    if not isinstance(value, str):
        return value
    rewritten = value
    for original, qualified in sorted(
        key_map.items(), key=lambda item: len(item[0]), reverse=True
    ):
        rewritten = rewritten.replace(
            f"{{state.{original}", f"{{state.{qualified}"
        )
    return rewritten


def _specialized_child_state(
    state: dict[str, Any],
    *,
    child_contract: Any,
    namespace: str,
    scope_key: str,
    child_outcome: str,
    reserved_channels: frozenset[str],
) -> tuple[dict[str, str], dict[str, Any]]:
    key_map = {
        key: _specialized_state_key(namespace, key) for key in tuple(state)
    }
    collisions = {
        key: qualified
        for key, qualified in key_map.items()
        if key != "lockstep_outcome" and qualified in reserved_channels
    }
    if collisions:
        raise ValueError(
            "child state specialization collides with compiler-reserved "
            f"call channels: {sorted(collisions)}"
        )
    key_map["lockstep_outcome"] = child_outcome
    key_map[scope_key] = scope_key
    for key in {*child_contract.state_inputs, *child_contract.state_exports}:
        key_map[key] = _specialized_state_key(namespace, key)

    new_state = {key_map[key]: value for key, value in state.items()}
    new_state[scope_key] = "dict"
    for key, state_type in {
        **dict(child_contract.state_inputs),
        **dict(child_contract.state_exports),
    }.items():
        new_state[key_map[key]] = state_type
    return key_map, new_state


def _descriptor_matches_artifact(
    *,
    logical_id: object,
    node_resume_key: object,
    namespace: str,
    key_map: dict[str, str],
    artifact_bindings: tuple[tuple[str, str, str, str, str, str], ...],
) -> bool:
    return any(
        key_map.get(item[4], _specialized_state_key(namespace, item[4]))
        == node_resume_key
        and logical_id == item[5]
        for item in artifact_bindings
    )


def _specialize_descriptor_logical_id(
    descriptor: dict[str, Any], namespace: str
) -> None:
    logical_id = descriptor.get("logical_id")
    if isinstance(logical_id, str):
        logical_digest = hashlib.sha256(
            b"lockstep.specialized-logical-id/v1\0"
            + namespace.encode("ascii")
            + b"\0"
            + logical_id.encode("utf-8")
        ).hexdigest()
        descriptor["logical_id"] = f"child-{logical_digest}"


def _specialize_descriptor_runner(
    descriptor: dict[str, Any],
    *,
    runner: str,
    scope_key: str,
    key_map: dict[str, str],
) -> None:
    if descriptor.get("kind") == "manual" and descriptor.get("runner") is None:
        descriptor["kind"] = "managed"
        descriptor["runner"] = {
            "selector": runner,
            "required_capabilities": ["workspace", "bounded_result", "sandbox"],
        }
        descriptor["scope_state_keys"] = [scope_key]
    elif isinstance(descriptor.get("scope_state_keys"), list):
        mapped_scopes = [
            key_map.get(key, key) for key in descriptor["scope_state_keys"]
        ]
        descriptor["scope_state_keys"] = mapped_scopes if mapped_scopes else [scope_key]


def _specialize_descriptor_inputs(
    descriptor: dict[str, Any],
    *,
    namespace: str,
    key_map: dict[str, str],
) -> None:
    inputs = descriptor.get("inputs")
    if isinstance(inputs, dict):
        for selector in inputs.values():
            if isinstance(selector, dict) and isinstance(
                selector.get("state_key"), str
            ):
                state_key = selector["state_key"]
                selector["state_key"] = key_map.get(
                    state_key, _specialized_state_key(namespace, state_key)
                )


def _specialize_scope_descriptor(
    descriptor: dict[str, Any],
    *,
    namespace: str,
    scope_key: str,
    key_map: dict[str, str],
) -> None:
    if descriptor.get("kind") == "scope":
        ancestors = [
            key_map.get(key, key)
            for key in descriptor.get("ancestor_deadline_state_keys", [])
        ]
        descriptor["ancestor_deadline_state_keys"] = [scope_key, *ancestors]
        result_key = descriptor.get("result_state_key")
        if isinstance(result_key, str):
            descriptor["result_state_key"] = key_map.get(
                result_key, _specialized_state_key(namespace, result_key)
            )


def _specialize_child_descriptor(
    raw_descriptor: dict[str, Any],
    *,
    namespace: str,
    runner: str,
    scope_key: str,
    key_map: dict[str, str],
    new_state: dict[str, Any],
    node_resume_key: object,
    artifact_bindings: tuple[tuple[str, str, str, str, str, str], ...],
    inside_parallel_branch: bool,
) -> tuple[dict[str, Any], bool]:
    descriptor = plain(raw_descriptor)
    if inside_parallel_branch and descriptor.get("kind") == "decide":
        raise ValueError("parallel child may not hide a decision descriptor")
    matching_artifact = _descriptor_matches_artifact(
        logical_id=descriptor.get("logical_id"),
        node_resume_key=node_resume_key,
        namespace=namespace,
        key_map=key_map,
        artifact_bindings=artifact_bindings,
    )
    _specialize_descriptor_logical_id(descriptor, namespace)
    _specialize_descriptor_runner(
        descriptor, runner=runner, scope_key=scope_key, key_map=key_map
    )
    _specialize_descriptor_inputs(
        descriptor, namespace=namespace, key_map=key_map
    )
    _specialize_scope_descriptor(
        descriptor,
        namespace=namespace,
        scope_key=scope_key,
        key_map=key_map,
    )
    parse_effect_descriptor(descriptor, known_state_keys=set(new_state))
    return descriptor, matching_artifact


def _specialize_child_node(
    raw_node: object,
    *,
    namespace: str,
    runner: str,
    scope_key: str,
    key_map: dict[str, str],
    new_state: dict[str, Any],
    artifact_bindings: tuple[tuple[str, str, str, str, str, str], ...],
    inside_parallel_branch: bool,
) -> dict[str, Any]:
    if not isinstance(raw_node, dict):
        raise ValueError("resolved child node must be a mapping")
    node = plain(raw_node)
    output = node.get("output")
    if isinstance(output, dict):
        node["output"] = {
            key_map.get(key, key): _rewrite_child_state_template(value, key_map)
            for key, value in output.items()
        }
    for field in ("state_key", "resume_key"):
        value = node.get(field)
        if isinstance(value, str):
            node[field] = key_map.get(
                value, _specialized_state_key(namespace, value)
            )
            new_state.setdefault(node[field], "dict")
    message = node.get("message")
    descriptor = message.get("lockstep_effect") if isinstance(message, dict) else None
    if isinstance(descriptor, dict):
        rewritten, matching_artifact = _specialize_child_descriptor(
            descriptor,
            namespace=namespace,
            runner=runner,
            scope_key=scope_key,
            key_map=key_map,
            new_state=new_state,
            node_resume_key=node.get("resume_key"),
            artifact_bindings=artifact_bindings,
            inside_parallel_branch=inside_parallel_branch,
        )
        if matching_artifact:
            message["artifact_contract"] = {}
        message["lockstep_effect"] = rewritten
    if isinstance(message, dict):
        for message_key, message_value in tuple(message.items()):
            if message_key != "lockstep_effect":
                message[message_key] = _rewrite_child_state_template(
                    message_value, key_map
                )
    return node


def _specialize_child_edges(
    raw_edges: object,
    *,
    node_map: dict[str, str],
    key_map: dict[str, str],
) -> list[dict[str, Any]]:
    rewritten_edges: list[dict[str, Any]] = []
    for raw_edge in raw_edges:
        edge = plain(raw_edge)
        source = edge.get("from")
        if source not in {"START", "END"}:
            edge["from"] = node_map[source]
        targets = edge.get("to")
        if isinstance(targets, list):
            edge["to"] = [
                target if target in {"START", "END"} else node_map[target]
                for target in targets
            ]
        elif targets not in {"START", "END"}:
            edge["to"] = node_map[targets]
        if "condition" in edge:
            edge["condition"] = _rewrite_condition_references(
                edge["condition"], key_map
            )
        rewritten_edges.append(edge)
    return rewritten_edges


def _specialize_child_loops(
    document: dict[str, Any],
    node_map: dict[str, str],
) -> None:
    for field in ("loop_limits", "loop_exits"):
        raw = document.get(field)
        if isinstance(raw, dict):
            document[field] = {
                node_map.get(key, key): (
                    node_map.get(value, value) if field == "loop_exits" else value
                )
                for key, value in raw.items()
            }


@dataclass
class _Exit:
    source: str
    condition: str | None = None


@dataclass
class _Fragment:
    entry: str
    exits: list[_Exit]


@dataclass(frozen=True)
class _GraphFragmentPlan:
    raw: dict[str, Any]
    fragment: dict[str, Any]
    nodes: dict[str, Any]
    edges: list[Any]
    state: dict[str, Any]
    namespace: str
    local_names: frozenset[str]
    entry: str
    exits: dict[str, str]
    logical_name: str
    source_definition_sha256: str


@dataclass(frozen=True)
class _FragmentNames:
    namespace: str
    state: Mapping[str, Any]

    def node(self, name: str) -> str:
        return f"{self.namespace}.{name}"

    def state_key(self, name: str) -> str:
        return f"fragment_{_fragment_state_namespace(self.namespace)}_{name}"

    def identity(self, kind: str, name: str) -> str:
        digest = hashlib.sha256(
            b"lockstep.fragment-identity/v1\0"
            + kind.encode("ascii")
            + b"\0"
            + self.namespace.encode("utf-8")
            + b"\0"
            + name.encode("utf-8")
        ).hexdigest()
        return f"fragment-{kind}-{digest}"

    def template(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self.template(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.template(item) for item in value]
        if not isinstance(value, str):
            return value
        referenced = re.findall(r"\{state\.([A-Za-z_][A-Za-z0-9_]*)", value)
        unknown = set(referenced) - set(self.state)
        if unknown:
            raise ValueError(
                f"fragment template references unknown state: {sorted(unknown)}"
            )
        rewritten = value
        for local_key in self.state:
            rewritten = rewritten.replace(
                f"{{state.{local_key}", f"{{state.{self.state_key(local_key)}"
            )
        return rewritten

    def condition(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return _rewrite_condition_references(
            value,
            {local_key: self.state_key(local_key) for local_key in self.state},
            reject_unknown=True,
        )


class _Builder:
    def __init__(
        self, validated: ValidatedWorkflow, catalog: WorkflowCatalog | None = None
    ) -> None:
        self.validated = validated
        self.workflow = validated.workflow
        self.catalog = catalog
        self.generated_files: list[LoweredGeneratedFile] = []
        self.dependencies: list[LoweredDependency] = []
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.state: dict[str, str] = {"lockstep_outcome": "str"}
        self.state["lockstep_continue"] = "bool"
        self.generated_state_names = {"lockstep_outcome", "lockstep_continue"}
        self.loop_limits: dict[str, int] = {}
        self.loop_exits: dict[str, str] = {}
        self.source_nodes: dict[str, dict[str, int | str]] = {}
        self.outcome_keys: dict[str, str] = {}
        self.artifact_state_keys: dict[str, tuple[str, str]] = {}
        self.terminals = {
            outcome: self.node("/terminal", "terminal", outcome.lower(), {
                "type": "passthrough", "output": {"lockstep_outcome": outcome}
            })
            for outcome in ("PASS", "FAIL", "ERROR", "ABORTED")
        }
        self.active_scope_state_keys: tuple[str, ...] = ()
        self.outcome_targets: dict[str, str] = dict(self.terminals)
        self.capture_aborted_effects = False
        self.inside_parallel_branch = False

    def outcome_target(self, outcome: str) -> str:
        return self.outcome_targets[outcome]

    def declare_generated_state(self, name: str, state_type: str) -> None:
        """Register an internal channel without ever aliasing public state."""
        existing = self.state.get(name)
        if existing is not None and name not in self.generated_state_names:
            raise ValueError(f"generated state collision: {name}")
        if existing is not None and existing != state_type:
            raise ValueError(f"generated state type collision: {name}")
        self.state[name] = state_type
        self.generated_state_names.add(name)

    def node(self, pointer: str, kind: str, role: str, value: dict[str, Any]) -> str:
        name = _stable_id(pointer, kind, role)
        existing = self.nodes.get(name)
        if existing is not None:
            raise ValueError(f"stable generated node collision: {name}")
        self.nodes[name] = value
        mark = self.workflow.location_for(pointer)
        self.source_nodes[name] = {
            "pointer": pointer,
            "line": mark.line if mark else 1,
            "column": mark.column if mark else 1,
        }
        return name

    def edge(
        self, source: str, target: str | list[str], condition: str | None = None
    ) -> None:
        edge: dict[str, Any] = {"from": source, "to": target}
        if condition is not None:
            edge["condition"] = condition
        self.edges.append(edge)

    def connect(self, exits: list[_Exit], target: str) -> None:
        for item in exits:
            self.edge(item.source, target, item.condition)

    def descriptor_interrupt(
        self,
        pointer: str,
        kind: str,
        logical_id: str,
        descriptor: dict[str, Any],
        message: dict[str, Any],
        result_key: str,
        retry_limit: int | None,
        *,
        failure_target: str | None = None,
    ) -> _Fragment:
        parse_effect_descriptor(descriptor)
        request_key = f"{logical_id.replace('-', '_')}_request"
        self.declare_generated_state(request_key, "dict")
        self.declare_generated_state(result_key, "dict")
        interrupt = self.node(pointer, kind, "effect", {
            "type": "interrupt", "message": message,
            "state_key": request_key, "resume_key": result_key, "idempotent": False,
        })
        entry = interrupt
        retry_gate = None
        if retry_limit is not None:
            retry_gate = self.node(pointer, kind, "attempt", {
                "type": "passthrough", "output": {"lockstep_continue": True}
            })
            exhausted = self.node(pointer, kind, "exhausted", {"type": "passthrough"})
            self.edge(retry_gate, interrupt, "lockstep_continue == true")
            self.edge(exhausted, self.outcome_target("FAIL"))
            self.loop_limits[retry_gate] = retry_limit
            self.loop_exits[retry_gate] = exhausted
            entry = retry_gate
        if self.capture_aborted_effects:
            self.edge(
                interrupt,
                self.outcome_target("ABORTED"),
                f"{result_key}.fixed_error_code == 'cancelled'",
            )
            self.edge(
                interrupt,
                self.outcome_target("ERROR"),
                f"{result_key}.outcome == 'ERROR' and "
                f"{result_key}.fixed_error_code != 'cancelled'",
            )
        else:
            self.edge(
                interrupt,
                self.outcome_target("ABORTED"),
                f"{result_key}.fixed_error_code == 'cancelled'",
            )
            self.edge(
                interrupt,
                self.outcome_target("ERROR"),
                f"{result_key}.outcome == 'ERROR'",
            )
        fail_target = retry_gate or failure_target or self.outcome_target("FAIL")
        self.edge(interrupt, fail_target, f"{result_key}.outcome == 'FAIL'")
        return _Fragment(entry, [_Exit(interrupt, f"{result_key}.outcome == 'PASS'")])

    def _lower_step(
        self,
        block: StepIR,
        pointer: str,
        retry_limit: int | None,
        failure_target: str | None,
    ) -> _Fragment:
        logical = block.id or block.step
        result_key = f"{logical.replace('-', '_')}_result"
        descriptor = {
            "schema": "lockstep.effect/v1",
            "kind": "manual",
            "logical_id": logical,
            "runner": None,
            "inputs": {},
            "writes": list(block.writes),
            "artifacts": [],
            "deadline_seconds": None,
            "scope_state_keys": [],
            "result_schema": "lockstep.effect-result/v1",
        }
        message = {
            "step": block.step,
            "task": block.task,
            "exit_criterion": block.exit,
            "evidence_schema": (
                plain(block.evidence) if block.evidence is not None else {}
            ),
            "artifact_contract": (
                plain(block.artifact) if block.artifact is not None else {}
            ),
            "lockstep_effect": descriptor,
        }
        return self.descriptor_interrupt(
            pointer,
            "step",
            logical,
            descriptor,
            message,
            result_key,
            retry_limit,
            failure_target=failure_target,
        )

    def _lower_verify(
        self,
        block: VerifyIR,
        pointer: str,
        retry_limit: int | None,
        failure_target: str | None,
    ) -> _Fragment:
        logical = block.id or f"verify-{pointer.rsplit('/', 1)[-1]}"
        result_key = f"{logical.replace('-', '_')}_result"
        command_key = f"{logical.replace('-', '_')}_command"
        self.declare_generated_state(command_key, "dict")
        prepare = self.node(
            pointer,
            "verify",
            "command",
            {
                "type": "passthrough",
                "output": {command_key: {
                    "schema": "lockstep.pinned-command/v1",
                    "logical_argv": shlex.split(block.command),
                    "logical_cwd": block.cwd or ".",
                    "result_source": "exit",
                }},
            },
        )
        descriptor = {
            "schema": "lockstep.effect/v1",
            "kind": "verify",
            "logical_id": logical,
            "runner": {
                "selector": "pinned",
                "required_capabilities": ["workspace", "bounded_result", "sandbox"],
            },
            "inputs": {
                "command": {"state_key": command_key},
                "snapshot": {"runtime_key": "current_project_snapshot"},
            },
            "writes": [],
            "artifacts": [],
            "deadline_seconds": block.timeout,
            "scope_state_keys": list(self.active_scope_state_keys),
            "result_schema": "lockstep.effect-result/v1",
        }
        effect = self.descriptor_interrupt(
            pointer,
            "verify",
            logical,
            descriptor,
            {"step": logical, "lockstep_effect": descriptor},
            result_key,
            retry_limit,
            failure_target=failure_target,
        )
        self.edge(prepare, effect.entry)
        return _Fragment(prepare, effect.exits)

    def _lower_decide(self, block: DecideIR, pointer: str) -> _Fragment:
        logical = block.id or "decision"
        result_key = f"{logical.replace('-', '_')}_result"
        using = plain(block.using)
        descriptor = {
            "schema": "lockstep.effect/v1",
            "kind": "decide",
            "logical_id": logical,
            "decision": {
                "type": "changed-paths",
                "since": "start",
                "cases": [
                    {"label": label, "paths": list(paths)}
                    for label, paths in using["cases"].items()
                ],
                "default": using["default"],
            },
            "inputs": {
                "start_snapshot": {"runtime_key": "run_start_project_snapshot"},
                "current_snapshot": {"runtime_key": "current_project_snapshot"},
            },
            "result_schema": "lockstep.decision-result/v1",
        }
        self.outcome_keys[logical] = result_key
        return self.descriptor_interrupt(
            pointer,
            "decide",
            logical,
            descriptor,
            {"step": logical, "lockstep_effect": descriptor},
            result_key,
            None,
        )

    def _lower_accept(self, block: AcceptIR, pointer: str) -> _Fragment:
        logical = block.id or f"accept-{pointer.rsplit('/', 1)[-1]}"
        result_key = f"{logical.replace('-', '_')}_result"
        try:
            producer_key, declared_name = self.artifact_state_keys[
                block.artifact_from
            ]
        except KeyError as exc:
            raise ValueError(
                "accept artifact lacks a compiler-owned producer result channel"
            ) from exc
        artifact = self.validated.artifacts[block.artifact_from]
        descriptor = lower_accept_descriptor(
            logical,
            block.artifact_from,
            producer_key,
            declared_name,
            artifact.destination,
        )
        acceptance = self.descriptor_interrupt(
            pointer,
            "accept",
            logical,
            descriptor,
            {"step": logical, "lockstep_effect": descriptor},
            result_key,
            None,
        )
        publication_logical = f"publish-{logical}"
        publication_result = f"{publication_logical.replace('-', '_')}_result"
        publish_descriptor = lower_publish_descriptor(
            publication_logical,
            artifact_handle=block.artifact_from,
            producer_result_state_key=producer_key,
            declared_name=declared_name,
            acceptance_result_state_key=result_key,
            destination=artifact.destination,
        )
        publication = self.descriptor_interrupt(
            pointer,
            "publish",
            publication_logical,
            publish_descriptor,
            {"step": publication_logical, "lockstep_effect": publish_descriptor},
            publication_result,
            None,
        )
        self.connect(acceptance.exits, publication.entry)
        return _Fragment(acceptance.entry, publication.exits)

    def _lower_choose(
        self,
        block: ChooseIR,
        contract: BlockContract,
        pointer: str,
    ) -> _Fragment:
        result_key = self.outcome_keys.get(
            block.value, block.value.replace("-", "_") + "_result"
        )
        router = self.node(pointer, "choose", "route", {"type": "passthrough"})
        join = self.node(pointer, "choose", "join", {"type": "passthrough"})
        for label in block.cases:
            fragment = self.flow_contract(
                contract.branches[label], f"{pointer}/choose/cases/{label}"
            )
            self.edge(router, fragment.entry, f"{result_key}.value == '{label}'")
            self.connect(fragment.exits, join)
        if block.default is not None and contract.default is not None:
            fragment = self.flow_contract(
                contract.default, f"{pointer}/choose/default"
            )
            condition = " and ".join(
                f"{result_key}.value != '{label}'" for label in block.cases
            )
            self.edge(router, fragment.entry, condition)
            self.connect(fragment.exits, join)
        return _Fragment(router, [_Exit(join)])

    def block(
        self, contract: BlockContract, pointer: str, *, failure_target: str | None = None
    ) -> _Fragment:
        block = contract.block
        retry_limit = contract.retry.limit if contract.retry else None
        if isinstance(block, StepIR):
            return self._lower_step(block, pointer, retry_limit, failure_target)
        if isinstance(block, VerifyIR):
            return self._lower_verify(block, pointer, retry_limit, failure_target)
        if isinstance(block, DecideIR):
            return self._lower_decide(block, pointer)
        if isinstance(block, AcceptIR):
            return self._lower_accept(block, pointer)
        if isinstance(block, EscalateIR):
            return _Fragment(self.outcome_target("FAIL"), [])
        if isinstance(block, ChooseIR):
            return self._lower_choose(block, contract, pointer)
        if isinstance(block, GraphIR):
            return self.graph(contract, pointer)
        if isinstance(block, CallIR):
            return self.call(contract, pointer)
        if isinstance(block, ParallelIR):
            return self.parallel(contract, pointer)
        raise NotImplementedError(f"Task 8 cannot lower {type(block).__name__}")

    def _parallel_scope_fragment(
        self,
        block: ParallelIR,
        pointer: str,
        digest: str,
        outer_scopes: tuple[str, ...],
    ) -> tuple[_Fragment | None, tuple[str, ...]]:
        if block.timeout_minutes is not None:
            scope_key = f"parallel_{digest}_scope_result"
            descriptor = {
                "schema": "lockstep.effect/v1",
                "kind": "scope",
                "logical_id": f"parallel-{digest}-scope",
                "scope_kind": "parallel",
                "duration_seconds": block.timeout_minutes * 60,
                "runner_selector": None,
                "ancestor_deadline_state_keys": list(outer_scopes),
                "result_state_key": scope_key,
                "result_schema": "lockstep.scope-result/v1",
            }
            scope_fragment = self.descriptor_interrupt(
                pointer,
                "parallel",
                f"parallel-{digest}-scope",
                descriptor,
                {"step": block.id, "lockstep_effect": descriptor},
                scope_key,
                None,
            )
            return scope_fragment, (*outer_scopes, scope_key)
        return None, outer_scopes

    def _lower_parallel_branches(
        self,
        *,
        contract: BlockContract,
        pointer: str,
        digest: str,
        join: str,
        branch_scopes: tuple[str, ...],
    ) -> tuple[list[str], list[str]]:
        outer_targets = dict(self.outcome_targets)
        outer_scopes = self.active_scope_state_keys
        outer_aborted_capture = self.capture_aborted_effects
        outer_parallel_branch = self.inside_parallel_branch
        branch_entries: list[str] = []
        branch_result_keys: list[str] = []
        try:
            for branch_name, branch_flow in contract.branches.items():
                branch_pointer = f"{pointer}/parallel/branches/{branch_name}"
                branch_key = (
                    f"parallel_{digest}_{branch_name.replace('-', '_')}_outcome"
                )
                self.declare_generated_state(branch_key, "str")
                branch_result_keys.append(branch_key)
                completion = self.node(
                    branch_pointer,
                    "parallel-branch",
                    "complete",
                    {"type": "passthrough"},
                )
                setters = {
                    outcome: self.node(
                        branch_pointer,
                        "parallel-branch",
                        f"set-{outcome.lower()}",
                        {"type": "passthrough", "output": {branch_key: outcome}},
                    )
                    for outcome in ("PASS", "FAIL", "ERROR", "ABORTED")
                }
                for setter in setters.values():
                    self.edge(setter, completion)
                self.edge(completion, join)

                self.active_scope_state_keys = branch_scopes
                self.outcome_targets = setters
                self.capture_aborted_effects = True
                self.inside_parallel_branch = True
                fragment = self.flow_contract(branch_flow, branch_pointer)
                branch_entries.append(fragment.entry)
                self.connect(fragment.exits, setters["PASS"])
        finally:
            self.active_scope_state_keys = outer_scopes
            self.outcome_targets = outer_targets
            self.capture_aborted_effects = outer_aborted_capture
            self.inside_parallel_branch = outer_parallel_branch
        return branch_entries, branch_result_keys

    def _route_parallel_outcomes(
        self,
        *,
        pointer: str,
        join: str,
        result_key: str,
        branch_result_keys: list[str],
        outer_targets: Mapping[str, str],
    ) -> str:
        aggregate = {
            "PASS": {"outcome": "PASS", "value": "pass"},
            "FAIL": {"outcome": "FAIL", "value": "fail"},
            "ERROR": {"outcome": "ERROR", "value": "error"},
            "ABORTED": {
                "outcome": "ERROR",
                "value": "error",
                "fixed_error_code": "cancelled",
            },
        }
        outcomes = {
            outcome: self.node(
                pointer,
                "parallel",
                f"outcome-{outcome.lower()}",
                {"type": "passthrough", "output": {result_key: value}},
            )
            for outcome, value in aggregate.items()
        }
        route = join
        for precedence in ("ABORTED", "ERROR", "FAIL"):
            for index, branch_key in enumerate(branch_result_keys):
                next_route = self.node(
                    pointer,
                    "parallel",
                    f"check-{precedence.lower()}-{index}",
                    {"type": "passthrough"},
                )
                self.edge(route, outcomes[precedence], f"{branch_key} == '{precedence}'")
                self.edge(route, next_route, f"{branch_key} != '{precedence}'")
                route = next_route
        self.edge(route, outcomes["PASS"])
        for outcome in ("FAIL", "ERROR", "ABORTED"):
            self.edge(outcomes[outcome], outer_targets[outcome])
        return outcomes["PASS"]

    def parallel(self, contract: BlockContract, pointer: str) -> _Fragment:
        block = contract.block
        if not isinstance(block, ParallelIR):
            raise TypeError("parallel lowering requires ParallelIR")
        if block.id is None or block.join != "all":
            raise ValueError("parallel lowering requires an id and join: all")

        outer_targets = dict(self.outcome_targets)
        outer_scopes = self.active_scope_state_keys
        digest = hashlib.sha256(
            b"lockstep.parallel-scope/v1\0" + pointer.encode("utf-8")
        ).hexdigest()[:24]
        scope_fragment, branch_scopes = self._parallel_scope_fragment(
            block, pointer, digest, outer_scopes
        )

        fork = self.node(pointer, "parallel", "fork", {"type": "passthrough"})
        join = self.node(pointer, "parallel", "join", {"type": "passthrough"})
        result_key = f"{block.id.replace('-', '_')}_result"
        self.declare_generated_state(result_key, "dict")
        self.outcome_keys[block.id] = result_key

        branch_entries, branch_result_keys = self._lower_parallel_branches(
            contract=contract,
            pointer=pointer,
            digest=digest,
            join=join,
            branch_scopes=branch_scopes,
        )

        self.edge(fork, branch_entries)
        if scope_fragment is None:
            entry = fork
        else:
            self.connect(scope_fragment.exits, fork)
            entry = scope_fragment.entry

        pass_outcome = self._route_parallel_outcomes(
            pointer=pointer,
            join=join,
            result_key=result_key,
            branch_result_keys=branch_result_keys,
            outer_targets=outer_targets,
        )
        return _Fragment(entry, [_Exit(pass_outcome)])

    def _resolved_call_child(self, block: CallIR) -> Any:
        if self.catalog is None:
            raise ValueError("call lowering requires a resolved catalog")
        resolver = getattr(self.catalog, "child_for", None)
        resolved = resolver(block.workflow) if callable(resolver) else None
        if resolved is None:
            raise ValueError(
                f"resolved compiled child is unavailable for {block.workflow!r}"
            )
        return resolved

    def _call_identity(
        self,
        block: CallIR,
        resolved: Any,
        pointer: str,
    ) -> tuple[str, str, str, dict[str, str], frozenset[str]]:
        call_digest = hashlib.sha256(
            b"lockstep.call-specialization/v1\0"
            + pointer.encode("utf-8")
            + b"\0"
            + self.workflow.source_sha256.encode("ascii")
            + b"\0"
            + block.workflow.encode("utf-8")
            + b"\0"
            + block.runner.encode("utf-8")
            + b"\0"
            + str(resolved.source_definition_sha256).encode("ascii")
            + b"\0"
            + str(resolved.standalone.bundle_sha256).encode("ascii")
            + b"\0"
            + canonical_json({
                "state_inputs": dict(resolved.contract.state_inputs),
                "state_exports": dict(resolved.contract.state_exports),
            })
        ).hexdigest()
        namespace = f"call_{call_digest}"
        scope_key = f"{namespace}_scope_result"
        child_outcome = f"{namespace}_outcome"
        saved_context = {
            "current_step": f"{namespace}_parent_current_step",
            "_loop_counts": f"{namespace}_parent_loop_counts",
            "_loop_limit_reached": f"{namespace}_parent_loop_limit_reached",
        }
        scope_request_key = f"call_{call_digest}_scope_request"
        reserved_child_channels = frozenset({
            scope_request_key,
            scope_key,
            child_outcome,
            *saved_context.values(),
        })
        return (
            call_digest,
            scope_key,
            child_outcome,
            saved_context,
            reserved_child_channels,
        )

    def _declare_call_context_channels(
        self,
        *,
        child_outcome: str,
        saved_context: dict[str, str],
    ) -> None:
        self.declare_generated_state(child_outcome, "str")
        self.declare_generated_state("current_step", "str")
        self.declare_generated_state("_loop_counts", "dict")
        self.declare_generated_state("_loop_limit_reached", "bool")
        self.declare_generated_state(saved_context["current_step"], "any")
        self.declare_generated_state(saved_context["_loop_counts"], "dict")
        self.declare_generated_state(saved_context["_loop_limit_reached"], "any")

    def _bind_call_artifacts(
        self,
        block: CallIR,
        child_contract: Any,
    ) -> dict[str, tuple[str, str, str, str, str]]:
        artifact_specs: dict[str, tuple[str, str, str, str, str]] = {}
        for handle, destination in block.artifacts.items():
            export = child_contract.exports[handle]
            matches = [
                artifact
                for qualified, artifact in self.validated.artifacts.items()
                if qualified.endswith(f".{block.id}.{handle}")
                or qualified == f"{block.id}.{handle}"
                if artifact.source == export.fixed_source
                and artifact.destination == destination
            ]
            if len(matches) != 1:
                raise ValueError(
                    "child artifact export is not uniquely bound in parent semantics"
                )
            qualified = matches[0].handle
            declared_name = export.declared_name
            channel = "artifact_" + hashlib.sha256(
                ("lockstep.artifact-channel/v1\0" + qualified).encode("utf-8")
            ).hexdigest()
            self.declare_generated_state(channel, "dict")
            self.artifact_state_keys[qualified] = (channel, declared_name)
            artifact_specs[qualified] = (
                declared_name,
                export.fixed_source,
                export.media_type,
                export.producer_logical_id,
                export.producer_result_state_key,
            )
        return artifact_specs

    def _declare_call_contract_state(
        self,
        child_contract: Any,
        namespace: str,
    ) -> None:
        for key, state_type in {
            **dict(child_contract.state_inputs),
            **dict(child_contract.state_exports),
        }.items():
            existing = self.state.get(key)
            if key in self.generated_state_names:
                raise ValueError(f"call state collides with generated channel: {key}")
            if existing is not None and existing != state_type:
                raise ValueError(f"call state type collision: {key}")
            self.state[key] = state_type
            self.declare_generated_state(f"{namespace}_{key}", state_type)

    def _call_scope_fragment(
        self,
        block: CallIR,
        pointer: str,
        call_digest: str,
        scope_key: str,
    ) -> _Fragment:
        descriptor = {
            "schema": "lockstep.effect/v1",
            "kind": "scope",
            "logical_id": f"call-{call_digest}-scope",
            "scope_kind": "call",
            "duration_seconds": (
                block.timeout_minutes * 60
                if block.timeout_minutes is not None
                else None
            ),
            "runner_selector": block.runner,
            "ancestor_deadline_state_keys": list(self.active_scope_state_keys),
            "result_state_key": scope_key,
            "result_schema": "lockstep.scope-result/v1",
        }
        return self.descriptor_interrupt(
            pointer,
            "call",
            f"call-{call_digest}-scope",
            descriptor,
            {"step": block.id or block.workflow, "lockstep_effect": descriptor},
            scope_key,
            None,
        )

    def _call_context_nodes(
        self,
        *,
        pointer: str,
        namespace: str,
        child_contract: Any,
        saved_context: dict[str, str],
    ) -> tuple[str, str]:
        pre_output = {
            f"{namespace}_{key}": f"{{state.{key}}}"
            for key in child_contract.state_inputs
        }
        context_output = {
            saved_context["current_step"]: "{state.current_step}",
            saved_context["_loop_counts"]: "{state._loop_counts}",
            saved_context["_loop_limit_reached"]: "{state._loop_limit_reached}",
            "current_step": None,
            "_loop_counts": {},
            "_loop_limit_reached": False,
        }
        context = self.node(
            pointer,
            "call",
            "context",
            {"type": "passthrough", "output": context_output},
        )
        pre = self.node(
            pointer,
            "call",
            "pre",
            {"type": "passthrough", "output": pre_output},
        )
        return context, pre

    def _register_specialized_child_channels(
        self,
        specialized: dict[str, Any],
    ) -> None:
        specialized_state = specialized.get("state", {})
        for specialized_node in specialized.get("nodes", {}).values():
            if not isinstance(specialized_node, dict):
                continue
            for field in ("state_key", "resume_key"):
                state_key = specialized_node.get(field)
                if isinstance(state_key, str) and state_key not in self.state:
                    self.declare_generated_state(
                        state_key, specialized_state.get(state_key, "dict")
                    )
            message = specialized_node.get("message")
            effect = (
                message.get("lockstep_effect")
                if isinstance(message, dict)
                else None
            )
            inputs = effect.get("inputs") if isinstance(effect, dict) else None
            if isinstance(inputs, dict):
                for selector in inputs.values():
                    state_key = (
                        selector.get("state_key")
                        if isinstance(selector, dict)
                        else None
                    )
                    if isinstance(state_key, str) and state_key not in self.state:
                        self.declare_generated_state(
                            state_key, specialized_state.get(state_key, "any")
                        )
            if isinstance(effect, dict):
                self._register_specialized_effect_channels(
                    effect, specialized_state
                )

    def _register_specialized_effect_channels(
        self,
        effect: dict[str, Any],
        specialized_state: dict[str, Any],
    ) -> None:
        shared_keys = []
        for field in ("scope_state_keys", "ancestor_deadline_state_keys"):
            values = effect.get(field)
            if isinstance(values, list):
                shared_keys.extend(key for key in values if isinstance(key, str))
        result_state_key = effect.get("result_state_key")
        if isinstance(result_state_key, str):
            shared_keys.append(result_state_key)
        for state_key in shared_keys:
            if state_key not in self.state:
                self.declare_generated_state(
                    state_key,
                    specialized_state.get(state_key, "dict"),
                )

    def _specialize_call_members(
        self,
        *,
        block: CallIR,
        pointer: str,
        resolved: Any,
        call_digest: str,
        namespace: str,
        scope_key: str,
        child_outcome: str,
        reserved_child_channels: frozenset[str],
        producer_bindings: Mapping[
            str, tuple[tuple[str, str, str, str, str, str], ...]
        ],
    ) -> tuple[str, list[tuple[str, bytes]]]:
        generated_base = f"generated/children/{call_digest}"
        generated_path = (
            f"{generated_base}/{resolved.standalone.root_relative_path}"
        )
        specialized_members: list[tuple[str, bytes]] = []
        for source_file in resolved.standalone.files:
            specialized = self._specialize_child(
                resolved,
                namespace,
                scope_key,
                child_outcome,
                block.runner,
                reserved_channels=reserved_child_channels,
                source_file=source_file,
                artifact_bindings=producer_bindings.get(
                    source_file.relative_path, ()
                ),
            )
            target_path = f"{generated_base}/{source_file.relative_path}"
            specialized_bytes = canonical_yaml(specialized)
            specialized_members.append((target_path, specialized_bytes))
            self._register_specialized_child_channels(specialized)
            self.generated_files.append(
                LoweredGeneratedFile(
                    target_path,
                    specialized_bytes,
                    hashlib.sha256(specialized_bytes).hexdigest(),
                    block.workflow,
                    pointer,
                    resolved.source_definition_sha256,
                )
            )
        return generated_path, specialized_members

    @staticmethod
    def _compiled_bundle_digest(
        root_path: str,
        members: Mapping[str, bytes],
    ) -> str:
        digest = hashlib.sha256(b"lockstep.compiled-bundle/v1\0")
        digest.update(root_path.encode("utf-8"))
        digest.update(b"\0")
        for member_path, member_bytes in sorted(members.items()):
            digest.update(member_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(member_bytes).hexdigest().encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()

    def _record_call_workflow_dependency(
        self,
        *,
        block: CallIR,
        pointer: str,
        resolved: Any,
        generated_path: str,
        specialized_members: list[tuple[str, bytes]],
    ) -> None:
        self.dependencies.append(
            LoweredDependency(
                "workflow",
                block.workflow,
                pointer,
                resolved.source_definition_sha256,
                self._compiled_bundle_digest(
                    generated_path, dict(specialized_members)
                ),
                generated_path,
            )
        )

    @staticmethod
    def _specialized_members_by_source(
        resolved: Any,
        specialized_members: list[tuple[str, bytes]],
    ) -> dict[str, tuple[str, bytes]]:
        return {
            source_file.relative_path: member
            for source_file, member in zip(
                resolved.standalone.files, specialized_members, strict=True
            )
        }

    @staticmethod
    def _reachable_dependency_members(
        generated_root: str,
        specialized_by_source: Mapping[str, tuple[str, bytes]],
    ) -> set[str]:
        reachable = {generated_root}
        pending = [generated_root]
        while pending:
            current = pending.pop()
            current_document = yaml.safe_load(specialized_by_source[current][1])
            nodes = (
                current_document.get("nodes", {})
                if isinstance(current_document, dict)
                else {}
            )
            for node in nodes.values() if isinstance(nodes, dict) else ():
                graph = node.get("graph") if isinstance(node, dict) else None
                if not isinstance(graph, str):
                    continue
                child_source = (PurePosixPath(current).parent / graph).as_posix()
                if child_source not in specialized_by_source:
                    raise ValueError(
                        "compiled child dependency graph references an unknown member"
                    )
                if child_source not in reachable:
                    reachable.add(child_source)
                    pending.append(child_source)
        return reachable

    def _rebased_dependency_digest(
        self,
        generated_root: str,
        specialized_by_source: Mapping[str, tuple[str, bytes]],
    ) -> tuple[str, str]:
        rebased_root = specialized_by_source[generated_root][0]
        reachable = self._reachable_dependency_members(
            generated_root, specialized_by_source
        )
        members = {
            specialized_by_source[source_path][0]:
            specialized_by_source[source_path][1]
            for source_path in reachable
        }
        return rebased_root, self._compiled_bundle_digest(rebased_root, members)

    @staticmethod
    def _call_fragment_dependency_digest(
        *,
        resolved: Any,
        specialized_by_source: Mapping[str, tuple[str, bytes]],
        dependency: Any,
        namespace: str,
    ) -> str:
        for source_file in resolved.standalone.files:
            _target_path, specialized_bytes = specialized_by_source[
                source_file.relative_path
            ]
            original_document = yaml.safe_load(source_file.content)
            specialized_document = yaml.safe_load(specialized_bytes)
            if not isinstance(original_document, dict) or not isinstance(
                specialized_document, dict
            ):
                continue
            transformed = _specialized_fragment_digest(
                original_document,
                specialized_document,
                dependency.compiled_sha256,
                namespace,
            )
            if transformed is not None:
                return transformed
        raise ValueError(
            "compiled child fragment dependency projection is unavailable"
        )

    def _record_call_transitive_dependencies(
        self,
        *,
        resolved: Any,
        specialized_members: list[tuple[str, bytes]],
        namespace: str,
        pointer: str,
    ) -> None:
        specialized_by_source = self._specialized_members_by_source(
            resolved, specialized_members
        )
        for dependency in resolved.standalone.dependencies:
            rebased_root = None
            compiled_sha256 = dependency.compiled_sha256
            if dependency.generated_root is not None:
                rebased_root, compiled_sha256 = self._rebased_dependency_digest(
                    dependency.generated_root, specialized_by_source
                )
            elif dependency.kind == "fragment":
                compiled_sha256 = self._call_fragment_dependency_digest(
                    resolved=resolved,
                    specialized_by_source=specialized_by_source,
                    dependency=dependency,
                    namespace=namespace,
                )
            self.dependencies.append(
                LoweredDependency(
                    dependency.kind,
                    dependency.logical_name,
                    f"{pointer}{dependency.use_pointer}",
                    dependency.definition_sha256,
                    compiled_sha256,
                    rebased_root,
                )
            )

    def _call_post_output(
        self,
        *,
        namespace: str,
        child_contract: Any,
        artifact_specs: Mapping[str, tuple[str, str, str, str, str]],
        producer_bindings: Mapping[
            str, tuple[tuple[str, str, str, str, str, str], ...]
        ],
    ) -> dict[str, str]:
        output = {
            key: f"{{state.{namespace}_{key}}}"
            for key in child_contract.state_exports
        }
        producers = tuple(
            item for items in producer_bindings.values() for item in items
        )
        for qualified in artifact_specs:
            channel, _name = self.artifact_state_keys[qualified]
            producer = next(item for item in producers if item[0] == qualified)
            output[channel] = (
                f"{{state.{_specialized_state_key(namespace, producer[4])}}}"
            )
        return output

    def _finish_call_graph(
        self,
        *,
        pointer: str,
        generated_path: str,
        namespace: str,
        child_contract: Any,
        artifact_specs: Mapping[str, tuple[str, str, str, str, str]],
        producer_bindings: Mapping[
            str, tuple[tuple[str, str, str, str, str, str], ...]
        ],
        saved_context: dict[str, str],
        child_outcome: str,
        context: str,
        scope: _Fragment,
        pre: str,
    ) -> _Fragment:
        child = self.node(
            pointer,
            "call",
            "direct",
            {"type": "subgraph", "graph": generated_path, "mode": "direct"},
        )
        post = self.node(
            pointer,
            "call",
            "post",
            {
                "type": "passthrough",
                "output": self._call_post_output(
                    namespace=namespace,
                    child_contract=child_contract,
                    artifact_specs=artifact_specs,
                    producer_bindings=producer_bindings,
                ),
            },
        )
        restoration_output = {
            "current_step": f"{{state.{saved_context['current_step']}}}",
            "_loop_counts": f"{{state.{saved_context['_loop_counts']}}}",
            "_loop_limit_reached": (
                f"{{state.{saved_context['_loop_limit_reached']}}}"
            ),
        }
        restorations = {
            outcome: self.node(
                pointer,
                "call",
                f"restore-{outcome.lower()}",
                {"type": "passthrough", "output": restoration_output},
            )
            for outcome in ("PASS", "FAIL", "ERROR", "ABORTED")
        }
        self.edge(context, scope.entry)
        self.connect(scope.exits, pre)
        self.edge(pre, child)
        self.edge(child, post)
        for outcome, restore in restorations.items():
            self.edge(post, restore, f"{child_outcome} == '{outcome}'")
            if outcome != "PASS":
                self.edge(restore, self.outcome_target(outcome))
        return _Fragment(context, [_Exit(restorations["PASS"])])

    def call(self, contract: BlockContract, pointer: str) -> _Fragment:
        block = contract.block
        if not isinstance(block, CallIR):
            raise TypeError("call lowering requires CallIR")
        resolved = self._resolved_call_child(block)
        (
            call_digest,
            scope_key,
            child_outcome,
            saved_context,
            reserved_child_channels,
        ) = self._call_identity(block, resolved, pointer)
        namespace = f"call_{call_digest}"
        self._declare_call_context_channels(
            child_outcome=child_outcome,
            saved_context=saved_context,
        )
        child_contract = resolved.contract
        artifact_specs = self._bind_call_artifacts(block, child_contract)
        producer_bindings = self._artifact_producers(resolved, artifact_specs)
        self._declare_call_contract_state(child_contract, namespace)

        scope = self._call_scope_fragment(
            block,
            pointer,
            call_digest,
            scope_key,
        )
        context, pre = self._call_context_nodes(
            pointer=pointer,
            namespace=namespace,
            child_contract=child_contract,
            saved_context=saved_context,
        )
        generated_path, specialized_members = self._specialize_call_members(
            block=block,
            pointer=pointer,
            resolved=resolved,
            call_digest=call_digest,
            namespace=namespace,
            scope_key=scope_key,
            child_outcome=child_outcome,
            reserved_child_channels=reserved_child_channels,
            producer_bindings=producer_bindings,
        )
        self._record_call_workflow_dependency(
            block=block,
            pointer=pointer,
            resolved=resolved,
            generated_path=generated_path,
            specialized_members=specialized_members,
        )
        self._record_call_transitive_dependencies(
            resolved=resolved,
            specialized_members=specialized_members,
            namespace=namespace,
            pointer=pointer,
        )
        return self._finish_call_graph(
            pointer=pointer,
            generated_path=generated_path,
            namespace=namespace,
            child_contract=child_contract,
            artifact_specs=artifact_specs,
            producer_bindings=producer_bindings,
            saved_context=saved_context,
            child_outcome=child_outcome,
            context=context,
            scope=scope,
            pre=pre,
        )

    @staticmethod
    def _artifact_producers(
        resolved: Any,
        artifact_specs: Mapping[str, tuple[str, str, str, str, str]],
    ) -> dict[str, tuple[tuple[str, str, str, str, str, str], ...]]:
        by_file: dict[str, list[tuple[str, str, str, str, str, str]]] = {}
        for qualified, spec in artifact_specs.items():
            declared_name, source, media_type, producer_logical_id, result_key = spec
            candidates: list[tuple[str, tuple[str, str, str, str, str, str]]] = []
            for source_file in resolved.standalone.files:
                document = yaml.safe_load(source_file.content)
                nodes = document.get("nodes", {}) if isinstance(document, dict) else {}
                for node_name, node in nodes.items() if isinstance(nodes, dict) else ():
                    if not isinstance(node, dict) or node.get("type") != "interrupt":
                        continue
                    message = node.get("message")
                    descriptor = (
                        message.get("lockstep_effect")
                        if isinstance(message, dict)
                        else None
                    )
                    resume_key = node.get("resume_key")
                    if not isinstance(descriptor, dict) or not isinstance(resume_key, str):
                        continue
                    if (
                        descriptor.get("logical_id") == producer_logical_id
                        and resume_key == result_key
                    ):
                        declarations = descriptor.get("artifacts")
                        expected = {
                            "name": declared_name,
                            "source_path": source,
                            "media_type": media_type,
                            "required": True,
                        }
                        if (
                            not isinstance(declarations, list)
                            or sum(item == expected for item in declarations) != 1
                        ):
                            raise ValueError(
                                "child artifact contract differs from producer declaration"
                            )
                        candidates.append((source_file.relative_path, (
                                qualified,
                                declared_name,
                                source,
                                media_type,
                                resume_key,
                                producer_logical_id,
                            )))
            if len(candidates) != 1:
                raise ValueError(
                    f"child artifact source {source!r} requires exactly one contract-bound producer"
                )
            relative_path, candidate = candidates[0]
            by_file.setdefault(relative_path, []).append(candidate)
        return {key: tuple(value) for key, value in by_file.items()}

    def _specialize_child(
        self,
        resolved: Any,
        namespace: str,
        scope_key: str,
        child_outcome: str,
        runner: str,
        *,
        reserved_channels: frozenset[str],
        source_file: Any | None = None,
        artifact_bindings: tuple[tuple[str, str, str, str, str, str], ...] = (),
    ) -> dict[str, Any]:
        selected_file = source_file or next(
            item
            for item in resolved.standalone.files
            if item.relative_path == resolved.standalone.root_relative_path
        )
        document = yaml.safe_load(selected_file.content)
        if not isinstance(document, dict):
            raise ValueError("resolved child root must be a YAML mapping")
        state = document.setdefault("state", {})
        if not isinstance(state, dict):
            raise ValueError("resolved child state must be a mapping")
        child_contract = resolved.contract
        if selected_file.relative_path == resolved.standalone.root_relative_path:
            for key, state_type in {
                **dict(child_contract.state_inputs),
                **dict(child_contract.state_exports),
            }.items():
                if key not in state:
                    raise ValueError(
                        f"child state contract key {key!r} is missing from standalone schema"
                    )
                if state[key] != state_type:
                    raise ValueError(
                        f"child state contract type mismatch for {key!r}: "
                        f"expected {state_type!r}, got {state[key]!r}"
                    )
        key_map, new_state = _specialized_child_state(
            state,
            child_contract=child_contract,
            namespace=namespace,
            scope_key=scope_key,
            child_outcome=child_outcome,
            reserved_channels=reserved_channels,
        )
        document["state"] = new_state
        nodes = document.get("nodes", {})
        if not isinstance(nodes, dict):
            raise ValueError("resolved child nodes must be a mapping")
        node_map = {name: f"{namespace}.{name}" for name in nodes}
        document["nodes"] = {
            node_map[name]: _specialize_child_node(
                raw_node,
                namespace=namespace,
                runner=runner,
                scope_key=scope_key,
                key_map=key_map,
                new_state=new_state,
                artifact_bindings=artifact_bindings,
                inside_parallel_branch=self.inside_parallel_branch,
            )
            for name, raw_node in nodes.items()
        }
        document["edges"] = _specialize_child_edges(
            document.get("edges", []),
            node_map=node_map,
            key_map=key_map,
        )
        _specialize_child_loops(document, node_map)
        document["name"] = f"{document.get('name', resolved.logical_name)}-{namespace}"
        return document

    def _graph_source(
        self,
        block: GraphIR,
        pointer: str,
    ) -> tuple[dict[str, Any], str, str]:
        if block.kind == "inline":
            inline_document = plain(block.graph or {})
            inline_document.pop("id", None)
            raw = plain(FragmentIR.parse(inline_document).document)
            source_definition_sha256 = hashlib.sha256(
                canonical_yaml(raw)
            ).hexdigest()
            return raw, source_definition_sha256, f"inline:{pointer}"
        if self.catalog is None:
            raise ValueError("include_graph lowering requires a resolved catalog")
        resolver = getattr(self.catalog, "fragment_for", None)
        resolved = resolver(block.path) if callable(resolver) else None
        if resolved is None:
            raise ValueError(f"resolved fragment is unavailable for {block.path!r}")
        return (
            plain(resolved.fragment.document),
            resolved.source_definition_sha256,
            resolved.logical_path,
        )

    def _graph_plan(self, block: GraphIR, pointer: str) -> _GraphFragmentPlan:
        raw, source_definition_sha256, logical_name = self._graph_source(
            block, pointer
        )
        fragment, nodes, edges, state = self._closed_graph_components(raw)
        namespace = block.id or _stable_id(pointer, "graph", "namespace")
        local_names, entry, exits = self._closed_graph_boundary(
            fragment, nodes
        )
        return _GraphFragmentPlan(
            raw=raw,
            fragment=fragment,
            nodes=nodes,
            edges=edges,
            state=state,
            namespace=namespace,
            local_names=local_names,
            entry=entry,
            exits=exits,
            logical_name=logical_name,
            source_definition_sha256=source_definition_sha256,
        )

    @staticmethod
    def _closed_graph_components(
        raw: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], list[Any], dict[str, Any]]:
        fragment = raw.get("fragment")
        nodes = raw.get("nodes")
        edges = raw.get("edges")
        state = raw.get("state", {})
        if not isinstance(fragment, dict) or not isinstance(nodes, dict):
            raise ValueError("invalid closed graph fragment")
        if not isinstance(edges, list) or not isinstance(state, dict):
            raise ValueError("invalid closed graph fragment")
        return fragment, nodes, edges, state

    @staticmethod
    def _closed_graph_boundary(
        fragment: Mapping[str, Any],
        nodes: Mapping[str, Any],
    ) -> tuple[frozenset[str], str, dict[str, str]]:
        local_names = frozenset(nodes)
        if len(local_names) > 1_000:
            raise ValueError("graph fragment exceeds the 1000-node expansion cap")
        if not local_names or any(
            not isinstance(name, str) or not name for name in local_names
        ):
            raise ValueError("graph fragment nodes must be a non-empty string mapping")
        entry = fragment.get("entry")
        exits = fragment.get("exits")
        if entry not in local_names or not isinstance(exits, dict) or "pass" not in exits:
            raise ValueError("graph fragment requires an existing entry and pass exit")
        if not exits or set(exits) - {"pass", "fail", "error"}:
            raise ValueError("graph fragment exits are not closed")
        if any(target not in local_names for target in exits.values()):
            raise ValueError("graph fragment exit targets must exist")
        return local_names, entry, exits

    def _declare_fragment_state(
        self,
        plan: _GraphFragmentPlan,
        names: _FragmentNames,
    ) -> set[str]:
        fragment_state_keys: set[str] = set()
        for key, state_type in plan.state.items():
            qualified = names.state_key(key)
            if qualified in self.state:
                raise ValueError(f"fragment generated state collision: {qualified}")
            self.declare_generated_state(qualified, state_type)
            fragment_state_keys.add(qualified)
        return fragment_state_keys

    @staticmethod
    def _qualify_fragment_descriptor_state(
        descriptor: dict[str, Any],
        names: _FragmentNames,
    ) -> None:
        inputs = descriptor.get("inputs")
        if isinstance(inputs, dict):
            for selector in inputs.values():
                if isinstance(selector, dict) and isinstance(
                    selector.get("state_key"), str
                ):
                    selector["state_key"] = names.state_key(selector["state_key"])
        for field in ("scope_state_keys", "ancestor_deadline_state_keys"):
            if isinstance(descriptor.get(field), list):
                descriptor[field] = [
                    names.state_key(key) for key in descriptor[field]
                ]
        if isinstance(descriptor.get("result_state_key"), str):
            descriptor["result_state_key"] = names.state_key(
                descriptor["result_state_key"]
            )

    @staticmethod
    def _qualify_fragment_descriptor_artifacts(
        descriptor: dict[str, Any],
        names: _FragmentNames,
    ) -> None:
        artifacts = descriptor.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if isinstance(artifact, dict) and isinstance(
                    artifact.get("name"), str
                ):
                    artifact["name"] = names.identity(
                        "artifact", artifact["name"]
                    )
        if isinstance(descriptor.get("artifact_handle"), str):
            descriptor["artifact_handle"] = names.identity(
                "artifact", descriptor["artifact_handle"]
            )

    def _inherit_fragment_scopes(self, descriptor: dict[str, Any]) -> None:
        if not self.active_scope_state_keys:
            return
        if descriptor.get("kind") == "manual":
            raise ValueError(
                "unmanaged manual fragment effects cannot enter a bounded scope"
            )
        if descriptor.get("kind") == "scope":
            descriptor["ancestor_deadline_state_keys"] = [
                *self.active_scope_state_keys,
                *descriptor.get("ancestor_deadline_state_keys", []),
            ]
        elif isinstance(descriptor.get("scope_state_keys"), list):
            descriptor["scope_state_keys"] = [
                *self.active_scope_state_keys,
                *descriptor["scope_state_keys"],
            ]

    @staticmethod
    def _fragment_descriptor_outcomes(parsed: Any) -> tuple[str, ...]:
        if isinstance(parsed, EffectDescriptor):
            return ("pass", "fail", "error")
        if isinstance(parsed, (ScopeDescriptor, DecisionDescriptor)):
            return ("pass", "error")
        if isinstance(parsed, AcceptDescriptor):
            return ("pass",)
        raise TypeError("unknown protected fragment descriptor")

    def _rewrite_fragment_interrupt(
        self,
        copied: dict[str, Any],
        original: dict[str, Any],
        names: _FragmentNames,
        fragment_state_keys: set[str],
    ) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        message = copied.get("message")
        descriptor = (
            message.get("lockstep_effect") if isinstance(message, dict) else None
        )
        if not isinstance(descriptor, dict):
            raise ValueError("fragment interrupts must carry a protected descriptor")
        for field in ("state_key", "resume_key"):
            value = copied.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"fragment interrupt requires {field}")
            copied[field] = names.state_key(value)
            if copied[field] not in self.state:
                self.declare_generated_state(copied[field], "dict")
            fragment_state_keys.add(copied[field])
        descriptor = plain(descriptor)
        if self.inside_parallel_branch and descriptor.get("kind") == "decide":
            raise ValueError("parallel graph may not hide a decision descriptor")
        logical_id = descriptor.get("logical_id")
        if isinstance(logical_id, str):
            descriptor["logical_id"] = names.identity("effect", logical_id)
        self._qualify_fragment_descriptor_state(descriptor, names)
        self._qualify_fragment_descriptor_artifacts(descriptor, names)
        if isinstance(message.get("step"), str):
            message["step"] = names.identity("step", message["step"])
        self._inherit_fragment_scopes(descriptor)
        if message.get("artifact_contract") not in (None, [], {}):
            raise ValueError(
                "fragment artifact contracts must use protected descriptor artifacts"
            )
        for message_key, message_value in tuple(message.items()):
            if message_key != "lockstep_effect":
                message[message_key] = names.template(message_value)
        message["lockstep_effect"] = descriptor
        parsed = parse_effect_descriptor(descriptor, known_state_keys=set(self.state))
        return (
            original["resume_key"],
            self._fragment_descriptor_outcomes(parsed),
            tuple(getattr(parsed, "writes", ())),
        )

    @staticmethod
    def _rewrite_fragment_output(
        copied: dict[str, Any],
        *,
        protected_resume_keys: set[Any],
        plan: _GraphFragmentPlan,
        names: _FragmentNames,
    ) -> None:
        output = copied.get("output")
        if not isinstance(output, dict):
            return
        overwritten_results = set(output) & protected_resume_keys
        if overwritten_results:
            raise ValueError(
                "fragment passthrough may not overwrite protected result "
                f"channels: {sorted(overwritten_results)}"
            )
        unknown_outputs = set(output) - set(plan.state)
        if unknown_outputs:
            raise ValueError(
                f"fragment output writes undeclared state: {sorted(unknown_outputs)}"
            )
        copied["output"] = {
            names.state_key(key): names.template(value)
            for key, value in output.items()
        }

    def _install_fragment_nodes(
        self,
        plan: _GraphFragmentPlan,
        names: _FragmentNames,
        pointer: str,
    ) -> tuple[set[str], dict[str, tuple[str, tuple[str, ...]]], list[str]]:
        fragment_state_keys = self._declare_fragment_state(plan, names)
        protected_resume_keys = {
            node.get("resume_key")
            for node in plan.nodes.values()
            if isinstance(node, dict) and node.get("type") == "interrupt"
        }
        interrupt_outcomes: dict[str, tuple[str, tuple[str, ...]]] = {}
        declared_writes: list[str] = []
        for name, node in plan.nodes.items():
            if not isinstance(node, dict) or node.get("type") not in {
                "passthrough", "interrupt",
            }:
                raise ValueError(
                    "generated graph fragments may contain only passthrough and "
                    "protected interrupt nodes"
                )
            copied = plain(node)
            if copied.get("type") == "interrupt":
                resume_key, outcomes, writes = self._rewrite_fragment_interrupt(
                    copied, node, names, fragment_state_keys
                )
                interrupt_outcomes[name] = (resume_key, outcomes)
                for write in writes:
                    if write not in declared_writes:
                        declared_writes.append(write)
            self._rewrite_fragment_output(
                copied,
                protected_resume_keys=protected_resume_keys,
                plan=plan,
                names=names,
            )
            qualified = names.node(name)
            if qualified in self.nodes:
                raise ValueError(f"fragment node collision: {qualified}")
            self.nodes[qualified] = copied
            mark = self.workflow.location_for(pointer)
            self.source_nodes[qualified] = {
                "pointer": pointer,
                "line": mark.line if mark else 1,
                "column": mark.column if mark else 1,
            }
        return fragment_state_keys, interrupt_outcomes, declared_writes

    @staticmethod
    def _validate_fragment_effects(
        fragment: Mapping[str, Any],
        declared_writes: list[str],
    ) -> None:
        effects = fragment.get("effects", {})
        mode = effects.get("mode") if isinstance(effects, dict) else None
        expected_writes = (
            list(effects.get("writes", [])) if isinstance(effects, dict) else []
        )
        if mode not in {"read-only", "declared-writes"}:
            raise ValueError("fragment effects mode must be closed")
        if mode == "read-only" and (expected_writes or declared_writes):
            raise ValueError("read-only fragment may not declare protected writes")
        canonical_writes = sorted(set(declared_writes))
        if expected_writes != sorted(set(expected_writes)):
            raise ValueError("fragment declared writes must be canonical and unique")
        if expected_writes != canonical_writes:
            raise ValueError(
                "fragment declared writes must exactly equal protected descriptor writes"
            )

    def _install_fragment_edges(
        self,
        plan: _GraphFragmentPlan,
        names: _FragmentNames,
    ) -> tuple[dict[str, set[str]], dict[str, list[dict[str, Any]]]]:
        adjacency: dict[str, set[str]] = {
            name: set() for name in plan.local_names
        }
        edges_by_source: dict[str, list[dict[str, Any]]] = {
            name: [] for name in plan.local_names
        }
        for edge in plan.edges:
            if not isinstance(edge, dict) or set(edge) - {
                "from", "to", "condition"
            }:
                raise ValueError("invalid graph fragment edge")
            source, target = edge.get("from"), edge.get("to")
            if source not in plan.local_names or target not in plan.local_names:
                raise ValueError(
                    "graph fragment edges must remain inside the fragment"
                )
            adjacency[source].add(target)
            edges_by_source[source].append(edge)
            self.edge(
                names.node(source),
                names.node(target),
                names.condition(edge.get("condition")),
            )
        return adjacency, edges_by_source

    @staticmethod
    def _fragment_conditions_are_exhaustive(
        source: str,
        conditions: list[str],
        interrupt_outcomes: Mapping[str, tuple[str, tuple[str, ...]]],
    ) -> bool:
        outcome_contract = interrupt_outcomes.get(source)
        if outcome_contract is not None:
            result_key, outcomes = outcome_contract
            covered = set()
            for condition in conditions:
                match = re.fullmatch(
                    rf"\s*(?:state\.)?{re.escape(result_key)}\.outcome\s*==\s*"
                    r"(['\"])(PASS|FAIL|ERROR)\1\s*",
                    condition,
                )
                if match is not None:
                    covered.add(match.group(2).lower())
            if covered >= set(outcomes):
                return True
        comparisons: list[tuple[str, str, str]] = []
        for condition in conditions:
            match = re.fullmatch(
                r"\s*([A-Za-z_][A-Za-z0-9_.]*)\s*"
                r"(==|!=|<=|>=|<|>)\s*(.+?)\s*",
                condition,
            )
            if match is not None:
                comparisons.append(match.groups())
        complements = {"==": "!=", "!=": "=="}
        return any(
            left == other_left
            and value == other_value
            and operator in complements
            and complements[operator] == other_operator
            for left, operator, value in comparisons
            for other_left, other_operator, other_value in comparisons
        )

    def _validate_fragment_edge_routes(
        self,
        edges_by_source: Mapping[str, list[dict[str, Any]]],
        interrupt_outcomes: Mapping[str, tuple[str, tuple[str, ...]]],
    ) -> None:
        for source, outgoing in edges_by_source.items():
            conditional = [
                edge
                for edge in outgoing
                if isinstance(edge.get("condition"), str)
            ]
            if not conditional:
                continue
            if len(conditional) != len(outgoing):
                raise ValueError(
                    "fragment nodes may not mix conditional and unconditional edges"
                )
            conditions = [edge["condition"] for edge in conditional]
            if self._fragment_conditions_are_exhaustive(
                source, conditions, interrupt_outcomes
            ):
                continue
            raise ValueError("fragment conditional routing must be proven exhaustive")

    @staticmethod
    def _fragment_loop_analysis(
        plan: _GraphFragmentPlan,
        adjacency: Mapping[str, set[str]],
    ) -> tuple[dict[str, int], dict[str, str], dict[str, set[str]]]:
        loop_limits = plan.raw.get("loop_limits", {})
        loop_exits = plan.raw.get("loop_exits", {})
        if not isinstance(loop_limits, dict) or not isinstance(loop_exits, dict):
            raise ValueError("fragment loop metadata must be mappings")
        if set(loop_limits) != set(loop_exits):
            raise ValueError(
                "fragment loop limits and exits must name the same nodes"
            )
        if any(
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            for limit in loop_limits.values()
        ):
            raise ValueError("fragment loop limits must be positive integers")
        analysis_adjacency = {
            name: set(targets) for name, targets in adjacency.items()
        }
        for local_name, exit_target in loop_exits.items():
            if local_name not in plan.local_names or exit_target not in plan.local_names:
                raise ValueError("fragment loop exit references an unknown node")
            if exit_target not in plan.exits.values():
                raise ValueError(
                    "fragment loop exit must target a declared local exit"
                )
            analysis_adjacency[local_name].add(exit_target)
        return loop_limits, loop_exits, analysis_adjacency

    @staticmethod
    def _effect_outcome_reachability(
        *,
        interrupt_name: str,
        resume_key: str,
        outcome_name: str,
        edge_records: list[dict[str, Any]],
        loop_exits: Mapping[str, str],
        interrupt_outcomes: Mapping[str, tuple[str, tuple[str, ...]]],
    ) -> tuple[set[str], bool]:
        reachable: set[str] = set()
        pending = [interrupt_name]
        reached_successor_effect = False
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            if current != interrupt_name and current in interrupt_outcomes:
                reached_successor_effect = True
                continue
            possible_targets = {
                edge["to"]
                for edge in edge_records
                if edge.get("from") == current
                and _condition_may_match_outcome(
                    edge.get("condition"), resume_key, outcome_name.upper()
                )
            }
            if current in loop_exits:
                possible_targets.add(loop_exits[current])
            if current == interrupt_name and len(possible_targets) != 1:
                raise ValueError(
                    "protected fragment effect routing must select exactly one "
                    "successor for every outcome"
                )
            pending.extend(possible_targets)
        return reachable, reached_successor_effect

    def _validate_fragment_effect_outcomes(
        self,
        plan: _GraphFragmentPlan,
        interrupt_outcomes: Mapping[str, tuple[str, tuple[str, ...]]],
        loop_exits: Mapping[str, str],
    ) -> None:
        edge_records = [
            edge for edge in plan.edges if isinstance(edge, dict)
        ]
        for interrupt_name, (resume_key, outcomes) in interrupt_outcomes.items():
            for outcome_name in outcomes:
                if outcome_name not in plan.exits:
                    raise ValueError(
                        f"fallible fragment effect requires a declared "
                        f"{outcome_name} exit"
                    )
                reachable, reached_successor = self._effect_outcome_reachability(
                    interrupt_name=interrupt_name,
                    resume_key=resume_key,
                    outcome_name=outcome_name,
                    edge_records=edge_records,
                    loop_exits=loop_exits,
                    interrupt_outcomes=interrupt_outcomes,
                )
                reached_exits = {
                    name
                    for name, target in plan.exits.items()
                    if target in reachable
                }
                pass_valid = outcome_name == "pass" and (
                    (reached_successor and not reached_exits)
                    or (not reached_successor and reached_exits == {"pass"})
                )
                failure_valid = (
                    outcome_name != "pass"
                    and not reached_successor
                    and reached_exits == {outcome_name}
                )
                if not (pass_valid or failure_valid):
                    raise ValueError(
                        "protected fragment effect outcomes must reach only their "
                        "matching declared exit"
                    )

    @staticmethod
    def _reachable_fragment_nodes(
        starts: list[str],
        adjacency: Mapping[str, set[str]],
    ) -> set[str]:
        reachable: set[str] = set()
        frontier = list(starts)
        while frontier:
            current = frontier.pop()
            if current in reachable:
                continue
            reachable.add(current)
            frontier.extend(adjacency[current])
        return reachable

    @staticmethod
    def _fragment_termination_nodes(
        exits: Mapping[str, str],
        adjacency: Mapping[str, set[str]],
    ) -> set[str]:
        reverse: dict[str, set[str]] = {name: set() for name in adjacency}
        for source, targets in adjacency.items():
            for target in targets:
                reverse[target].add(source)
        return _Builder._reachable_fragment_nodes(list(exits.values()), reverse)

    @staticmethod
    def _visit_fragment_cycle(
        name: str,
        *,
        adjacency: Mapping[str, set[str]],
        loop_limits: Mapping[str, int],
        loop_exits: Mapping[str, str],
        exits: Mapping[str, str],
        visiting: set[str],
        visited: set[str],
    ) -> None:
        if name in visiting:
            raise ValueError("unbounded graph fragment cycle is not allowed")
        if name in visited:
            return
        visiting.add(name)
        for target in adjacency[name]:
            if target in visiting:
                capped = target if target in loop_limits else name
                cap = loop_limits.get(capped)
                exit_target = loop_exits.get(capped)
                if (
                    not isinstance(cap, int)
                    or isinstance(cap, bool)
                    or cap < 1
                    or exit_target not in exits.values()
                ):
                    raise ValueError(
                        "graph fragment cycle requires a positive local limit "
                        "and declared local exit"
                    )
                continue
            _Builder._visit_fragment_cycle(
                target,
                adjacency=adjacency,
                loop_limits=loop_limits,
                loop_exits=loop_exits,
                exits=exits,
                visiting=visiting,
                visited=visited,
            )
        visiting.remove(name)
        visited.add(name)

    def _validate_fragment_topology(
        self,
        *,
        plan: _GraphFragmentPlan,
        names: _FragmentNames,
        adjacency: Mapping[str, set[str]],
        analysis_adjacency: Mapping[str, set[str]],
        loop_limits: Mapping[str, int],
        loop_exits: Mapping[str, str],
    ) -> None:
        reachable = self._reachable_fragment_nodes(
            [plan.entry], analysis_adjacency
        )
        if any(target not in reachable for target in plan.exits.values()):
            raise ValueError("every declared graph fragment exit must be reachable")
        if reachable != plan.local_names:
            raise ValueError("graph fragment may not contain unreachable nodes")
        can_terminate = self._fragment_termination_nodes(
            plan.exits, analysis_adjacency
        )
        if reachable - can_terminate:
            raise ValueError(
                "every reachable fragment path must be able to terminate"
            )
        self._visit_fragment_cycle(
            plan.entry,
            adjacency=adjacency,
            loop_limits=loop_limits,
            loop_exits=loop_exits,
            exits=plan.exits,
            visiting=set(),
            visited=set(),
        )
        for local_name, limit in loop_limits.items():
            if local_name not in plan.local_names:
                raise ValueError("fragment loop limit references an unknown node")
            self.loop_limits[names.node(local_name)] = limit
        for local_name, exit_target in loop_exits.items():
            self.loop_exits[names.node(local_name)] = names.node(exit_target)
        terminal_names = {name for name in reachable if not adjacency[name]}
        if terminal_names - set(plan.exits.values()):
            raise ValueError(
                "every reachable graph fragment path must end at an exit"
            )
        if any(adjacency[target] for target in plan.exits.values()):
            raise ValueError("graph fragment exit nodes may not have outgoing edges")

    def _finish_graph_fragment(
        self,
        *,
        plan: _GraphFragmentPlan,
        names: _FragmentNames,
        pointer: str,
        fragment_state_keys: set[str],
    ) -> _Fragment:
        entry_gate = self.node(
            pointer, "graph", "entry", {"type": "passthrough"}
        )
        pass_gate = self.node(pointer, "graph", "pass", {"type": "passthrough"})
        self.edge(entry_gate, names.node(plan.entry))
        self.edge(names.node(plan.exits["pass"]), pass_gate)
        if "fail" in plan.exits:
            self.edge(
                names.node(plan.exits["fail"]), self.outcome_target("FAIL")
            )
        if "error" in plan.exits:
            self.edge(
                names.node(plan.exits["error"]), self.outcome_target("ERROR")
            )
        expansion = canonical_yaml({
            "state": {
                key: self.state[key]
                for key in self.state
                if key in fragment_state_keys
            },
            "nodes": {
                key: self.nodes[key]
                for key in self.nodes
                if key.startswith(plan.namespace + ".")
            },
            "edges": [
                edge
                for edge in self.edges
                if str(edge.get("from", "")).startswith(plan.namespace + ".")
                or str(edge.get("to", "")).startswith(plan.namespace + ".")
            ],
        })
        self.dependencies.append(
            LoweredDependency(
                "fragment",
                plan.logical_name,
                pointer,
                plan.source_definition_sha256,
                hashlib.sha256(expansion).hexdigest(),
                None,
            )
        )
        return _Fragment(entry_gate, [_Exit(pass_gate)])

    def graph(self, contract: BlockContract, pointer: str) -> _Fragment:
        block = contract.block
        if not isinstance(block, GraphIR):
            raise TypeError("graph lowering requires GraphIR")
        plan = self._graph_plan(block, pointer)
        raw = plan.raw
        fragment = plan.fragment
        nodes = plan.nodes
        edges = plan.edges
        state = plan.state
        namespace = plan.namespace
        local_names = plan.local_names
        entry = plan.entry
        exits = plan.exits
        names = _FragmentNames(namespace, state)

        (
            fragment_state_keys,
            interrupt_outcomes,
            declared_writes,
        ) = self._install_fragment_nodes(plan, names, pointer)
        self._validate_fragment_effects(fragment, declared_writes)
        adjacency, edges_by_source = self._install_fragment_edges(plan, names)
        self._validate_fragment_edge_routes(edges_by_source, interrupt_outcomes)
        loop_limits, loop_exits, analysis_adjacency = (
            self._fragment_loop_analysis(plan, adjacency)
        )
        self._validate_fragment_effect_outcomes(
            plan, interrupt_outcomes, loop_exits
        )
        self._validate_fragment_topology(
            plan=plan,
            names=names,
            adjacency=adjacency,
            analysis_adjacency=analysis_adjacency,
            loop_limits=loop_limits,
            loop_exits=loop_exits,
        )
        return self._finish_graph_fragment(
            plan=plan,
            names=names,
            pointer=pointer,
            fragment_state_keys=fragment_state_keys,
        )

    def repeat(self, contract: RepeatContract, pointer: str) -> _Fragment:
        gate = self.node(pointer, "repeat", "attempt", {
            "type": "passthrough", "output": {"lockstep_continue": True}
        })
        exhausted = self.node(pointer, "repeat", "exhausted", {"type": "passthrough"})
        self.edge(exhausted, self.outcome_target("FAIL"))
        self.loop_limits[gate] = contract.limit
        self.loop_exits[gate] = exhausted
        blocks = list(contract.body.blocks)
        if not blocks:
            raise ValueError("repeat body must not be empty")
        fragments: list[_Fragment] = []
        for index, item in enumerate(blocks):
            item_pointer = f"{pointer}/repeat/do/{index}"
            failure = gate if index == len(blocks) - 1 else None
            if isinstance(item, RepeatContract):
                fragments.append(self.repeat(item, item_pointer))
            else:
                fragments.append(self.block(item, item_pointer, failure_target=failure))
        for left, right in zip(fragments, fragments[1:]):
            self.connect(left.exits, right.entry)
        self.edge(gate, fragments[0].entry, "lockstep_continue == true")
        return _Fragment(gate, fragments[-1].exits)

    def flow_contract(self, flow: FlowContract, pointer: str = "/flow") -> _Fragment:
        if not flow.blocks:
            empty = self.node(pointer, "flow", "empty", {"type": "passthrough"})
            return _Fragment(empty, [_Exit(empty)])
        fragments: list[_Fragment] = []
        for index, item in enumerate(flow.blocks):
            item_pointer = f"{pointer}/{index}" if pointer == "/flow" else f"{pointer}/{index}"
            fragments.append(self.repeat(item, item_pointer) if isinstance(item, RepeatContract) else self.block(item, item_pointer))
        for left, right in zip(fragments, fragments[1:]):
            self.connect(left.exits, right.entry)
        return _Fragment(fragments[0].entry, fragments[-1].exits)

    def build(self) -> tuple[
        dict[str, Any], dict[str, Any], tuple[LoweredGeneratedFile, ...],
        tuple[LoweredDependency, ...],
    ]:
        flow = self.flow_contract(self.validated.flow)
        self.edge("START", flow.entry)
        self.connect(flow.exits, self.terminals["PASS"])
        for terminal in self.terminals.values():
            self.edge(terminal, "END")
        source = f"../workflows/{self.workflow.name}.workflow.yaml"
        document: dict[str, Any] = {
            "version": "1.0", "name": self.workflow.name,
            "description": self.workflow.description,
            "x-lockstep-generated": {
                "schema": "lockstep.generated/v1", "compiler_version": "1",
                "workflow_version": self.workflow.version, "source": source,
                "source_sha256": self.workflow.source_sha256,
            },
            "state": self.state, "nodes": self.nodes, "edges": self.edges,
        }
        if self.loop_limits:
            document["loop_limits"] = self.loop_limits
            document["loop_exits"] = self.loop_exits
        source_map = {
            "schema": "lockstep.source-map/v1", "compiler_version": "1",
            "source": source, "nodes": self.source_nodes,
        }
        return (
            document, source_map, tuple(self.generated_files),
            tuple(self.dependencies),
        )


def lower_workflow(
    validated: ValidatedWorkflow, catalog: WorkflowCatalog | None = None
) -> tuple[
    dict[str, Any], dict[str, Any], tuple[LoweredGeneratedFile, ...],
    tuple[LoweredDependency, ...],
]:
    if not isinstance(validated, ValidatedWorkflow):
        raise TypeError("compile input must be a ValidatedWorkflow")
    return _Builder(validated, catalog).build()
