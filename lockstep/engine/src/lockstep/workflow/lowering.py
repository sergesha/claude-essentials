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


@dataclass
class _Exit:
    source: str
    condition: str | None = None


@dataclass
class _Fragment:
    entry: str
    exits: list[_Exit]


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

    def block(
        self, contract: BlockContract, pointer: str, *, failure_target: str | None = None
    ) -> _Fragment:
        block = contract.block
        retry_limit = contract.retry.limit if contract.retry else None
        if isinstance(block, StepIR):
            logical = block.id or block.step
            result_key = f"{logical.replace('-', '_')}_result"
            descriptor = {
                "schema": "lockstep.effect/v1", "kind": "manual", "logical_id": logical,
                "runner": None, "inputs": {}, "writes": list(block.writes), "artifacts": [],
                "deadline_seconds": None, "scope_state_keys": [],
                "result_schema": "lockstep.effect-result/v1",
            }
            message = {
                "step": block.step, "task": block.task, "exit_criterion": block.exit,
                "evidence_schema": plain(block.evidence) if block.evidence is not None else {},
                "artifact_contract": plain(block.artifact) if block.artifact is not None else {},
                "lockstep_effect": descriptor,
            }
            return self.descriptor_interrupt(pointer, "step", logical, descriptor, message, result_key, retry_limit, failure_target=failure_target)
        if isinstance(block, VerifyIR):
            logical = block.id or f"verify-{pointer.rsplit('/', 1)[-1]}"
            result_key = f"{logical.replace('-', '_')}_result"
            command_key = f"{logical.replace('-', '_')}_command"
            self.declare_generated_state(command_key, "dict")
            prepare = self.node(pointer, "verify", "command", {
                "type": "passthrough", "output": {command_key: {
                    "schema": "lockstep.pinned-command/v1",
                    "logical_argv": shlex.split(block.command),
                    "logical_cwd": block.cwd or ".", "result_source": "exit",
                }}
            })
            descriptor = {
                "schema": "lockstep.effect/v1", "kind": "verify", "logical_id": logical,
                "runner": {"selector": "pinned", "required_capabilities": ["workspace", "bounded_result", "sandbox"]},
                "inputs": {"command": {"state_key": command_key}, "snapshot": {"runtime_key": "current_project_snapshot"}},
                "writes": [], "artifacts": [], "deadline_seconds": block.timeout,
                "scope_state_keys": list(self.active_scope_state_keys),
                "result_schema": "lockstep.effect-result/v1",
            }
            effect = self.descriptor_interrupt(pointer, "verify", logical, descriptor, {"step": logical, "lockstep_effect": descriptor}, result_key, retry_limit, failure_target=failure_target)
            self.edge(prepare, effect.entry)
            return _Fragment(prepare, effect.exits)
        if isinstance(block, DecideIR):
            logical = block.id or "decision"
            result_key = f"{logical.replace('-', '_')}_result"
            using = plain(block.using)
            descriptor = {
                "schema": "lockstep.effect/v1", "kind": "decide", "logical_id": logical,
                "decision": {
                    "type": "changed-paths", "since": "start",
                    "cases": [{"label": label, "paths": list(paths)} for label, paths in using["cases"].items()],
                    "default": using["default"],
                },
                "inputs": {
                    "start_snapshot": {"runtime_key": "run_start_project_snapshot"},
                    "current_snapshot": {"runtime_key": "current_project_snapshot"},
                },
                "result_schema": "lockstep.decision-result/v1",
            }
            self.outcome_keys[logical] = result_key
            return self.descriptor_interrupt(pointer, "decide", logical, descriptor, {"step": logical, "lockstep_effect": descriptor}, result_key, None)
        if isinstance(block, AcceptIR):
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
        if isinstance(block, EscalateIR):
            return _Fragment(self.outcome_target("FAIL"), [])
        if isinstance(block, ChooseIR):
            result_key = self.outcome_keys.get(block.value, block.value.replace("-", "_") + "_result")
            router = self.node(pointer, "choose", "route", {"type": "passthrough"})
            join = self.node(pointer, "choose", "join", {"type": "passthrough"})
            for label, flow in block.cases.items():
                fragment = self.flow_contract(contract.branches[label], f"{pointer}/choose/cases/{label}")
                self.edge(router, fragment.entry, f"{result_key}.value == '{label}'")
                self.connect(fragment.exits, join)
            if block.default is not None and contract.default is not None:
                fragment = self.flow_contract(contract.default, f"{pointer}/choose/default")
                labels = list(block.cases)
                condition = " and ".join(f"{result_key}.value != '{label}'" for label in labels)
                self.edge(router, fragment.entry, condition)
                self.connect(fragment.exits, join)
            return _Fragment(router, [_Exit(join)])
        if isinstance(block, GraphIR):
            return self.graph(contract, pointer)
        if isinstance(block, CallIR):
            return self.call(contract, pointer)
        if isinstance(block, ParallelIR):
            return self.parallel(contract, pointer)
        raise NotImplementedError(f"Task 8 cannot lower {type(block).__name__}")

    def parallel(self, contract: BlockContract, pointer: str) -> _Fragment:
        block = contract.block
        if not isinstance(block, ParallelIR):
            raise TypeError("parallel lowering requires ParallelIR")
        if block.id is None or block.join != "all":
            raise ValueError("parallel lowering requires an id and join: all")

        outer_targets = dict(self.outcome_targets)
        outer_scopes = self.active_scope_state_keys
        outer_aborted_capture = self.capture_aborted_effects
        outer_parallel_branch = self.inside_parallel_branch
        digest = hashlib.sha256(
            b"lockstep.parallel-scope/v1\0" + pointer.encode("utf-8")
        ).hexdigest()[:24]
        scope_fragment: _Fragment | None = None
        branch_scopes = outer_scopes
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
            branch_scopes = (*outer_scopes, scope_key)

        fork = self.node(pointer, "parallel", "fork", {"type": "passthrough"})
        join = self.node(pointer, "parallel", "join", {"type": "passthrough"})
        result_key = f"{block.id.replace('-', '_')}_result"
        self.declare_generated_state(result_key, "dict")
        self.outcome_keys[block.id] = result_key

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

        self.edge(fork, branch_entries)
        if scope_fragment is None:
            entry = fork
        else:
            self.connect(scope_fragment.exits, fork)
            entry = scope_fragment.entry

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
                self.edge(
                    route,
                    outcomes[precedence],
                    f"{branch_key} == '{precedence}'",
                )
                self.edge(
                    route,
                    next_route,
                    f"{branch_key} != '{precedence}'",
                )
                route = next_route
        self.edge(route, outcomes["PASS"])
        for outcome in ("FAIL", "ERROR", "ABORTED"):
            self.edge(outcomes[outcome], outer_targets[outcome])
        return _Fragment(entry, [_Exit(outcomes["PASS"])])

    def call(self, contract: BlockContract, pointer: str) -> _Fragment:
        block = contract.block
        if not isinstance(block, CallIR):
            raise TypeError("call lowering requires CallIR")
        if self.catalog is None:
            raise ValueError("call lowering requires a resolved catalog")
        resolver = getattr(self.catalog, "child_for", None)
        resolved = resolver(block.workflow) if callable(resolver) else None
        if resolved is None:
            raise ValueError(
                f"resolved compiled child is unavailable for {block.workflow!r}"
            )
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
        self.declare_generated_state(child_outcome, "str")
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
        self.declare_generated_state("current_step", "str")
        self.declare_generated_state("_loop_counts", "dict")
        self.declare_generated_state("_loop_limit_reached", "bool")
        self.declare_generated_state(saved_context["current_step"], "any")
        self.declare_generated_state(saved_context["_loop_counts"], "dict")
        self.declare_generated_state(saved_context["_loop_limit_reached"], "any")
        child_contract = resolved.contract
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
        producer_bindings = self._artifact_producers(resolved, artifact_specs)
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
        scope = self.descriptor_interrupt(
            pointer,
            "call",
            f"call-{call_digest}-scope",
            descriptor,
            {"step": block.id or block.workflow, "lockstep_effect": descriptor},
            scope_key,
            None,
        )
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
        pre = self.node(pointer, "call", "pre", {
            "type": "passthrough", "output": pre_output,
        })
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
            specialized_state = specialized.get("state", {})
            for specialized_node in specialized.get("nodes", {}).values():
                if not isinstance(specialized_node, dict):
                    continue
                for field in ("state_key", "resume_key"):
                    state_key = specialized_node.get(field)
                    if isinstance(state_key, str):
                        if state_key not in self.state:
                            self.declare_generated_state(
                                state_key, specialized_state.get(state_key, "dict")
                            )
                message = specialized_node.get("message")
                effect = (
                    message.get("lockstep_effect")
                    if isinstance(message, dict) else None
                )
                inputs = effect.get("inputs") if isinstance(effect, dict) else None
                if isinstance(inputs, dict):
                    for selector in inputs.values():
                        state_key = (
                            selector.get("state_key")
                            if isinstance(selector, dict) else None
                        )
                        if isinstance(state_key, str):
                            if state_key not in self.state:
                                self.declare_generated_state(
                                    state_key, specialized_state.get(state_key, "any")
                                )
                if isinstance(effect, dict):
                    shared_keys = []
                    for field in (
                        "scope_state_keys", "ancestor_deadline_state_keys",
                    ):
                        values = effect.get(field)
                        if isinstance(values, list):
                            shared_keys.extend(
                                key for key in values if isinstance(key, str)
                            )
                    result_state_key = effect.get("result_state_key")
                    if isinstance(result_state_key, str):
                        shared_keys.append(result_state_key)
                    for state_key in shared_keys:
                        if state_key not in self.state:
                            self.declare_generated_state(
                                state_key,
                                specialized_state.get(state_key, "dict"),
                            )
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
        child_bundle_digest = hashlib.sha256(
            b"lockstep.compiled-bundle/v1\0"
        )
        child_bundle_digest.update(generated_path.encode("utf-8"))
        child_bundle_digest.update(b"\0")
        for member_path, member_bytes in sorted(specialized_members):
            child_bundle_digest.update(member_path.encode("utf-8"))
            child_bundle_digest.update(b"\0")
            child_bundle_digest.update(
                hashlib.sha256(member_bytes).hexdigest().encode("ascii")
            )
            child_bundle_digest.update(b"\0")
        self.dependencies.append(
            LoweredDependency(
                "workflow",
                block.workflow,
                pointer,
                resolved.source_definition_sha256,
                child_bundle_digest.hexdigest(),
                generated_path,
            )
        )
        specialized_by_source = {
            source_file.relative_path: (target_path, specialized_bytes)
            for source_file, (target_path, specialized_bytes) in zip(
                resolved.standalone.files, specialized_members, strict=True
            )
        }
        for dependency in resolved.standalone.dependencies:
            rebased_root = None
            compiled_sha256 = dependency.compiled_sha256
            if dependency.generated_root is not None:
                rebased_root = specialized_by_source[dependency.generated_root][0]
                reachable = {dependency.generated_root}
                pending = [dependency.generated_root]
                while pending:
                    current = pending.pop()
                    current_document = yaml.safe_load(
                        specialized_by_source[current][1]
                    )
                    nodes = (
                        current_document.get("nodes", {})
                        if isinstance(current_document, dict)
                        else {}
                    )
                    for node in nodes.values() if isinstance(nodes, dict) else ():
                        graph = node.get("graph") if isinstance(node, dict) else None
                        if not isinstance(graph, str):
                            continue
                        child_source = (
                            PurePosixPath(current).parent / graph
                        ).as_posix()
                        if child_source not in specialized_by_source:
                            raise ValueError(
                                "compiled child dependency graph references an unknown member"
                            )
                        if child_source not in reachable:
                            reachable.add(child_source)
                            pending.append(child_source)
                nested_digest = hashlib.sha256(b"lockstep.compiled-bundle/v1\0")
                nested_digest.update(rebased_root.encode("utf-8"))
                nested_digest.update(b"\0")
                for source_path in sorted(reachable):
                    member_path, member_bytes = specialized_by_source[source_path]
                    nested_digest.update(member_path.encode("utf-8"))
                    nested_digest.update(b"\0")
                    nested_digest.update(
                        hashlib.sha256(member_bytes).hexdigest().encode("ascii")
                    )
                    nested_digest.update(b"\0")
                compiled_sha256 = nested_digest.hexdigest()
            elif dependency.kind == "fragment":
                transformed_fragment_digest = None
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
                    transformed_fragment_digest = _specialized_fragment_digest(
                        original_document,
                        specialized_document,
                        dependency.compiled_sha256,
                        namespace,
                    )
                    if transformed_fragment_digest is not None:
                        break
                if transformed_fragment_digest is None:
                    raise ValueError(
                        "compiled child fragment dependency projection is unavailable"
                    )
                compiled_sha256 = transformed_fragment_digest
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
        child = self.node(pointer, "call", "direct", {
            "type": "subgraph", "graph": generated_path, "mode": "direct",
        })
        post_output = {
            key: f"{{state.{namespace}_{key}}}"
            for key in child_contract.state_exports
        }
        for qualified, (
            _declared_name,
            _source,
            _media_type,
            _producer_logical_id,
            _producer_result_state_key,
        ) in artifact_specs.items():
            channel, _name = self.artifact_state_keys[qualified]
            producer = next(
                item
                for items in producer_bindings.values()
                for item in items
                if item[0] == qualified
            )
            post_output[channel] = (
                f"{{state.{_specialized_state_key(namespace, producer[4])}}}"
            )
        restoration_output = {
            "current_step": f"{{state.{saved_context['current_step']}}}",
            "_loop_counts": f"{{state.{saved_context['_loop_counts']}}}",
            "_loop_limit_reached": (
                f"{{state.{saved_context['_loop_limit_reached']}}}"
            ),
        }
        post = self.node(pointer, "call", "post", {
            "type": "passthrough", "output": post_output,
        })
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
        if selected_file.relative_path == resolved.standalone.root_relative_path:
            for key, state_type in {
                **dict(resolved.contract.state_inputs),
                **dict(resolved.contract.state_exports),
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
        def specialized_key(key: str) -> str:
            return _specialized_state_key(namespace, key)

        key_map = {key: specialized_key(key) for key in tuple(state)}
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
        child_contract = resolved.contract
        for key in {*child_contract.state_inputs, *child_contract.state_exports}:
            key_map[key] = specialized_key(key)
        new_state = {key_map[key]: value for key, value in state.items()}
        new_state[scope_key] = "dict"
        for key, state_type in {
            **dict(child_contract.state_inputs),
            **dict(child_contract.state_exports),
        }.items():
            new_state[key_map[key]] = state_type
        document["state"] = new_state
        nodes = document.get("nodes", {})
        if not isinstance(nodes, dict):
            raise ValueError("resolved child nodes must be a mapping")
        node_map = {name: f"{namespace}.{name}" for name in nodes}

        def rewrite_state_template(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: rewrite_state_template(item) for key, item in value.items()
                }
            if isinstance(value, list):
                return [rewrite_state_template(item) for item in value]
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

        def rewrite_state_condition(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            return _rewrite_condition_references(value, key_map)

        rewritten_nodes: dict[str, dict[str, Any]] = {}
        for name, raw_node in nodes.items():
            if not isinstance(raw_node, dict):
                raise ValueError("resolved child node must be a mapping")
            node = plain(raw_node)
            output = node.get("output")
            if isinstance(output, dict):
                node["output"] = {
                    key_map.get(key, key): rewrite_state_template(value)
                    for key, value in output.items()
                }
            for field in ("state_key", "resume_key"):
                value = node.get(field)
                if isinstance(value, str):
                    node[field] = key_map.get(value, specialized_key(value))
                    new_state.setdefault(node[field], "dict")
            message = node.get("message")
            descriptor = message.get("lockstep_effect") if isinstance(message, dict) else None
            if isinstance(descriptor, dict):
                descriptor = plain(descriptor)
                if self.inside_parallel_branch and descriptor.get("kind") == "decide":
                    raise ValueError(
                        "parallel child may not hide a decision descriptor"
                    )
                matching_artifacts = [
                    item for item in artifact_bindings
                    if key_map.get(item[4], specialized_key(item[4]))
                    == node.get("resume_key")
                    and descriptor.get("logical_id") == item[5]
                ]
                if matching_artifacts:
                    # The immutable child descriptor already carries the full
                    # ordered declaration set. Contract matching selects refs;
                    # specialization must never rewrite or drop declarations.
                    message["artifact_contract"] = {}
                if isinstance(descriptor.get("logical_id"), str):
                    logical_digest = hashlib.sha256(
                        b"lockstep.specialized-logical-id/v1\0"
                        + namespace.encode("ascii")
                        + b"\0"
                        + descriptor["logical_id"].encode("utf-8")
                    ).hexdigest()
                    descriptor["logical_id"] = f"child-{logical_digest}"
                if descriptor.get("kind") == "manual" and descriptor.get("runner") is None:
                    descriptor["kind"] = "managed"
                    descriptor["runner"] = {
                        "selector": runner,
                        "required_capabilities": [
                            "workspace", "bounded_result", "sandbox",
                        ],
                    }
                    descriptor["scope_state_keys"] = [scope_key]
                elif isinstance(descriptor.get("scope_state_keys"), list):
                    mapped_scopes = [
                        key_map.get(key, key)
                        for key in descriptor["scope_state_keys"]
                    ]
                    descriptor["scope_state_keys"] = (
                        mapped_scopes if mapped_scopes else [scope_key]
                    )
                inputs = descriptor.get("inputs")
                if isinstance(inputs, dict):
                    for selector in inputs.values():
                        if isinstance(selector, dict) and isinstance(selector.get("state_key"), str):
                            selector["state_key"] = key_map.get(
                                selector["state_key"], specialized_key(selector["state_key"])
                            )
                if descriptor.get("kind") == "scope":
                    ancestors = [
                        key_map.get(key, key)
                        for key in descriptor.get("ancestor_deadline_state_keys", [])
                    ]
                    descriptor["ancestor_deadline_state_keys"] = [scope_key, *ancestors]
                    result_key = descriptor.get("result_state_key")
                    if isinstance(result_key, str):
                        descriptor["result_state_key"] = key_map.get(
                            result_key, specialized_key(result_key)
                        )
                message["lockstep_effect"] = descriptor
                parse_effect_descriptor(descriptor, known_state_keys=set(new_state))
            if isinstance(message, dict):
                for message_key, message_value in tuple(message.items()):
                    if message_key != "lockstep_effect":
                        message[message_key] = rewrite_state_template(message_value)
            rewritten_nodes[node_map[name]] = node
        document["nodes"] = rewritten_nodes
        rewritten_edges: list[dict[str, Any]] = []
        for raw_edge in document.get("edges", []):
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
                edge["condition"] = rewrite_state_condition(edge["condition"])
            rewritten_edges.append(edge)
        document["edges"] = rewritten_edges
        for field in ("loop_limits", "loop_exits"):
            raw = document.get(field)
            if isinstance(raw, dict):
                document[field] = {
                    node_map.get(key, key): node_map.get(value, value)
                    if field == "loop_exits" else value
                    for key, value in raw.items()
                }
        document["name"] = f"{document.get('name', resolved.logical_name)}-{namespace}"
        return document

    def graph(self, contract: BlockContract, pointer: str) -> _Fragment:
        block = contract.block
        if not isinstance(block, GraphIR):
            raise TypeError("graph lowering requires GraphIR")
        if block.kind == "inline":
            inline_document = plain(block.graph or {})
            inline_document.pop("id", None)
            parsed_fragment = FragmentIR.parse(inline_document)
            raw = plain(parsed_fragment.document)
            source_definition_sha256 = hashlib.sha256(
                canonical_yaml(raw)
            ).hexdigest()
            logical_name = f"inline:{pointer}"
        else:
            if self.catalog is None:
                raise ValueError("include_graph lowering requires a resolved catalog")
            resolver = getattr(self.catalog, "fragment_for", None)
            resolved = resolver(block.path) if callable(resolver) else None
            if resolved is None:
                raise ValueError(f"resolved fragment is unavailable for {block.path!r}")
            raw = plain(resolved.fragment.document)
            source_definition_sha256 = resolved.source_definition_sha256
            logical_name = resolved.logical_path
        fragment = raw.get("fragment")
        nodes = raw.get("nodes")
        edges = raw.get("edges")
        state = raw.get("state", {})
        if not isinstance(fragment, dict) or not isinstance(nodes, dict) or not isinstance(edges, list):
            raise ValueError("invalid closed graph fragment")
        namespace = block.id or _stable_id(pointer, "graph", "namespace")
        local_names = set(nodes)
        if len(local_names) > 1_000:
            raise ValueError("graph fragment exceeds the 1000-node expansion cap")
        if not local_names or any(not isinstance(name, str) or not name for name in local_names):
            raise ValueError("graph fragment nodes must be a non-empty string mapping")
        entry = fragment.get("entry")
        exits = fragment.get("exits")
        if entry not in local_names or not isinstance(exits, dict) or "pass" not in exits:
            raise ValueError("graph fragment requires an existing entry and pass exit")
        if not exits or set(exits) - {"pass", "fail", "error"}:
            raise ValueError("graph fragment exits are not closed")
        if any(target not in local_names for target in exits.values()):
            raise ValueError("graph fragment exit targets must exist")

        def qualify(name: str) -> str:
            return f"{namespace}.{name}"

        state_namespace = _fragment_state_namespace(namespace)

        def qualify_state(name: str) -> str:
            return f"fragment_{state_namespace}_{name}"

        def qualify_identity(kind: str, name: str) -> str:
            digest = hashlib.sha256(
                b"lockstep.fragment-identity/v1\0"
                + kind.encode("ascii")
                + b"\0"
                + namespace.encode("utf-8")
                + b"\0"
                + name.encode("utf-8")
            ).hexdigest()
            return f"fragment-{kind}-{digest}"

        def rewrite_fragment_template(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: rewrite_fragment_template(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [rewrite_fragment_template(item) for item in value]
            if not isinstance(value, str):
                return value
            referenced = re.findall(
                r"\{state\.([A-Za-z_][A-Za-z0-9_]*)", value
            )
            unknown = set(referenced) - set(state)
            if unknown:
                raise ValueError(
                    f"fragment template references unknown state: {sorted(unknown)}"
                )
            rewritten = value
            for local_key in state:
                rewritten = rewritten.replace(
                    f"{{state.{local_key}", f"{{state.{qualify_state(local_key)}"
                )
            return rewritten

        def rewrite_fragment_condition(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            return _rewrite_condition_references(
                value,
                {local_key: qualify_state(local_key) for local_key in state},
                reject_unknown=True,
            )

        fragment_state_keys: set[str] = set()
        protected_resume_keys = {
            node.get("resume_key")
            for node in nodes.values()
            if isinstance(node, dict) and node.get("type") == "interrupt"
        }
        for key, state_type in state.items():
            qualified = qualify_state(key)
            if qualified in self.state:
                raise ValueError(f"fragment generated state collision: {qualified}")
            self.declare_generated_state(qualified, state_type)
            fragment_state_keys.add(qualified)
        declared_writes: list[str] = []
        interrupt_outcomes: dict[str, tuple[str, tuple[str, ...]]] = {}
        for name, node in nodes.items():
            if not isinstance(node, dict) or node.get("type") not in {
                "passthrough", "interrupt",
            }:
                raise ValueError(
                    "generated graph fragments may contain only passthrough and protected interrupt nodes"
                )
            copied = plain(node)
            if copied.get("type") == "interrupt":
                message = copied.get("message")
                descriptor = (
                    message.get("lockstep_effect")
                    if isinstance(message, dict) else None
                )
                if not isinstance(descriptor, dict):
                    raise ValueError("fragment interrupts must carry a protected descriptor")
                for field in ("state_key", "resume_key"):
                    value = copied.get(field)
                    if not isinstance(value, str) or not value:
                        raise ValueError(f"fragment interrupt requires {field}")
                    copied[field] = qualify_state(value)
                    if copied[field] not in self.state:
                        self.declare_generated_state(copied[field], "dict")
                    fragment_state_keys.add(copied[field])
                descriptor = plain(descriptor)
                if self.inside_parallel_branch and descriptor.get("kind") == "decide":
                    raise ValueError(
                        "parallel graph may not hide a decision descriptor"
                    )
                logical_id = descriptor.get("logical_id")
                if isinstance(logical_id, str):
                    descriptor["logical_id"] = qualify_identity(
                        "effect", logical_id
                    )
                inputs = descriptor.get("inputs")
                if isinstance(inputs, dict):
                    for selector in inputs.values():
                        if isinstance(selector, dict) and isinstance(
                            selector.get("state_key"), str
                        ):
                            selector["state_key"] = qualify_state(selector["state_key"])
                for field in ("scope_state_keys", "ancestor_deadline_state_keys"):
                    if isinstance(descriptor.get(field), list):
                        descriptor[field] = [qualify_state(key) for key in descriptor[field]]
                if isinstance(descriptor.get("result_state_key"), str):
                    descriptor["result_state_key"] = qualify_state(
                        descriptor["result_state_key"]
                    )
                artifacts = descriptor.get("artifacts")
                if isinstance(artifacts, list):
                    for artifact in artifacts:
                        if isinstance(artifact, dict) and isinstance(
                            artifact.get("name"), str
                        ):
                            artifact["name"] = qualify_identity(
                                "artifact", artifact["name"]
                            )
                if isinstance(descriptor.get("artifact_handle"), str):
                    descriptor["artifact_handle"] = qualify_identity(
                        "artifact", descriptor["artifact_handle"]
                    )
                if isinstance(message.get("step"), str):
                    message["step"] = qualify_identity("step", message["step"])
                if self.active_scope_state_keys:
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
                artifact_contract = message.get("artifact_contract")
                if artifact_contract not in (None, [], {}):
                    raise ValueError(
                        "fragment artifact contracts must use protected descriptor artifacts"
                    )
                for message_key, message_value in tuple(message.items()):
                    if message_key != "lockstep_effect":
                        message[message_key] = rewrite_fragment_template(
                            message_value
                        )
                message["lockstep_effect"] = descriptor
                parsed = parse_effect_descriptor(
                    descriptor, known_state_keys=set(self.state)
                )
                if isinstance(parsed, EffectDescriptor):
                    outcomes = ("pass", "fail", "error")
                elif isinstance(parsed, (ScopeDescriptor, DecisionDescriptor)):
                    outcomes = ("pass", "error")
                elif isinstance(parsed, AcceptDescriptor):
                    outcomes = ("pass",)
                else:  # pragma: no cover - parser union is intentionally closed
                    raise TypeError("unknown protected fragment descriptor")
                interrupt_outcomes[name] = (node["resume_key"], outcomes)
                for write in getattr(parsed, "writes", ()):
                    if write not in declared_writes:
                        declared_writes.append(write)
            output = copied.get("output")
            if isinstance(output, dict):
                overwritten_results = set(output) & protected_resume_keys
                if overwritten_results:
                    raise ValueError(
                        "fragment passthrough may not overwrite protected result "
                        f"channels: {sorted(overwritten_results)}"
                    )
                unknown_outputs = set(output) - set(state)
                if unknown_outputs:
                    raise ValueError(
                        f"fragment output writes undeclared state: {sorted(unknown_outputs)}"
                    )
                copied["output"] = {
                    qualify_state(key): rewrite_fragment_template(value)
                    for key, value in output.items()
                }
            qualified = qualify(name)
            if qualified in self.nodes:
                raise ValueError(f"fragment node collision: {qualified}")
            self.nodes[qualified] = copied
            mark = self.workflow.location_for(pointer)
            self.source_nodes[qualified] = {
                "pointer": pointer,
                "line": mark.line if mark else 1,
                "column": mark.column if mark else 1,
            }
        effects = fragment.get("effects", {})
        mode = effects.get("mode") if isinstance(effects, dict) else None
        expected_writes = list(effects.get("writes", [])) if isinstance(effects, dict) else []
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
        adjacency: dict[str, set[str]] = {name: set() for name in local_names}
        edges_by_source: dict[str, list[dict[str, Any]]] = {
            name: [] for name in local_names
        }
        for edge in edges:
            if not isinstance(edge, dict) or set(edge) - {"from", "to", "condition"}:
                raise ValueError("invalid graph fragment edge")
            source, target = edge.get("from"), edge.get("to")
            if source not in local_names or target not in local_names:
                raise ValueError("graph fragment edges must remain inside the fragment")
            adjacency[source].add(target)
            edges_by_source[source].append(edge)
            self.edge(
                qualify(source), qualify(target),
                rewrite_fragment_condition(edge.get("condition")),
            )

        def conditions_are_exhaustive(
            source: str, conditions: list[str]
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
                if match is None:
                    continue
                comparisons.append(match.groups())
            complements = {
                "==": "!=", "!=": "==",
            }
            return any(
                left == other_left
                and value == other_value
                and operator in complements
                and complements[operator] == other_operator
                for left, operator, value in comparisons
                for other_left, other_operator, other_value in comparisons
            )

        for source, outgoing in edges_by_source.items():
            conditional = [
                edge for edge in outgoing if isinstance(edge.get("condition"), str)
            ]
            if not conditional:
                continue
            if len(conditional) != len(outgoing):
                raise ValueError(
                    "fragment nodes may not mix conditional and unconditional edges"
                )
            if conditions_are_exhaustive(
                source, [edge["condition"] for edge in conditional]
            ):
                continue
            raise ValueError(
                "fragment conditional routing must be proven exhaustive"
            )
        loop_limits = raw.get("loop_limits", {})
        loop_exits = raw.get("loop_exits", {})
        if not isinstance(loop_limits, dict) or not isinstance(loop_exits, dict):
            raise ValueError("fragment loop metadata must be mappings")
        if set(loop_limits) != set(loop_exits):
            raise ValueError("fragment loop limits and exits must name the same nodes")
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
            if local_name not in local_names or exit_target not in local_names:
                raise ValueError("fragment loop exit references an unknown node")
            if exit_target not in exits.values():
                raise ValueError("fragment loop exit must target a declared local exit")
            analysis_adjacency[local_name].add(exit_target)
        edge_records = [edge for edge in edges if isinstance(edge, dict)]
        for interrupt_name, (resume_key, outcomes) in interrupt_outcomes.items():
            for outcome_name in outcomes:
                if outcome_name not in exits:
                    raise ValueError(
                        f"fallible fragment effect requires a declared {outcome_name} exit"
                    )
                reachable_for_outcome: set[str] = set()
                pending_for_outcome = [interrupt_name]
                reached_successor_effect = False
                while pending_for_outcome:
                    current = pending_for_outcome.pop()
                    if current in reachable_for_outcome:
                        continue
                    reachable_for_outcome.add(current)
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
                            "protected fragment effect routing must select exactly "
                            "one successor for every outcome"
                        )
                    pending_for_outcome.extend(possible_targets)
                reached_exits = {
                    name for name, target in exits.items()
                    if target in reachable_for_outcome
                }
                valid = (
                    outcome_name == "pass"
                    and (
                        reached_successor_effect
                        and not reached_exits
                        or not reached_successor_effect
                        and reached_exits == {"pass"}
                    )
                    or outcome_name != "pass"
                    and not reached_successor_effect
                    and reached_exits == {outcome_name}
                )
                if not valid:
                    raise ValueError(
                        "protected fragment effect outcomes must reach only their "
                        "matching declared exit"
                    )
        reachable: set[str] = set()
        frontier = [entry]
        while frontier:
            current = frontier.pop()
            if current in reachable:
                continue
            reachable.add(current)
            frontier.extend(analysis_adjacency[current])
        if any(target not in reachable for target in exits.values()):
            raise ValueError("every declared graph fragment exit must be reachable")
        if reachable != local_names:
            raise ValueError("graph fragment may not contain unreachable nodes")
        reverse: dict[str, set[str]] = {name: set() for name in local_names}
        for source, targets in analysis_adjacency.items():
            for target in targets:
                reverse[target].add(source)
        can_terminate: set[str] = set()
        frontier = list(exits.values())
        while frontier:
            current = frontier.pop()
            if current in can_terminate:
                continue
            can_terminate.add(current)
            frontier.extend(reverse[current])
        if reachable - can_terminate:
            raise ValueError("every reachable fragment path must be able to terminate")
        visiting: set[str] = set()
        visited: set[str] = set()

        def reject_cycle(name: str) -> None:
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
                reject_cycle(target)
            visiting.remove(name)
            visited.add(name)

        reject_cycle(entry)
        for local_name, limit in loop_limits.items():
            if local_name not in local_names:
                raise ValueError("fragment loop limit references an unknown node")
            self.loop_limits[qualify(local_name)] = limit
        for local_name, exit_target in loop_exits.items():
            if local_name not in local_names or exit_target not in local_names:
                raise ValueError("fragment loop exit references an unknown node")
            self.loop_exits[qualify(local_name)] = qualify(exit_target)
        terminal_names = {name for name in reachable if not adjacency[name]}
        if terminal_names - set(exits.values()):
            raise ValueError("every reachable graph fragment path must end at an exit")
        if any(adjacency[target] for target in exits.values()):
            raise ValueError("graph fragment exit nodes may not have outgoing edges")
        entry_gate = self.node(
            pointer, "graph", "entry", {"type": "passthrough"}
        )
        pass_gate = self.node(pointer, "graph", "pass", {"type": "passthrough"})
        self.edge(entry_gate, qualify(entry))
        self.edge(qualify(exits["pass"]), pass_gate)
        if "fail" in exits:
            self.edge(qualify(exits["fail"]), self.outcome_target("FAIL"))
        if "error" in exits:
            self.edge(qualify(exits["error"]), self.outcome_target("ERROR"))
        expansion = canonical_yaml({
            "state": {
                key: self.state[key]
                for key in self.state
                if key in fragment_state_keys
            },
            "nodes": {key: self.nodes[key] for key in self.nodes if key.startswith(namespace + ".")},
            "edges": [
                edge for edge in self.edges
                if str(edge.get("from", "")).startswith(namespace + ".")
                or str(edge.get("to", "")).startswith(namespace + ".")
            ],
        })
        self.dependencies.append(
            LoweredDependency(
                "fragment", logical_name, pointer, source_definition_sha256,
                hashlib.sha256(expansion).hexdigest(), None,
            )
        )
        return _Fragment(entry_gate, [_Exit(pass_gate)])

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
