"""Pure lowering from validated Workflow DSL contracts to yamlgraph data."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import shlex
from typing import Any

from lockstep.runtime.effects.descriptors import parse_effect_descriptor

from .canonical import plain
from .ir import AcceptIR, ChooseIR, DecideIR, EscalateIR, StepIR, VerifyIR
from .semantics import BlockContract, FlowContract, RepeatContract, ValidatedWorkflow


def _stable_id(pointer: str, kind: str, role: str) -> str:
    digest = hashlib.sha256(
        b"lockstep.workflow-node/v1\0"
        + pointer.encode("utf-8") + b"\0" + kind.encode("ascii") + b"\0" + role.encode("ascii")
    ).hexdigest()[:12]
    stem = pointer.rsplit("/", 1)[-1] or "root"
    return f"{kind}-{stem}-{role}-{digest}"


def lower_accept_descriptor(logical_id: str, artifact_handle: str) -> dict[str, Any]:
    descriptor = {
        "schema": "lockstep.effect/v1",
        "kind": "accept",
        "logical_id": logical_id,
        "artifact_handle": artifact_handle,
        "verdict": "PASS",
        "result_schema": "lockstep.acceptance-result/v1",
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
    def __init__(self, validated: ValidatedWorkflow) -> None:
        self.validated = validated
        self.workflow = validated.workflow
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.state: dict[str, str] = {"lockstep_outcome": "str"}
        self.state["lockstep_continue"] = "bool"
        self.loop_limits: dict[str, int] = {}
        self.loop_exits: dict[str, str] = {}
        self.source_nodes: dict[str, dict[str, int | str]] = {}
        self.outcome_keys: dict[str, str] = {}
        self.terminals = {
            outcome: self.node("/terminal", "terminal", outcome.lower(), {
                "type": "passthrough", "output": {"lockstep_outcome": outcome}
            })
            for outcome in ("PASS", "FAIL", "ERROR", "ABORTED")
        }

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

    def edge(self, source: str, target: str, condition: str | None = None) -> None:
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
        self.state[request_key] = "dict"
        self.state[result_key] = "dict"
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
            self.edge(exhausted, self.terminals["FAIL"])
            self.loop_limits[retry_gate] = retry_limit
            self.loop_exits[retry_gate] = exhausted
            entry = retry_gate
        self.edge(interrupt, self.terminals["ABORTED"], f"{result_key}.fixed_error_code == 'cancelled'")
        self.edge(interrupt, self.terminals["ERROR"], f"{result_key}.outcome == 'ERROR'")
        fail_target = retry_gate or failure_target or self.terminals["FAIL"]
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
            self.state[command_key] = "dict"
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
                "scope_state_keys": [], "result_schema": "lockstep.effect-result/v1",
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
            descriptor = lower_accept_descriptor(logical, block.artifact_from)
            return self.descriptor_interrupt(pointer, "accept", logical, descriptor, {"step": logical, "lockstep_effect": descriptor}, result_key, None)
        if isinstance(block, EscalateIR):
            return _Fragment(self.terminals["FAIL"], [])
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
        raise NotImplementedError(f"Task 8 cannot lower {type(block).__name__}")

    def repeat(self, contract: RepeatContract, pointer: str) -> _Fragment:
        gate = self.node(pointer, "repeat", "attempt", {
            "type": "passthrough", "output": {"lockstep_continue": True}
        })
        exhausted = self.node(pointer, "repeat", "exhausted", {"type": "passthrough"})
        self.edge(exhausted, self.terminals["FAIL"])
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

    def build(self) -> tuple[dict[str, Any], dict[str, Any]]:
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
        return document, source_map


def lower_workflow(validated: ValidatedWorkflow) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(validated, ValidatedWorkflow):
        raise TypeError("compile input must be a ValidatedWorkflow")
    return _Builder(validated).build()
