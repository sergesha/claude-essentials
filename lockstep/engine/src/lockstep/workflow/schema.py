"""Marked, alias-free YAML loading and the Workflow DSL's v1 schema."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, ClassVar

import yaml
from yaml.events import (
    AliasEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)
from yaml.nodes import MappingNode, Node, SequenceNode

from ._schema_validation import (
    _V2_KEYS,
    MarkedDocument,
    SourceMark,
    _escape,
    _SchemaValidation,
)
from .diagnostics import Diagnostic, DiagnosticError
from .ir import (
    AcceptIR,
    BlockIR,
    CallIR,
    ChooseIR,
    DecideIR,
    EscalateIR,
    ExportedArtifactIR,
    GraphIR,
    MarkdownArtifactIR,
    ParallelIR,
    RepeatIR,
    RetryIR,
    StepIR,
    VerifyIR,
    WorkflowDefaultsIR,
    WorkflowIR,
)

_WORKFLOW_SUFFIX = ".workflow.yaml"
_MAX_YAML_DEPTH = 64
_MAX_YAML_NODES = 50_000
_MAX_YAML_COLLECTION_ITEMS = 10_000
_MAX_YAML_SCALAR_BYTES = 2 * 1024 * 1024
_BLOCKS = frozenset({"step", "verify", "decide", "choose", "repeat", "call", "accept", "parallel", "graph", "include_graph", "escalate"})
class _MarkedYamlError(Exception):
    def __init__(self, code: str, message: str, pointer: str, mark: Any) -> None:
        self.code, self.message, self.pointer, self.mark = code, message, pointer, mark


class _MarkedSafeLoader(yaml.SafeLoader):
    yaml_implicit_resolvers: ClassVar[dict] = {
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


def _structure_diagnostic(path: Path, message: str, mark: Any = None) -> DiagnosticError:
    return DiagnosticError((Diagnostic(
        "LSW111",
        message,
        path,
        mark.line + 1 if mark else 1,
        mark.column + 1 if mark else 1,
        "",
        "reduce the workflow YAML depth, node count, collection size, or scalar content",
    ),))


def _preflight_yaml_structure(path: Path, source_text: str) -> None:
    stack: list[list[int]] = []
    nodes = scalar_bytes = 0
    for event in yaml.parse(source_text, Loader=_MarkedSafeLoader):
        is_node = isinstance(event, (AliasEvent, MappingStartEvent, ScalarEvent, SequenceStartEvent))
        if is_node:
            nodes += 1
            if nodes > _MAX_YAML_NODES:
                raise _structure_diagnostic(path, "workflow YAML node limit exceeded", event.start_mark)
            if stack:
                stack[-1][1] += 1
                limit = 2 * _MAX_YAML_COLLECTION_ITEMS if stack[-1][0] else _MAX_YAML_COLLECTION_ITEMS
                if stack[-1][1] > limit:
                    raise _structure_diagnostic(path, "workflow YAML collection item limit exceeded", event.start_mark)
        if isinstance(event, ScalarEvent):
            scalar_bytes += len(event.value.encode("utf-8"))
            if scalar_bytes > _MAX_YAML_SCALAR_BYTES:
                raise _structure_diagnostic(path, "workflow YAML scalar byte limit exceeded", event.start_mark)
        elif isinstance(event, (MappingStartEvent, SequenceStartEvent)):
            stack.append([int(isinstance(event, MappingStartEvent)), 0])
            if len(stack) > _MAX_YAML_DEPTH:
                raise _structure_diagnostic(path, "workflow YAML depth limit exceeded", event.start_mark)
        elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            stack.pop()


def load_workflow_bytes(path: str | Path, source_bytes: bytes) -> MarkedDocument:
    """Parse one already-captured workflow byte sequence."""

    source = Path(path)
    if not isinstance(source_bytes, bytes):
        raise TypeError("workflow source bytes must be bytes")
    try:
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        source_text = source_bytes.decode("utf-8")
        _preflight_yaml_structure(source, source_text)
        loader = _MarkedSafeLoader(source_text)
        try:
            node = loader.get_single_node()
            if node is None:
                return MarkedDocument(source, None, {}, source_sha256)
            marks: dict[str, SourceMark] = {}
            _collect_marks(node, "", marks)
            return MarkedDocument(
                source, loader.construct_document(node), marks, source_sha256
            )
        finally:
            loader.dispose()
    except _MarkedYamlError as exc:
        mark = _source_mark(exc.mark)
        raise DiagnosticError((Diagnostic(exc.code, exc.message, source, mark.line, mark.column, exc.pointer, "remove the unsupported YAML construct"),)) from exc
    except yaml.YAMLError as exc:
        raise _diagnostic_from_yaml(source, exc) from exc
    except RecursionError as exc:
        raise _structure_diagnostic(
            source, "workflow YAML recursion limit exceeded"
        ) from exc


def load_workflow(path: str | Path) -> MarkedDocument:
    source = Path(path)
    return load_workflow_bytes(source, source.read_bytes())


class _Parser:
    def __init__(self, document: MarkedDocument) -> None:
        self.document = document
        self.validation = _SchemaValidation(document)
        self._ids: dict[str, str] = {}

    def parse(self) -> WorkflowIR:
        root = self.validation.mapping(self.document.data, "", "workflow document")
        self.validation.keys(root, "", {"workflow_version", "name", "description", "protect", "defaults", "flow"}, {"workflow_version", "name", "description", "protect", "flow"})
        version = root["workflow_version"]
        if version != "1":
            self.validation.fail("LSW120", "only workflow_version '1' is supported", "/workflow_version", "use workflow_version: '1'")
        name = self.validation.identifier(root["name"], "/name", "workflow name")
        expected_name = self.document.path.name.removesuffix(_WORKFLOW_SUFFIX)
        if not self.document.path.name.endswith(_WORKFLOW_SUFFIX) or name != expected_name:
            self.validation.fail("LSW109", "workflow name must match its .workflow.yaml filename", "/name", f"use name: {expected_name}")
        description = self.validation.string(root["description"], "/description", "description")
        protect = self.validation.strings(root["protect"], "/protect", "protect")
        if protect != ("**",):
            self.validation.fail("LSW301", "v1 workflows must protect the complete project", "/protect", 'use protect: ["**"]')
        defaults_ir = WorkflowDefaultsIR()
        if "defaults" in root:
            defaults = self.validation.mapping(root["defaults"], "/defaults", "defaults")
            self.validation.keys(defaults, "/defaults", {"retry"})
            if "retry" in defaults:
                defaults_ir = WorkflowDefaultsIR(self.retry(defaults["retry"], "/defaults/retry"))
        flow = tuple(self.parse_flow(self.validation.sequence(root["flow"], "/flow", "flow"), "/flow"))
        return WorkflowIR(
            "1", name, description, protect, flow, defaults_ir,
            self.document.path, self.document.marks, self.document.source_sha256,
        )

    def parse_flow(self, items: list[Any], pointer: str, parallel: bool = False) -> list[BlockIR]:
        blocks: list[BlockIR] = []
        for index, item in enumerate(items):
            block_pointer = f"{pointer}/{index}"
            block = self.parse_block(item, block_pointer, parallel)
            blocks.append(block)
        return blocks

    def parse_block(self, raw: Any, pointer: str, parallel: bool = False) -> BlockIR:
        item = self.validation.mapping(raw, pointer, "flow item")
        v2_discriminators = [key for key in item if key in _V2_KEYS]
        if v2_discriminators:
            key = v2_discriminators[0]
            self.validation.fail("LSW120", f"{key!r} is not available in Workflow DSL v1", f"{pointer}/{_escape(key)}", "remove the v2-only key")
        discriminators = [key for key in item if key in _BLOCKS]
        if len(discriminators) != 1:
            self.validation.fail("LSW107", "a flow item must contain exactly one block discriminator", pointer, "use exactly one block discriminator")
        kind = discriminators[0]
        block = getattr(self, f"block_{kind}")(item, pointer)
        if parallel and isinstance(block, (AcceptIR, RepeatIR, ParallelIR)):
            self.validation.fail("LSP101", f"{kind!r} is not permitted in a parallel branch", pointer, "use only parallel-eligible blocks")
        if block.id is not None:
            if block.id in self._ids:
                self.validation.fail("LSW110", f"duplicate id {block.id!r}", pointer, "use a unique explicit id")
            self._ids[block.id] = pointer
        return block

    def block_step(self, item: dict[str, Any], pointer: str) -> StepIR:
        self.validation.keys(item, pointer, {"step", "id", "task", "exit", "writes", "evidence", "artifact", "retry", "on_failure", "on_error"}, {"step", "task", "exit"})
        step = self.validation.identifier(item["step"], f"{pointer}/step", "step")
        artifact = (
            self.exported_artifact(item["artifact"], f"{pointer}/artifact")
            if "artifact" in item
            else None
        )
        return StepIR(self.validation.identifier(item.get("id"), f"{pointer}/id", optional=True), step, self.validation.string(item["task"], f"{pointer}/task", "task"), self.validation.string(item["exit"], f"{pointer}/exit", "exit"), self.validation.strings(item.get("writes", []), f"{pointer}/writes", "writes"), self.optional_mapping(item, "evidence", pointer), artifact, self.retry(item["retry"], f"{pointer}/retry") if "retry" in item else None, self.validation.handler(item.get("on_failure"), f"{pointer}/on_failure"), self.validation.handler(item.get("on_error"), f"{pointer}/on_error"))

    def exported_artifact(self, value: Any, pointer: str) -> ExportedArtifactIR:
        artifact = self.validation.mapping(value, pointer, "artifact")
        self.validation.keys(artifact, pointer, {"handle", "path", "markdown"}, {"handle", "path", "markdown"})
        markdown_pointer = f"{pointer}/markdown"
        markdown = self.validation.mapping(artifact["markdown"], markdown_pointer, "artifact markdown")
        self.validation.keys(markdown, markdown_pointer, {"sections"}, {"sections"})
        sections_pointer = f"{markdown_pointer}/sections"
        sections = self.validation.strings(markdown["sections"], sections_pointer, "sections")
        if not sections:
            self.validation.fail("LSW108", "artifact Markdown sections must not be empty", sections_pointer, "declare at least one requested heading")
        seen: set[str] = set()
        for index, section in enumerate(sections):
            if section in seen:
                self.validation.fail("LSW108", "artifact Markdown sections must be unique", f"{sections_pointer}/{index}", "remove the duplicate heading")
            seen.add(section)
        return ExportedArtifactIR(
            self.validation.identifier(artifact["handle"], f"{pointer}/handle", "artifact handle") or "",
            self.validation.string(artifact["path"], f"{pointer}/path", "artifact path"),
            MarkdownArtifactIR(sections),
        )

    def block_verify(self, item: dict[str, Any], pointer: str) -> VerifyIR:
        self.validation.keys(item, pointer, {"verify"})
        body = self.validation.mapping(item["verify"], f"{pointer}/verify", "verify")
        self.validation.keys(body, f"{pointer}/verify", {"id", "command", "cwd", "timeout", "retry", "on_failure", "on_error"}, {"command"})
        return VerifyIR(self.validation.identifier(body.get("id"), f"{pointer}/verify/id", optional=True), self.validation.string(body["command"], f"{pointer}/verify/command", "command"), self.validation.string(body["cwd"], f"{pointer}/verify/cwd", "cwd") if "cwd" in body else None, self.validation.positive_int(body["timeout"], f"{pointer}/verify/timeout", "timeout") if "timeout" in body else None, self.retry(body["retry"], f"{pointer}/verify/retry") if "retry" in body else None, self.validation.handler(body.get("on_failure"), f"{pointer}/verify/on_failure"), self.validation.handler(body.get("on_error"), f"{pointer}/verify/on_error"))

    def block_decide(self, item: dict[str, Any], pointer: str) -> DecideIR:
        self.validation.keys(item, pointer, {"decide"})
        body = self.validation.mapping(item["decide"], f"{pointer}/decide", "decide")
        self.validation.keys(body, f"{pointer}/decide", {"id", "using", "on_failure", "on_error"}, {"using"})
        using = self.validation.mapping(body["using"], f"{pointer}/decide/using", "decision provider")
        self.validation.keys(using, f"{pointer}/decide/using", {"type", "since", "cases", "default"}, {"type", "since", "cases", "default"})
        if using["type"] != "changed-paths" or using["since"] != "start":
            self.validation.fail("LSW108", "v1 decide uses changed-paths since start", f"{pointer}/decide/using", "use type: changed-paths and since: start")
        cases = self.validation.mapping(using["cases"], f"{pointer}/decide/using/cases", "decision cases")
        for key, value in cases.items():
            label_pointer = f"{pointer}/decide/using/cases/{_escape(str(key))}"
            self.validation.identifier(key, label_pointer, "case label")
            paths = self.validation.strings(value, label_pointer, "case paths")
            if not paths:
                self.validation.fail("LSW108", "decision case paths must not be empty", label_pointer, "declare at least one changed path glob")
        self.validation.identifier(using["default"], f"{pointer}/decide/using/default", "decision default")
        return DecideIR(self.validation.identifier(body.get("id"), f"{pointer}/decide/id", optional=True), using, self.validation.handler(body.get("on_failure"), f"{pointer}/decide/on_failure"), self.validation.handler(body.get("on_error"), f"{pointer}/decide/on_error"))

    def block_choose(self, item: dict[str, Any], pointer: str) -> ChooseIR:
        self.validation.keys(item, pointer, {"choose"})
        body = self.validation.mapping(item["choose"], f"{pointer}/choose", "choose")
        self.validation.keys(body, f"{pointer}/choose", {"id", "value", "cases", "default"}, {"value", "cases"})
        cases_raw = self.validation.mapping(body["cases"], f"{pointer}/choose/cases", "choose cases")
        cases = {str(label): tuple(self.parse_flow(self.validation.sequence(value, f"{pointer}/choose/cases/{_escape(str(label))}", "case"), f"{pointer}/choose/cases/{_escape(str(label))}")) for label, value in cases_raw.items()}
        default = tuple(self.parse_flow(self.validation.sequence(body["default"], f"{pointer}/choose/default", "default"), f"{pointer}/choose/default")) if "default" in body else None
        return ChooseIR(self.validation.identifier(body.get("id"), f"{pointer}/choose/id", optional=True), self.validation.string(body["value"], f"{pointer}/choose/value", "choose value"), cases, default)

    def block_repeat(self, item: dict[str, Any], pointer: str) -> RepeatIR:
        self.validation.keys(item, pointer, {"repeat"})
        body = self.validation.mapping(item["repeat"], f"{pointer}/repeat", "repeat")
        self.validation.keys(body, f"{pointer}/repeat", {"id", "limit", "until", "do", "exhausted"}, {"limit", "until", "do", "exhausted"})
        return RepeatIR(self.validation.identifier(body.get("id"), f"{pointer}/repeat/id", optional=True), self.validation.positive_int(body["limit"], f"{pointer}/repeat/limit", "repeat limit"), self.validation.string(body["until"], f"{pointer}/repeat/until", "repeat until"), tuple(self.parse_flow(self.validation.sequence(body["do"], f"{pointer}/repeat/do", "repeat do"), f"{pointer}/repeat/do")), self.validation.handler(body["exhausted"], f"{pointer}/repeat/exhausted") or "")

    def block_call(self, item: dict[str, Any], pointer: str) -> CallIR:
        self.validation.keys(item, pointer, {"call"})
        body = self.validation.mapping(item["call"], f"{pointer}/call", "call")
        self.validation.keys(body, f"{pointer}/call", {"id", "workflow", "runner", "timeout_minutes", "artifacts", "on_failure", "on_error"}, {"workflow", "runner"})
        artifacts = self.string_mapping(body.get("artifacts", {}), f"{pointer}/call/artifacts", "artifacts")
        if artifacts and "id" not in body:
            self.validation.fail("LSW106", "a call with artifacts requires an explicit id", f"{pointer}/call", "add a unique call id")
        return CallIR(self.validation.identifier(body.get("id"), f"{pointer}/call/id", optional=True), self.validation.identifier(body["workflow"], f"{pointer}/call/workflow", "workflow") or "", self.validation.identifier(body["runner"], f"{pointer}/call/runner", "runner") or "", self.validation.positive_int(body["timeout_minutes"], f"{pointer}/call/timeout_minutes", "timeout minutes") if "timeout_minutes" in body else None, artifacts, self.validation.handler(body.get("on_failure"), f"{pointer}/call/on_failure"), self.validation.handler(body.get("on_error"), f"{pointer}/call/on_error"))

    def block_accept(self, item: dict[str, Any], pointer: str) -> AcceptIR:
        self.validation.keys(item, pointer, {"accept"})
        body = self.validation.mapping(item["accept"], f"{pointer}/accept", "accept")
        self.validation.keys(body, f"{pointer}/accept", {"id", "artifact_from", "verdict"}, {"artifact_from", "verdict"})
        if body["verdict"] != "PASS":
            self.validation.fail("LSW108", "accept verdict must be PASS", f"{pointer}/accept/verdict", "use verdict: PASS")
        return AcceptIR(self.validation.identifier(body.get("id"), f"{pointer}/accept/id", optional=True), self.validation.string(body["artifact_from"], f"{pointer}/accept/artifact_from", "artifact_from"), "PASS")

    def block_parallel(self, item: dict[str, Any], pointer: str) -> ParallelIR:
        self.validation.keys(item, pointer, {"parallel"})
        body = self.validation.mapping(item["parallel"], f"{pointer}/parallel", "parallel")
        self.validation.keys(body, f"{pointer}/parallel", {"id", "join", "timeout_minutes", "branches", "on_failure", "on_error"}, {"join", "branches"})
        if body["join"] != "all":
            self.validation.fail("LSW120", "only join: all is available in Workflow DSL v1", f"{pointer}/parallel/join", "use join: all")
        branch_data = self.validation.mapping(body["branches"], f"{pointer}/parallel/branches", "parallel branches")
        if not 2 <= len(branch_data) <= 8:
            self.validation.fail("LSP101", "parallel requires between 2 and 8 branches", f"{pointer}/parallel/branches", "declare 2 through 8 branches")
        branches: dict[str, tuple[BlockIR, ...]] = {}
        for branch, blocks in branch_data.items():
            label = self.validation.identifier(branch, f"{pointer}/parallel/branches/{_escape(str(branch))}", "branch")
            branch_pointer = f"{pointer}/parallel/branches/{_escape(label or '')}"
            branches[label or ""] = tuple(self.parse_flow(self.validation.sequence(blocks, branch_pointer, "branch"), branch_pointer, parallel=True))
        return ParallelIR(self.validation.identifier(body.get("id"), f"{pointer}/parallel/id", optional=True), "all", branches, self.validation.positive_int(body["timeout_minutes"], f"{pointer}/parallel/timeout_minutes", "timeout minutes") if "timeout_minutes" in body else None, self.validation.handler(body.get("on_failure"), f"{pointer}/parallel/on_failure"), self.validation.handler(body.get("on_error"), f"{pointer}/parallel/on_error"))

    def block_graph(self, item: dict[str, Any], pointer: str) -> GraphIR:
        self.validation.keys(item, pointer, {"graph"})
        body = self.validation.mapping(item["graph"], f"{pointer}/graph", "graph")
        self.validation.keys(body, f"{pointer}/graph", {"id", "fragment", "state", "tools", "nodes", "edges", "loop_limits", "loop_exits"}, {"fragment", "nodes", "edges"})
        self.fragment(body["fragment"], f"{pointer}/graph/fragment")
        self.validation.mapping(body["nodes"], f"{pointer}/graph/nodes", "graph nodes")
        self.validation.sequence(body["edges"], f"{pointer}/graph/edges", "graph edges")
        return GraphIR(self.validation.identifier(body.get("id"), f"{pointer}/graph/id", optional=True), "inline", body)

    def block_include_graph(self, item: dict[str, Any], pointer: str) -> GraphIR:
        self.validation.keys(item, pointer, {"include_graph"})
        body = self.validation.mapping(item["include_graph"], f"{pointer}/include_graph", "include_graph")
        self.validation.keys(body, f"{pointer}/include_graph", {"id", "path", "on"}, {"id", "path"})
        on = self.include_on(body.get("on"), f"{pointer}/include_graph/on")
        authored_on = tuple(body.get("on", {})) if isinstance(body.get("on"), dict) else ()
        raw_path = self.validation.string(
            body["path"], f"{pointer}/include_graph/path", "graph path"
        )
        candidate = Path(raw_path)
        if (
            candidate.is_absolute()
            or not candidate.parts
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            self.validation.fail(
                "LSW305",
                "include_graph path must be a safe relative path",
                f"{pointer}/include_graph/path",
                "use a contained relative path without '.' or '..' segments",
            )
        return GraphIR(
            self.validation.identifier(body["id"], f"{pointer}/include_graph/id") or "",
            "include",
            None,
            raw_path,
            on,
            authored_on,
        )

    def block_escalate(self, item: dict[str, Any], pointer: str) -> EscalateIR:
        self.validation.keys(item, pointer, {"escalate"})
        body = item["escalate"]
        if body is not None:
            self.validation.mapping(body, f"{pointer}/escalate", "escalate")
        return EscalateIR()

    def retry(self, value: Any, pointer: str) -> RetryIR:
        retry = self.validation.mapping(value, pointer, "retry")
        self.validation.keys(retry, pointer, {"limit", "exhausted"}, {"limit", "exhausted"})
        return RetryIR(self.validation.positive_int(retry["limit"], f"{pointer}/limit", "retry limit"), self.validation.handler(retry["exhausted"], f"{pointer}/exhausted"))

    def fragment(self, value: Any, pointer: str) -> None:
        fragment = self.validation.mapping(value, pointer, "graph fragment")
        self.validation.keys(fragment, pointer, {"entry", "exits", "effects"}, {"entry", "exits", "effects"})
        self.validation.string(fragment["entry"], f"{pointer}/entry", "fragment entry")
        exits = self.validation.mapping(fragment["exits"], f"{pointer}/exits", "fragment exits")
        if not exits:
            self.validation.fail("LSW108", "fragment exits must not be empty", f"{pointer}/exits", "declare at least one named exit")
        for name, target in exits.items():
            self.validation.string(name, f"{pointer}/exits/{_escape(str(name))}", "exit name")
            self.validation.string(target, f"{pointer}/exits/{_escape(str(name))}", "exit target")
        effects = self.validation.mapping(fragment["effects"], f"{pointer}/effects", "fragment effects")
        self.validation.keys(effects, f"{pointer}/effects", {"mode", "writes"}, {"mode", "writes"})
        writes = self.validation.strings(effects["writes"], f"{pointer}/effects/writes", "effect writes")
        mode = effects["mode"]
        if mode == "read-only" and writes:
            self.validation.fail("LSW108", "read-only graph effects require writes: []", f"{pointer}/effects/writes", "use writes: []")
        if mode == "declared-writes" and not writes:
            self.validation.fail("LSW108", "declared-writes graph effects require writes", f"{pointer}/effects/writes", "declare at least one write path")
        if mode not in {"read-only", "declared-writes"}:
            self.validation.fail("LSW108", "invalid graph effects mode", f"{pointer}/effects/mode", "use read-only or declared-writes")

    def include_on(self, value: Any, pointer: str) -> dict[str, str]:
        if value is None:
            return {"pass": "next", "fail": "escalate", "error": "escalate"}
        on = self.validation.mapping(value, pointer, "include_graph on")
        self.validation.keys(on, pointer, {"pass", "fail", "error"}, {"pass"})
        for outcome in ("fail", "error"):
            if outcome in on and on[outcome] is None:
                self.validation.fail("LSW108", f"include_graph on.{outcome} must be escalate", f"{pointer}/{outcome}", "use escalate or omit the key")
        result = {
            "pass": self.validation.string(on["pass"], f"{pointer}/pass", "include pass handler"),
            **{
                outcome: self.validation.handler(on[outcome], f"{pointer}/{outcome}")
                for outcome in ("fail", "error") if outcome in on
            },
        }
        result.setdefault("fail", "escalate")
        result.setdefault("error", "escalate")
        if result["pass"] != "next":
            self.validation.fail("LSW108", "include_graph on.pass must be next", f"{pointer}/pass", "use pass: next")
        return result

    def optional_mapping(self, item: dict[str, Any], key: str, pointer: str) -> dict[str, Any] | None:
        return self.validation.mapping(item[key], f"{pointer}/{key}", key) if key in item else None

    def string_mapping(self, value: Any, pointer: str, noun: str) -> dict[str, str]:
        mapping = self.validation.mapping(value, pointer, noun)
        return {self.validation.string(key, f"{pointer}/{_escape(str(key))}", f"{noun} key"): self.validation.string(item, f"{pointer}/{_escape(str(key))}", f"{noun} value") for key, item in mapping.items()}


def parse_workflow(document: MarkedDocument) -> WorkflowIR:
    """Parse a marked YAML document into the structural v1 workflow IR."""
    return _Parser(document).parse()
