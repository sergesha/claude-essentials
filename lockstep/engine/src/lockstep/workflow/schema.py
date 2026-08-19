"""Marked, alias-free YAML loading and the Workflow DSL's v1 schema."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, NoReturn

import yaml
from yaml.events import AliasEvent, MappingEndEvent, SequenceEndEvent
from yaml.nodes import MappingNode, Node, SequenceNode

from .diagnostics import Diagnostic, DiagnosticError
from .ir import (
    AcceptIR, BlockIR, CallIR, ChooseIR, DecideIR, EscalateIR, GraphIR,
    ParallelIR, RepeatIR, RetryIR, StepIR, VerifyIR, WorkflowDefaultsIR, WorkflowIR,
)


_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_WORKFLOW_SUFFIX = ".workflow.yaml"
_BLOCKS = frozenset({"step", "verify", "decide", "choose", "repeat", "call", "accept", "parallel", "graph", "include_graph", "escalate"})
_V2_KEYS = frozenset({
    "goto", "race", "cancel", "cancel_on_failure", "fail_fast", "speculative",
    "cleanup_deadline", "quorum", "weighted_quorum", "first_success", "first_terminal",
    "dynamic_branches", "branch_map", "map", "cross_machine", "patch", "patch_export",
    "merge", "merge_order", "conflict_resolution", "checkpoint", "resume", "migrate",
    "remote_heartbeat", "remote_lease", "artifact_store", "template", "templates",
    "template_registry", "plugin", "plugins", "runtime_compilation",
})


@dataclass(frozen=True)
class SourceMark:
    line: int
    column: int


@dataclass(frozen=True)
class MarkedDocument:
    path: Path
    data: Any
    marks: Mapping[str, SourceMark]

    def mark_for(self, pointer: str) -> SourceMark | None:
        current = pointer
        while current not in self.marks and current:
            current = current.rsplit("/", 1)[0]
        return self.marks.get(current) or self.marks.get("")


class _MarkedYamlError(Exception):
    def __init__(self, code: str, message: str, pointer: str, mark: Any) -> None:
        self.code, self.message, self.pointer, self.mark = code, message, pointer, mark


class _MarkedSafeLoader(yaml.SafeLoader):
    yaml_implicit_resolvers = {
        initial: [entry for entry in entries if entry[0] != "tag:yaml.org,2002:bool"]
        for initial, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    yaml_implicit_resolvers.setdefault("t", []).append(("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")))
    yaml_implicit_resolvers.setdefault("T", []).append(("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")))
    yaml_implicit_resolvers.setdefault("f", []).append(("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")))
    yaml_implicit_resolvers.setdefault("F", []).append(("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")))

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._node_pointers: dict[int, str] = {}

    def _pointer_for(self, parent: Node | None, index: Any) -> str:
        if parent is None:
            return ""
        parent_pointer = self._node_pointers[id(parent)]
        if isinstance(parent, MappingNode) and isinstance(index, yaml.ScalarNode):
            return f"{parent_pointer}/{_escape(index.value)}"
        if isinstance(parent, SequenceNode) and isinstance(index, int):
            return f"{parent_pointer}/{index}"
        return parent_pointer

    def compose_node(self, parent: Node | None, index: Any) -> Node:
        previous = getattr(self, "_next_pointer", "")
        self._next_pointer = self._pointer_for(parent, index)
        try:
            if self.check_event(AliasEvent):
                event = self.get_event()
                raise _MarkedYamlError("LSW102", "YAML aliases are not allowed", self._next_pointer, event.start_mark)
            return super().compose_node(parent, index)
        finally:
            self._next_pointer = previous

    def compose_mapping_node(self, anchor: str | None) -> MappingNode:
        start = self.get_event()
        tag = start.tag or self.resolve(MappingNode, None, start.implicit)
        node = MappingNode(tag, [], start.start_mark, None, flow_style=start.flow_style)
        self._node_pointers[id(node)] = self._pointer_for_current(node)
        if anchor is not None:
            self.anchors[anchor] = node
        while not self.check_event(MappingEndEvent):
            key = self.compose_node(node, None)
            value = self.compose_node(node, key)
            node.value.append((key, value))
        node.end_mark = self.get_event().end_mark
        return node

    def _pointer_for_current(self, node: Node) -> str:
        # compose_node assigns this transient parent/index context immediately before dispatch.
        return getattr(self, "_next_pointer", "")

    def compose_sequence_node(self, anchor: str | None) -> SequenceNode:
        start = self.get_event()
        tag = start.tag or self.resolve(SequenceNode, None, start.implicit)
        node = SequenceNode(tag, [], start.start_mark, None, flow_style=start.flow_style)
        self._node_pointers[id(node)] = self._pointer_for_current(node)
        if anchor is not None:
            self.anchors[anchor] = node
        index = 0
        while not self.check_event(SequenceEndEvent):
            node.value.append(self.compose_node(node, index))
            index += 1
        node.end_mark = self.get_event().end_mark
        return node


def _escape(pointer_part: str) -> str:
    return pointer_part.replace("~", "~0").replace("/", "~1")


def _source_mark(mark: Any) -> SourceMark:
    return SourceMark(mark.line + 1, mark.column + 1)


def _collect_marks(node: Node, pointer: str, marks: dict[str, SourceMark]) -> None:
    marks[pointer] = _source_mark(node.start_mark)
    if isinstance(node, MappingNode):
        seen: set[object] = set()
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                raise _MarkedYamlError("LSW104", "mapping keys must be strings", pointer, key_node.start_mark)
            key = key_node.value
            if key in seen:
                raise _MarkedYamlError("LSW103", f"duplicate key {key!r}", f"{pointer}/{_escape(key)}", key_node.start_mark)
            seen.add(key)
            _collect_marks(value_node, f"{pointer}/{_escape(key)}", marks)
    elif isinstance(node, SequenceNode):
        for index, child in enumerate(node.value):
            _collect_marks(child, f"{pointer}/{index}", marks)


def _diagnostic_from_yaml(path: Path, exc: Exception) -> DiagnosticError:
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    return DiagnosticError((Diagnostic(
        "LSW101", "invalid YAML", path,
        mark.line + 1 if mark else None, mark.column + 1 if mark else None, "",
        str(getattr(exc, "problem", "fix the YAML syntax")),
    ),))


def load_workflow(path: str | Path) -> MarkedDocument:
    source = Path(path)
    try:
        loader = _MarkedSafeLoader(source.read_text())
        try:
            node = loader.get_single_node()
            if node is None:
                return MarkedDocument(source, None, {})
            marks: dict[str, SourceMark] = {}
            _collect_marks(node, "", marks)
            return MarkedDocument(source, loader.construct_document(node), marks)
        finally:
            loader.dispose()
    except _MarkedYamlError as exc:
        mark = _source_mark(exc.mark)
        raise DiagnosticError((Diagnostic(exc.code, exc.message, source, mark.line, mark.column, exc.pointer, "remove the unsupported YAML construct"),)) from exc
    except yaml.YAMLError as exc:
        raise _diagnostic_from_yaml(source, exc) from exc


class _Parser:
    def __init__(self, document: MarkedDocument) -> None:
        self.document = document
        self._ids: dict[str, str] = {}

    def fail(self, code: str, message: str, pointer: str, hint: str) -> NoReturn:
        mark = self.document.mark_for(pointer)
        raise DiagnosticError((Diagnostic(
            code, message, self.document.path,
            mark.line if mark else None, mark.column if mark else None, pointer, hint,
        ),))

    def mapping(self, value: Any, pointer: str, noun: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.fail("LSW108", f"{noun} must be a mapping", pointer, f"make {noun} a YAML mapping")
        return value

    def sequence(self, value: Any, pointer: str, noun: str) -> list[Any]:
        if not isinstance(value, list):
            self.fail("LSW108", f"{noun} must be a list", pointer, f"make {noun} a YAML list")
        return value

    def string(self, value: Any, pointer: str, noun: str) -> str:
        if not isinstance(value, str) or not value:
            self.fail("LSW108", f"{noun} must be a non-empty string", pointer, f"provide a non-empty {noun}")
        return value

    def positive_int(self, value: Any, pointer: str, noun: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            self.fail("LSW108", f"{noun} must be a positive integer", pointer, f"provide a positive {noun}")
        return value

    def keys(self, value: dict[str, Any], pointer: str, allowed: Iterable[str], required: Iterable[str] = ()) -> None:
        allowed_set = set(allowed)
        for key in value:
            key_pointer = f"{pointer}/{_escape(str(key))}"
            if not isinstance(key, str):
                self.fail("LSW105", "mapping keys must be strings", key_pointer, "use a string key")
            if key.startswith("x-"):
                continue
            if key in _V2_KEYS:
                self.fail("LSW120", f"{key!r} is not available in Workflow DSL v1", key_pointer, "remove the v2-only key")
            if key not in allowed_set:
                self.fail("LSW105", f"unknown key {key!r}", key_pointer, "remove it or prefix inert metadata with x-")
        for key in required:
            if key not in value:
                self.fail("LSW106", f"missing required key {key!r}", pointer, f"add required key {key!r}")

    def identifier(self, value: Any, pointer: str, noun: str = "id", optional: bool = False) -> str | None:
        if value is None and optional:
            return None
        text = self.string(value, pointer, noun)
        if not _ID.fullmatch(text):
            self.fail("LSW110", f"invalid {noun} {text!r}", pointer, "use lowercase letters, digits, and hyphens, beginning with a letter")
        return text

    def strings(self, value: Any, pointer: str, noun: str) -> tuple[str, ...]:
        items = self.sequence(value, pointer, noun)
        return tuple(self.string(item, f"{pointer}/{index}", noun.removesuffix("s")) for index, item in enumerate(items))

    def handler(self, value: Any, pointer: str) -> str | None:
        if value is None:
            return None
        text = self.string(value, pointer, "outcome handler")
        if text != "escalate":
            self.fail("LSW108", "v1 outcome handlers must be escalate", pointer, "use escalate")
        return text

    def parse(self) -> WorkflowIR:
        root = self.mapping(self.document.data, "", "workflow document")
        self.keys(root, "", {"workflow_version", "name", "description", "protect", "defaults", "flow"}, {"workflow_version", "name", "description", "protect", "flow"})
        version = root["workflow_version"]
        if version != "1":
            self.fail("LSW120", "only workflow_version '1' is supported", "/workflow_version", "use workflow_version: '1'")
        name = self.identifier(root["name"], "/name", "workflow name")
        expected_name = self.document.path.name.removesuffix(_WORKFLOW_SUFFIX)
        if not self.document.path.name.endswith(_WORKFLOW_SUFFIX) or name != expected_name:
            self.fail("LSW109", "workflow name must match its .workflow.yaml filename", "/name", f"use name: {expected_name}")
        description = self.string(root["description"], "/description", "description")
        protect = self.strings(root["protect"], "/protect", "protect")
        if protect != ("**",):
            self.fail("LSW301", "v1 workflows must protect the complete project", "/protect", 'use protect: ["**"]')
        defaults_ir = WorkflowDefaultsIR()
        if "defaults" in root:
            defaults = self.mapping(root["defaults"], "/defaults", "defaults")
            self.keys(defaults, "/defaults", {"retry"})
            if "retry" in defaults:
                defaults_ir = WorkflowDefaultsIR(self.retry(defaults["retry"], "/defaults/retry"))
        flow = tuple(self.parse_flow(self.sequence(root["flow"], "/flow", "flow"), "/flow"))
        return WorkflowIR("1", name, description, protect, flow, defaults_ir, self.document.path)

    def parse_flow(self, items: list[Any], pointer: str, parallel: bool = False) -> list[BlockIR]:
        blocks: list[BlockIR] = []
        for index, item in enumerate(items):
            block_pointer = f"{pointer}/{index}"
            block = self.parse_block(item, block_pointer, parallel)
            blocks.append(block)
        return blocks

    def parse_block(self, raw: Any, pointer: str, parallel: bool = False) -> BlockIR:
        item = self.mapping(raw, pointer, "flow item")
        v2_discriminators = [key for key in item if key in _V2_KEYS]
        if v2_discriminators:
            key = v2_discriminators[0]
            self.fail("LSW120", f"{key!r} is not available in Workflow DSL v1", f"{pointer}/{_escape(key)}", "remove the v2-only key")
        discriminators = [key for key in item if key in _BLOCKS]
        if len(discriminators) != 1:
            self.fail("LSW107", "a flow item must contain exactly one block discriminator", pointer, "use exactly one block discriminator")
        kind = discriminators[0]
        block = getattr(self, f"block_{kind}")(item, pointer)
        if parallel and isinstance(block, (StepIR, AcceptIR, RepeatIR, ParallelIR)):
            self.fail("LSP101", f"{kind!r} is not permitted in a parallel branch", pointer, "use only parallel-eligible blocks")
        if block.id is not None:
            if block.id in self._ids:
                self.fail("LSW110", f"duplicate id {block.id!r}", pointer, "use a unique explicit id")
            self._ids[block.id] = pointer
        return block

    def block_step(self, item: dict[str, Any], pointer: str) -> StepIR:
        self.keys(item, pointer, {"step", "id", "task", "exit", "writes", "evidence", "artifact", "retry", "on_failure", "on_error"}, {"step", "task", "exit"})
        step = self.identifier(item["step"], f"{pointer}/step", "step")
        return StepIR(self.identifier(item.get("id"), f"{pointer}/id", optional=True), step, self.string(item["task"], f"{pointer}/task", "task"), self.string(item["exit"], f"{pointer}/exit", "exit"), self.strings(item.get("writes", []), f"{pointer}/writes", "writes"), self.optional_mapping(item, "evidence", pointer), self.optional_mapping(item, "artifact", pointer), self.retry(item["retry"], f"{pointer}/retry") if "retry" in item else None, self.handler(item.get("on_failure"), f"{pointer}/on_failure"), self.handler(item.get("on_error"), f"{pointer}/on_error"))

    def block_verify(self, item: dict[str, Any], pointer: str) -> VerifyIR:
        self.keys(item, pointer, {"verify"})
        body = self.mapping(item["verify"], f"{pointer}/verify", "verify")
        self.keys(body, f"{pointer}/verify", {"id", "command", "cwd", "timeout", "junit", "writes", "retry", "on_failure", "on_error"}, {"command"})
        return VerifyIR(self.identifier(body.get("id"), f"{pointer}/verify/id", optional=True), self.string(body["command"], f"{pointer}/verify/command", "command"), self.string(body["cwd"], f"{pointer}/verify/cwd", "cwd") if "cwd" in body else None, self.positive_int(body["timeout"], f"{pointer}/verify/timeout", "timeout") if "timeout" in body else None, self.optional_mapping(body, "junit", f"{pointer}/verify"), self.strings(body.get("writes", []), f"{pointer}/verify/writes", "writes"), self.retry(body["retry"], f"{pointer}/verify/retry") if "retry" in body else None, self.handler(body.get("on_failure"), f"{pointer}/verify/on_failure"), self.handler(body.get("on_error"), f"{pointer}/verify/on_error"))

    def block_decide(self, item: dict[str, Any], pointer: str) -> DecideIR:
        self.keys(item, pointer, {"decide"})
        body = self.mapping(item["decide"], f"{pointer}/decide", "decide")
        self.keys(body, f"{pointer}/decide", {"id", "using", "on_failure", "on_error"}, {"using"})
        using = self.mapping(body["using"], f"{pointer}/decide/using", "decision provider")
        self.keys(using, f"{pointer}/decide/using", {"type", "since", "cases", "default"}, {"type", "since", "cases", "default"})
        if using["type"] != "changed-paths" or using["since"] != "start":
            self.fail("LSW108", "v1 decide uses changed-paths since start", f"{pointer}/decide/using", "use type: changed-paths and since: start")
        cases = self.mapping(using["cases"], f"{pointer}/decide/using/cases", "decision cases")
        for key, value in cases.items():
            self.string(key, f"{pointer}/decide/using/cases/{_escape(str(key))}", "case label")
            self.strings(value, f"{pointer}/decide/using/cases/{_escape(str(key))}", "case paths")
        self.string(using["default"], f"{pointer}/decide/using/default", "decision default")
        return DecideIR(self.identifier(body.get("id"), f"{pointer}/decide/id", optional=True), using, self.handler(body.get("on_failure"), f"{pointer}/decide/on_failure"), self.handler(body.get("on_error"), f"{pointer}/decide/on_error"))

    def block_choose(self, item: dict[str, Any], pointer: str) -> ChooseIR:
        self.keys(item, pointer, {"choose"})
        body = self.mapping(item["choose"], f"{pointer}/choose", "choose")
        self.keys(body, f"{pointer}/choose", {"id", "value", "cases", "default"}, {"value", "cases"})
        cases_raw = self.mapping(body["cases"], f"{pointer}/choose/cases", "choose cases")
        cases = {str(label): tuple(self.parse_flow(self.sequence(value, f"{pointer}/choose/cases/{_escape(str(label))}", "case"), f"{pointer}/choose/cases/{_escape(str(label))}")) for label, value in cases_raw.items()}
        default = tuple(self.parse_flow(self.sequence(body["default"], f"{pointer}/choose/default", "default"), f"{pointer}/choose/default")) if "default" in body else None
        return ChooseIR(self.identifier(body.get("id"), f"{pointer}/choose/id", optional=True), self.string(body["value"], f"{pointer}/choose/value", "choose value"), cases, default)

    def block_repeat(self, item: dict[str, Any], pointer: str) -> RepeatIR:
        self.keys(item, pointer, {"repeat"})
        body = self.mapping(item["repeat"], f"{pointer}/repeat", "repeat")
        self.keys(body, f"{pointer}/repeat", {"id", "limit", "until", "do", "exhausted"}, {"limit", "until", "do", "exhausted"})
        return RepeatIR(self.identifier(body.get("id"), f"{pointer}/repeat/id", optional=True), self.positive_int(body["limit"], f"{pointer}/repeat/limit", "repeat limit"), self.string(body["until"], f"{pointer}/repeat/until", "repeat until"), tuple(self.parse_flow(self.sequence(body["do"], f"{pointer}/repeat/do", "repeat do"), f"{pointer}/repeat/do")), self.handler(body["exhausted"], f"{pointer}/repeat/exhausted") or "")

    def block_call(self, item: dict[str, Any], pointer: str) -> CallIR:
        self.keys(item, pointer, {"call"})
        body = self.mapping(item["call"], f"{pointer}/call", "call")
        self.keys(body, f"{pointer}/call", {"id", "workflow", "runner", "timeout_minutes", "artifacts", "on_failure", "on_error"}, {"workflow", "runner"})
        artifacts = self.string_mapping(body.get("artifacts", {}), f"{pointer}/call/artifacts", "artifacts")
        if artifacts and "id" not in body:
            self.fail("LSW106", "a call with artifacts requires an explicit id", f"{pointer}/call", "add a unique call id")
        return CallIR(self.identifier(body.get("id"), f"{pointer}/call/id", optional=True), self.identifier(body["workflow"], f"{pointer}/call/workflow", "workflow") or "", self.identifier(body["runner"], f"{pointer}/call/runner", "runner") or "", self.positive_int(body["timeout_minutes"], f"{pointer}/call/timeout_minutes", "timeout minutes") if "timeout_minutes" in body else None, artifacts, self.handler(body.get("on_failure"), f"{pointer}/call/on_failure"), self.handler(body.get("on_error"), f"{pointer}/call/on_error"))

    def block_accept(self, item: dict[str, Any], pointer: str) -> AcceptIR:
        self.keys(item, pointer, {"accept"})
        body = self.mapping(item["accept"], f"{pointer}/accept", "accept")
        self.keys(body, f"{pointer}/accept", {"id", "artifact", "hash_from", "artifact_from", "verdict"}, {"verdict"})
        paired = "artifact" in body and "hash_from" in body
        from_handle = "artifact_from" in body
        if paired == from_handle:
            self.fail("LSW108", "accept requires artifact plus hash_from, or artifact_from", f"{pointer}/accept", "choose exactly one accept artifact form")
        if body["verdict"] != "PASS":
            self.fail("LSW108", "accept verdict must be PASS", f"{pointer}/accept/verdict", "use verdict: PASS")
        return AcceptIR(self.identifier(body.get("id"), f"{pointer}/accept/id", optional=True), self.string(body["artifact"], f"{pointer}/accept/artifact", "artifact") if paired else None, self.string(body["hash_from"], f"{pointer}/accept/hash_from", "hash_from") if paired else None, self.string(body["artifact_from"], f"{pointer}/accept/artifact_from", "artifact_from") if from_handle else None, "PASS")

    def block_parallel(self, item: dict[str, Any], pointer: str) -> ParallelIR:
        self.keys(item, pointer, {"parallel"})
        body = self.mapping(item["parallel"], f"{pointer}/parallel", "parallel")
        self.keys(body, f"{pointer}/parallel", {"id", "join", "timeout_minutes", "branches", "on_failure", "on_error"}, {"join", "branches"})
        if body["join"] != "all":
            self.fail("LSW120", "only join: all is available in Workflow DSL v1", f"{pointer}/parallel/join", "use join: all")
        branch_data = self.mapping(body["branches"], f"{pointer}/parallel/branches", "parallel branches")
        if not 2 <= len(branch_data) <= 8:
            self.fail("LSP101", "parallel requires between 2 and 8 branches", f"{pointer}/parallel/branches", "declare 2 through 8 branches")
        branches: dict[str, tuple[BlockIR, ...]] = {}
        for branch, blocks in branch_data.items():
            label = self.identifier(branch, f"{pointer}/parallel/branches/{_escape(str(branch))}", "branch")
            branch_pointer = f"{pointer}/parallel/branches/{_escape(label or '')}"
            branches[label or ""] = tuple(self.parse_flow(self.sequence(blocks, branch_pointer, "branch"), branch_pointer, parallel=True))
        return ParallelIR(self.identifier(body.get("id"), f"{pointer}/parallel/id", optional=True), "all", branches, self.positive_int(body["timeout_minutes"], f"{pointer}/parallel/timeout_minutes", "timeout minutes") if "timeout_minutes" in body else None, self.handler(body.get("on_failure"), f"{pointer}/parallel/on_failure"), self.handler(body.get("on_error"), f"{pointer}/parallel/on_error"))

    def block_graph(self, item: dict[str, Any], pointer: str) -> GraphIR:
        self.keys(item, pointer, {"graph"})
        body = self.mapping(item["graph"], f"{pointer}/graph", "graph")
        self.keys(body, f"{pointer}/graph", {"id", "fragment", "state", "tools", "nodes", "edges", "loop_limits", "loop_exits"}, {"fragment", "nodes", "edges"})
        self.fragment(body["fragment"], f"{pointer}/graph/fragment")
        self.mapping(body["nodes"], f"{pointer}/graph/nodes", "graph nodes")
        self.sequence(body["edges"], f"{pointer}/graph/edges", "graph edges")
        return GraphIR(self.identifier(body.get("id"), f"{pointer}/graph/id", optional=True), "inline", body)

    def block_include_graph(self, item: dict[str, Any], pointer: str) -> GraphIR:
        self.keys(item, pointer, {"include_graph"})
        body = self.mapping(item["include_graph"], f"{pointer}/include_graph", "include_graph")
        self.keys(body, f"{pointer}/include_graph", {"id", "path", "on"}, {"id", "path"})
        on = self.include_on(body.get("on"), f"{pointer}/include_graph/on")
        return GraphIR(self.identifier(body["id"], f"{pointer}/include_graph/id") or "", "include", None, self.string(body["path"], f"{pointer}/include_graph/path", "graph path"), on)

    def block_escalate(self, item: dict[str, Any], pointer: str) -> EscalateIR:
        self.keys(item, pointer, {"escalate"})
        body = item["escalate"]
        if body is not None:
            self.mapping(body, f"{pointer}/escalate", "escalate")
        return EscalateIR()

    def retry(self, value: Any, pointer: str) -> RetryIR:
        retry = self.mapping(value, pointer, "retry")
        self.keys(retry, pointer, {"limit", "exhausted"}, {"limit", "exhausted"})
        return RetryIR(self.positive_int(retry["limit"], f"{pointer}/limit", "retry limit"), self.handler(retry["exhausted"], f"{pointer}/exhausted"))

    def fragment(self, value: Any, pointer: str) -> None:
        fragment = self.mapping(value, pointer, "graph fragment")
        self.keys(fragment, pointer, {"entry", "exits", "effects"}, {"entry", "exits", "effects"})
        self.string(fragment["entry"], f"{pointer}/entry", "fragment entry")
        exits = self.mapping(fragment["exits"], f"{pointer}/exits", "fragment exits")
        if not exits:
            self.fail("LSW108", "fragment exits must not be empty", f"{pointer}/exits", "declare at least one named exit")
        for name, target in exits.items():
            self.string(name, f"{pointer}/exits/{_escape(str(name))}", "exit name")
            self.string(target, f"{pointer}/exits/{_escape(str(name))}", "exit target")
        effects = self.mapping(fragment["effects"], f"{pointer}/effects", "fragment effects")
        self.keys(effects, f"{pointer}/effects", {"mode", "writes"}, {"mode", "writes"})
        writes = self.strings(effects["writes"], f"{pointer}/effects/writes", "effect writes")
        mode = effects["mode"]
        if mode == "read-only" and writes:
            self.fail("LSW108", "read-only graph effects require writes: []", f"{pointer}/effects/writes", "use writes: []")
        if mode == "declared-writes" and not writes:
            self.fail("LSW108", "declared-writes graph effects require writes", f"{pointer}/effects/writes", "declare at least one write path")
        if mode not in {"read-only", "declared-writes"}:
            self.fail("LSW108", "invalid graph effects mode", f"{pointer}/effects/mode", "use read-only or declared-writes")

    def include_on(self, value: Any, pointer: str) -> dict[str, str]:
        if value is None:
            return {"pass": "next", "fail": "escalate", "error": "escalate"}
        on = self.mapping(value, pointer, "include_graph on")
        self.keys(on, pointer, {"pass", "fail", "error"}, {"pass"})
        for outcome in ("fail", "error"):
            if outcome in on and on[outcome] is None:
                self.fail("LSW108", f"include_graph on.{outcome} must be escalate", f"{pointer}/{outcome}", "use escalate or omit the key")
        result = {
            "pass": self.string(on["pass"], f"{pointer}/pass", "include pass handler"),
            "fail": self.handler(on["fail"], f"{pointer}/fail") if "fail" in on else "escalate",
            "error": self.handler(on["error"], f"{pointer}/error") if "error" in on else "escalate",
        }
        if result["pass"] != "next":
            self.fail("LSW108", "include_graph on.pass must be next", f"{pointer}/pass", "use pass: next")
        return result

    def optional_mapping(self, item: dict[str, Any], key: str, pointer: str) -> dict[str, Any] | None:
        return self.mapping(item[key], f"{pointer}/{key}", key) if key in item else None

    def string_mapping(self, value: Any, pointer: str, noun: str) -> dict[str, str]:
        mapping = self.mapping(value, pointer, noun)
        return {self.string(key, f"{pointer}/{_escape(str(key))}", f"{noun} key"): self.string(item, f"{pointer}/{_escape(str(key))}", f"{noun} value") for key, item in mapping.items()}


def parse_workflow(document: MarkedDocument) -> WorkflowIR:
    """Parse a marked YAML document into the structural v1 workflow IR."""
    return _Parser(document).parse()
