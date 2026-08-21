"""Semantic contracts for the structured Workflow DSL.

The parser deliberately accepts only structural data.  This module is the
next, pure phase: it turns that immutable tree into compiler-ready contracts
without reading project files or consulting runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
from pathlib import Path, PurePosixPath
import re
import shlex
from typing import Any, Literal, Mapping, Protocol, TypeAlias

from lockstep.runtime.effects.models import PinnedCommandSpec

from .diagnostics import Diagnostic, DiagnosticError
from .ir import (
    AcceptIR, BlockIR, CallIR, ChooseIR, DecideIR, EscalateIR, GraphIR,
    FragmentIR, ParallelIR, RepeatIR, RetryIR, StepIR, VerifyIR, WorkflowIR, freeze,
)


_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_TERMINAL_VALUES = ("pass", "fail", "error")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
YamlgraphStateType: TypeAlias = Literal[
    "str", "string", "int", "integer", "float", "bool", "boolean",
    "list", "dict", "any",
]
_YAMLGRAPH_STATE_TYPES = frozenset({
    "str", "string", "int", "integer", "float", "bool", "boolean",
    "list", "dict", "any",
})
_STATE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_RESERVED_STATE_NAMES = frozenset({
    "lockstep_outcome", "lockstep_continue", "current_step",
    "_loop_counts", "_loop_limit_reached",
})


def _canonical_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("path must be a canonical contained POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("path must be a canonical contained POSIX path")
    return value


def _exact_sha256(content: bytes, claimed: str) -> None:
    if not isinstance(content, bytes):
        raise TypeError("compiled file content must be bytes")
    if not _SHA256.fullmatch(claimed) or hashlib.sha256(content).hexdigest() != claimed:
        raise ValueError("compiled file sha256 does not match its exact content")


def _manifest_bundle_sha256(
    root_relative_path: str, files: tuple[tuple[str, str], ...]
) -> str:
    digest = hashlib.sha256(b"lockstep.compiled-bundle/v1\0")
    digest.update(root_relative_path.encode("utf-8"))
    digest.update(b"\0")
    for relative_path, sha256 in sorted(files):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


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
class ChildArtifactContract:
    handle: str
    fixed_source: str


@dataclass(frozen=True)
class ChildWorkflowContract:
    """Closed child surface supplied by a caller-owned catalog.

    The catalog is deliberately a semantic lookup: it exposes no paths or
    filesystem operations.  Parallel eligibility is derived from the
    non-artifact effect surface, never supplied as an asserted boolean.
    """

    outcomes: tuple[str, ...]
    exports: Mapping[str, ChildArtifactContract] = field(default_factory=dict)
    non_artifact_writes: tuple[str, ...] = ()
    state_inputs: Mapping[str, YamlgraphStateType] = field(default_factory=dict)
    state_exports: Mapping[str, YamlgraphStateType] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcomes", tuple(self.outcomes))
        object.__setattr__(self, "exports", freeze(self.exports))
        object.__setattr__(self, "non_artifact_writes", tuple(self.non_artifact_writes))
        inputs = dict(self.state_inputs)
        exports = dict(self.state_exports)
        for name, state_type in (*inputs.items(), *exports.items()):
            if not isinstance(name, str) or not _STATE_NAME.fullmatch(name):
                raise ValueError("child state names must be logical state identifiers")
            if name in _RESERVED_STATE_NAMES or name.startswith("lockstep_"):
                raise ValueError(f"child state name {name!r} is reserved")
            if state_type not in _YAMLGRAPH_STATE_TYPES:
                raise ValueError(f"unsupported yamlgraph state type: {state_type!r}")
        for name in inputs.keys() & exports.keys():
            if inputs[name] != exports[name]:
                raise ValueError(
                    f"child state {name!r} has different types at input and export"
                )
        object.__setattr__(self, "state_inputs", freeze(inputs))
        object.__setattr__(self, "state_exports", freeze(exports))


@dataclass(frozen=True, slots=True)
class CatalogFile:
    relative_path: str
    content: bytes = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        _canonical_relative_path(self.relative_path)
        _exact_sha256(self.content, self.sha256)

    @classmethod
    def build(cls, relative_path: str, content: bytes) -> "CatalogFile":
        if not isinstance(content, bytes):
            raise TypeError("compiled file content must be bytes")
        return cls(relative_path, content, hashlib.sha256(content).hexdigest())


@dataclass(frozen=True, slots=True)
class BundleDependency:
    kind: Literal["workflow", "fragment"]
    logical_name: str
    use_pointer: str
    definition_sha256: str
    compiled_sha256: str
    generated_root: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"workflow", "fragment"}:
            raise ValueError("unsupported compiled bundle dependency kind")
        if not isinstance(self.logical_name, str) or not self.logical_name:
            raise ValueError("bundle dependency logical_name must be non-empty")
        if not isinstance(self.use_pointer, str) or not self.use_pointer.startswith("/"):
            raise ValueError("bundle dependency use_pointer must be an absolute pointer")
        for digest in (self.definition_sha256, self.compiled_sha256):
            if not _SHA256.fullmatch(digest):
                raise ValueError("bundle dependency digest must be lowercase SHA-256")
        if self.kind == "workflow" and self.generated_root is None:
            raise ValueError("workflow dependency requires a generated root")
        if self.kind == "fragment" and self.generated_root is not None:
            raise ValueError("fragment dependency may not carry a generated root")
        if self.generated_root is not None:
            _canonical_relative_path(self.generated_root)


@dataclass(frozen=True, slots=True)
class CanonicalCompiledBundle:
    root_relative_path: str
    files: tuple[CatalogFile, ...]
    bundle_sha256: str
    compiler_version: str
    dependencies: tuple[BundleDependency, ...] = ()

    def __post_init__(self) -> None:
        _canonical_relative_path(self.root_relative_path)
        files = tuple(sorted(self.files, key=lambda item: item.relative_path))
        paths = tuple(item.relative_path for item in files)
        if len(paths) != len(set(paths)):
            raise ValueError("compiled bundle contains a duplicate file path")
        if self.root_relative_path not in paths:
            raise ValueError("compiled bundle root is missing from files")
        expected = _manifest_bundle_sha256(
            self.root_relative_path,
            tuple((item.relative_path, item.sha256) for item in files),
        )
        if self.bundle_sha256 != expected:
            raise ValueError("compiled bundle sha256 does not match its manifest")
        if self.compiler_version != "1":
            raise ValueError("compiled bundle compiler_version must be exactly '1'")
        dependencies = tuple(
            sorted(
                self.dependencies,
                key=lambda item: (item.use_pointer, item.kind, item.logical_name),
            )
        )
        if len(dependencies) != len(
            {(item.use_pointer, item.kind, item.logical_name) for item in dependencies}
        ):
            raise ValueError("compiled bundle contains duplicate dependency uses")
        file_paths = set(paths)
        for item in dependencies:
            if item.generated_root is not None and item.generated_root not in file_paths:
                raise ValueError("bundle dependency generated root is missing")
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "dependencies", dependencies)

    @classmethod
    def build(
        cls,
        *,
        root_relative_path: str,
        files: tuple[CatalogFile, ...],
        compiler_version: str,
        dependencies: tuple[BundleDependency, ...] = (),
    ) -> "CanonicalCompiledBundle":
        frozen_files = tuple(files)
        return cls(
            root_relative_path,
            frozen_files,
            _manifest_bundle_sha256(
                root_relative_path,
                tuple((item.relative_path, item.sha256) for item in frozen_files),
            ),
            compiler_version,
            tuple(dependencies),
        )


@dataclass(frozen=True, slots=True)
class ResolvedChild:
    logical_name: str
    contract: ChildWorkflowContract
    source_definition_sha256: str
    standalone: CanonicalCompiledBundle

    def __post_init__(self) -> None:
        if not isinstance(self.logical_name, str) or not self.logical_name:
            raise ValueError("resolved child logical_name must be non-empty")
        if not _SHA256.fullmatch(self.source_definition_sha256):
            raise ValueError("resolved child source_definition_sha256 is invalid")


@dataclass(frozen=True, slots=True)
class ResolvedFragment:
    logical_path: str
    source_definition_sha256: str
    fragment: FragmentIR

    def __post_init__(self) -> None:
        _canonical_relative_path(self.logical_path)
        if not _SHA256.fullmatch(self.source_definition_sha256):
            raise ValueError("resolved fragment source_definition_sha256 is invalid")
        if not isinstance(self.fragment, FragmentIR):
            raise TypeError("resolved fragment requires closed FragmentIR")


@dataclass(frozen=True, slots=True)
class ResolvedCatalog:
    children: Mapping[str, ResolvedChild] = field(default_factory=dict)
    fragments: Mapping[str, ResolvedFragment] = field(default_factory=dict)

    def __post_init__(self) -> None:
        children = dict(self.children)
        fragments = dict(self.fragments)
        if any(key != child.logical_name for key, child in children.items()):
            raise ValueError("resolved child catalog key must equal logical_name")
        if any(key != fragment.logical_path for key, fragment in fragments.items()):
            raise ValueError("resolved fragment catalog key must equal logical_path")
        object.__setattr__(self, "children", freeze(children))
        object.__setattr__(self, "fragments", freeze(fragments))

    def contract_for(self, name: str) -> ChildWorkflowContract | None:
        child = self.children.get(name)
        return child.contract if child is not None else None

    def child_for(self, name: str) -> ResolvedChild | None:
        return self.children.get(name)

    def fragment_for(self, logical_path: str) -> ResolvedFragment | None:
        return self.fragments.get(logical_path)


class WorkflowCatalog(Protocol):
    def contract_for(self, name: str) -> ChildWorkflowContract | None: ...


@dataclass(frozen=True)
class InMemoryWorkflowCatalog:
    """Small immutable catalog useful to compilers and tests."""

    contracts: Mapping[str, ChildWorkflowContract]

    def __post_init__(self) -> None:
        object.__setattr__(self, "contracts", freeze(self.contracts))

    def contract_for(self, name: str) -> ChildWorkflowContract | None:
        return self.contracts.get(name)


@dataclass(frozen=True)
class EffectContract:
    writes: tuple[str, ...] = ()

    def union(self, *others: "EffectContract") -> "EffectContract":
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
    """Structured loop boundary consumed by deterministic compiler lowering."""

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
        """Model only the terminal producer's routing, including final failure."""
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcomes", freeze(self.outcomes))
        object.__setattr__(self, "artifacts", freeze(self.artifacts))


class _Validator:
    def __init__(self, workflow: WorkflowIR, catalog: WorkflowCatalog) -> None:
        self.workflow = workflow
        self.catalog = catalog
        self.outcomes: dict[str, OutcomeSymbol] = {}
        self.artifacts: dict[str, ArtifactContract] = {}
        self.ids: set[str] = set()

    def fail(self, code: str, message: str, pointer: str, hint: str) -> None:
        location = self.workflow.location_for(pointer)
        raise DiagnosticError((Diagnostic(
            code, message, self.workflow.source_path or Path("<workflow>"),
            line=location.line if location else None,
            column=location.column if location else None,
            pointer=pointer, hint=hint,
        ),))

    def validate(self) -> ValidatedWorkflow:
        if self.workflow.version != "1":
            self.fail("LSW120", "only workflow_version '1' is supported", "/workflow_version", "use workflow_version: '1'")
        if self.workflow.protect != ("**",):
            self.fail("LSW301", "v1 workflows must protect the complete project", "/protect", 'use protect: ["**"]')
        flow = self.flow(self.workflow.flow, "/flow", {}, parallel=False)
        return ValidatedWorkflow(self.workflow, flow, self.outcomes, self.artifacts)

    def flow(
        self, blocks: tuple[BlockIR, ...], pointer: str, inherited: Mapping[str, OutcomeSymbol], *, parallel: bool
    ) -> FlowContract:
        symbols = dict(inherited)
        contracts: list[BlockContract | RepeatContract] = []
        effect = EffectContract()
        for index, block in enumerate(blocks):
            block_pointer = f"{pointer}/{index}"
            contract, produced = self.block(block, block_pointer, symbols, parallel=parallel)
            contracts.append(contract)
            effect = effect.union(contract.effects)
            symbols.update(produced)
            if not parallel:
                self.outcomes.update(produced)
        return FlowContract(tuple(contracts), effect)

    def block(
        self, block: BlockIR, pointer: str, symbols: Mapping[str, OutcomeSymbol], *, parallel: bool
    ) -> tuple[BlockContract | RepeatContract, Mapping[str, OutcomeSymbol]]:
        known_blocks = (StepIR, VerifyIR, DecideIR, ChooseIR, RepeatIR, CallIR, AcceptIR, ParallelIR, GraphIR, EscalateIR)
        if not isinstance(block, known_blocks):
            code = "LSP101" if parallel else "LSW120"
            self.fail(code, "unsupported Workflow DSL v1 block", pointer, "remove the unsupported block")
        if parallel and not isinstance(block, (VerifyIR, DecideIR, ChooseIR, CallIR, GraphIR)):
            self.fail("LSP101", "block is not permitted in a parallel branch", pointer, "use verify, decide, choose, call, or a read-only graph")
        self.track_id(block, pointer)
        if isinstance(block, StepIR):
            if parallel:
                self.fail("LSP101", "step is not permitted in a parallel branch", pointer, "move agent work outside parallel")
            self.handlers(block.on_failure, block.on_error, pointer)
            retry = block.retry or self.workflow.defaults.retry
            return BlockContract(block, EffectContract(block.writes), self.retry(retry, f"{pointer}/retry")), {}
        if isinstance(block, VerifyIR):
            self.handlers(block.on_failure, block.on_error, f"{pointer}/verify")
            try:
                argv = tuple(shlex.split(block.command))
                PinnedCommandSpec.build(
                    logical_argv=argv,
                    logical_cwd=block.cwd or ".",
                    result_source="exit",
                )
            except (TypeError, ValueError) as exc:
                self.fail(
                    "LSW301",
                    f"verify command contract is invalid: {exc}",
                    f"{pointer}/verify",
                    "use a bounded shell-free argv string and safe relative cwd",
                )
            produced = self.validator_symbol(block, pointer)
            retry = block.retry or self.workflow.defaults.retry
            return BlockContract(block, EffectContract(), self.retry(retry, f"{pointer}/verify/retry")), produced
        if isinstance(block, DecideIR):
            self.handlers(block.on_failure, block.on_error, f"{pointer}/decide")
            return BlockContract(block, EffectContract(), None), self.decision_symbol(block, pointer)
        if isinstance(block, ChooseIR):
            return self.choose(block, pointer, symbols, parallel=parallel)
        if isinstance(block, RepeatIR):
            if parallel:
                self.fail("LSP101", "repeat is not permitted in a parallel branch", pointer, "move the loop outside parallel")
            return self.repeat(block, pointer, symbols)
        if isinstance(block, CallIR):
            return self.call(block, pointer, parallel=parallel)
        if isinstance(block, AcceptIR):
            if parallel:
                self.fail("LSP101", "accept is not permitted in a parallel branch", pointer, "accept artifacts after the parallel join")
            return self.accept(block, pointer), {}
        if isinstance(block, ParallelIR):
            if parallel:
                self.fail("LSP101", "nested parallel is not available in Workflow DSL v1", pointer, "move the inner parallel block outside its branch")
            return self.parallel(block, pointer, symbols)
        if isinstance(block, GraphIR):
            if block.kind not in {"inline", "include"}:
                self.fail("LSW120", "invalid graph block kind", pointer, "use an inline graph or include_graph")
            writes = self.graph_effects(block, pointer)
            if parallel and writes:
                self.fail("LSP102", "parallel graph fragments must be read-only", pointer, "use a read-only graph fragment")
            return BlockContract(block, EffectContract(writes)), {}
        if isinstance(block, EscalateIR):
            return BlockContract(block, EffectContract()), {}
        self.fail("LSW120", "unsupported Workflow DSL v1 block", pointer, "remove the unsupported block")

    def handlers(self, on_failure: str | None, on_error: str | None, pointer: str) -> None:
        for key, value in (("on_failure", on_failure), ("on_error", on_error)):
            if value not in {None, "escalate"}:
                self.fail("LSW120", "v1 outcome handlers must be escalate", f"{pointer}/{key}", "use escalate or omit the handler")

    def track_id(self, block: BlockIR, pointer: str) -> None:
        if block.id is None:
            return
        if not _ID.fullmatch(block.id):
            self.fail("LSW110", f"invalid id {block.id!r}", pointer, "use a lowercase identifier")
        if block.id in self.ids:
            self.fail("LSW110", f"duplicate id {block.id!r}", pointer, "use a unique explicit id")
        self.ids.add(block.id)

    def retry(self, retry: RetryIR | None, pointer: str) -> RetryContract | None:
        if retry is None:
            return None
        if retry.limit < 1 or retry.exhausted != "escalate":
            self.fail("LSW303", "retry must have a positive total-execution limit and exhausted: escalate", pointer, "use a positive limit and exhausted: escalate")
        return RetryContract(retry.limit, retry.exhausted, retry.limit)

    def validator_symbol(self, block: VerifyIR, pointer: str) -> Mapping[str, OutcomeSymbol]:
        if block.id is None:
            return {}
        return {
            block.id: OutcomeSymbol(block.id, _TERMINAL_VALUES, OutcomeProvenance.VALIDATOR),
            f"{block.id}.passed": OutcomeSymbol(f"{block.id}.passed", ("pass", "fail", "error"), OutcomeProvenance.VALIDATOR),
        }

    def decision_symbol(self, block: DecideIR, pointer: str) -> Mapping[str, OutcomeSymbol]:
        using = dict(block.using)
        allowed = {"type", "since", "cases", "default"}
        if set(using) != allowed or using.get("type") != "changed-paths" or using.get("since") != "start":
            self.fail("LSW301", "decision providers must be Lockstep-owned changed-paths since start", f"{pointer}/decide/using", "remove project commands, evidence, and untrusted provider options")
        if block.id is None:
            self.fail("LSW301", "a trusted decision requires an explicit id", f"{pointer}/decide/id", "add a unique decision id")
        cases = using.get("cases")
        default = using.get("default")
        if (
            not isinstance(cases, Mapping)
            or not isinstance(default, str)
            or not _ID.fullmatch(default)
        ):
            self.fail("LSW301", "changed-paths requires typed cases and a default", f"{pointer}/decide/using", "provide case labels and a default")
        for label, paths in cases.items():
            if (
                not isinstance(label, str)
                or not _ID.fullmatch(label)
                or not isinstance(paths, tuple)
                or not paths
                or any(not isinstance(path, str) or not path for path in paths)
            ):
                self.fail("LSW301", "changed-paths cases must have logical labels and non-empty path lists", f"{pointer}/decide/using/cases", "use logical labels with at least one path")
        values = tuple(cases) + (default,)
        if len(set(values)) != len(values):
            self.fail("LSW301", "decision outcome labels must be unique", f"{pointer}/decide/using", "do not repeat the default as a case")
        return {block.id: OutcomeSymbol(block.id, values, OutcomeProvenance.DECISION)}

    def choose(
        self, block: ChooseIR, pointer: str, symbols: Mapping[str, OutcomeSymbol], *, parallel: bool
    ) -> tuple[BlockContract, Mapping[str, OutcomeSymbol]]:
        symbol = symbols.get(block.value)
        if symbol is None:
            self.fail("LSW301", "choose.value must reference a prior trusted outcome", f"{pointer}/choose/value", "use a decision, validator, child, or parallel result")
        unknown = [label for label in block.cases if label not in symbol.values]
        if unknown:
            self.fail("LSW302", f"choose case {unknown[0]!r} is not an outcome of {block.value!r}", f"{pointer}/choose/cases/{_escape(unknown[0])}", "use one of the declared outcome values")
        missing = [value for value in symbol.values if value not in block.cases]
        if missing and block.default is None:
            self.fail("LSW302", "choose cases must exhaust the trusted outcome enum or declare default", f"{pointer}/choose/cases", "add the missing cases or a default flow")
        base_artifacts = dict(self.artifacts)
        branch_effects: list[EffectContract] = []
        branch_contracts: dict[str, FlowContract] = {}
        branch_artifacts: list[dict[str, ArtifactContract]] = []
        for label, branch in block.cases.items():
            self.artifacts = dict(base_artifacts)
            branch_flow = self.flow(branch, f"{pointer}/choose/cases/{_escape(label)}", symbols, parallel=parallel)
            branch_contracts[label] = branch_flow
            branch_effects.append(branch_flow.effects)
            branch_artifacts.append(dict(self.artifacts))
        default_contract: FlowContract | None = None
        if block.default is not None:
            self.artifacts = dict(base_artifacts)
            default_contract = self.flow(block.default, f"{pointer}/choose/default", symbols, parallel=parallel)
            branch_effects.append(default_contract.effects)
            branch_artifacts.append(dict(self.artifacts))
        shared_handles = (
            tuple(handle for handle in branch_artifacts[0] if all(handle in view for view in branch_artifacts[1:]))
            if branch_artifacts else ()
        )
        self.artifacts = {
            **base_artifacts,
            **{handle: branch_artifacts[0][handle] for handle in shared_handles if handle not in base_artifacts},
        }
        effect = EffectContract().union(*branch_effects)
        return BlockContract(block, effect, branches=branch_contracts, default=default_contract, reconverges=True), {}

    def repeat(
        self, block: RepeatIR, pointer: str, symbols: Mapping[str, OutcomeSymbol]
    ) -> tuple[RepeatContract, Mapping[str, OutcomeSymbol]]:
        if block.limit < 1 or block.exhausted != "escalate":
            self.fail("LSW303", "repeat requires a positive limit and exhausted: escalate", f"{pointer}/repeat", "use a positive limit and exhausted: escalate")
        producer, suffix = self.repeat_target(block.until, pointer)
        if suffix != "passed":
            self.fail("LSW303", "repeat.until must reference the producer's .passed outcome", f"{pointer}/repeat/until", "use <verify-id>.passed")
        if not block.do:
            self.fail("LSW303", "repeat do must contain its terminal producer", f"{pointer}/repeat/do", "add the referenced final verify block")
        last = block.do[-1]
        if not isinstance(last, VerifyIR) or last.id != producer:
            self.fail("LSW303", "repeat.until must name the last normally reachable verify in do", f"{pointer}/repeat/until", "make the referenced verify the final block of every iteration")
        effective_retry = last.retry or self.workflow.defaults.retry
        if effective_retry is not None:
            self.fail("LSW303", "repeat terminal producer cannot retry", f"{pointer}/repeat/do/{len(block.do) - 1}/verify/retry", "remove retry from the terminal producer and workflow defaults")
        cardinalities = self.repeat_cardinalities(block.do, f"{pointer}/repeat/do", producer)
        if not cardinalities or any(count != 1 for count in cardinalities):
            self.fail("LSW303", "every repeat path must execute its terminal producer exactly once", f"{pointer}/repeat/until", "make every path reconverge through the final producer exactly once")
        # The normal path is intentionally linear in v1: the final producer is
        # parsed exactly once, and all earlier failure/error paths escalate.
        nested = self.flow(block.do, f"{pointer}/repeat/do", symbols, parallel=False)
        control = RepeatControlContract(producer, cardinalities)
        return RepeatContract(block.id, block.limit, block.until, block.exhausted, nested.effects, nested, control), {}

    def repeat_cardinalities(self, blocks: tuple[BlockIR, ...], pointer: str, producer: str) -> tuple[int, ...]:
        """Count producer executions on every structured path through one iteration."""
        cardinalities = (0,)
        for index, item in enumerate(blocks):
            item_pointer = f"{pointer}/{index}"
            if isinstance(item, EscalateIR):
                self.fail("LSW303", "repeat paths cannot bypass their terminal producer", item_pointer, "remove escalation from repeat do or move it after the repeat")
            if isinstance(item, ChooseIR):
                branches = [(label, branch, False) for label, branch in item.cases.items()]
                if item.default is not None:
                    branches.append(("", item.default, True))
                branch_counts: list[int] = []
                for label, branch, is_default in branches:
                    branch_pointer = (
                        f"{item_pointer}/choose/default"
                        if is_default else f"{item_pointer}/choose/cases/{_escape(label)}"
                    )
                    branch_counts.extend(self.repeat_cardinalities(branch, branch_pointer, producer))
                cardinalities = tuple(before + count for before in cardinalities for count in branch_counts)
                continue
            increment = int(isinstance(item, VerifyIR) and item.id == producer)
            cardinalities = tuple(count + increment for count in cardinalities)
        return cardinalities

    def repeat_target(self, value: str, pointer: str) -> tuple[str, str]:
        if value.count(".") != 1:
            self.fail("LSW303", "repeat.until must be a single producer outcome reference", f"{pointer}/repeat/until", "use <verify-id>.passed")
        producer, suffix = value.split(".")
        if not _ID.fullmatch(producer):
            self.fail("LSW303", "repeat.until has an invalid producer id", f"{pointer}/repeat/until", "use <verify-id>.passed")
        return producer, suffix

    def call(self, block: CallIR, pointer: str, *, parallel: bool) -> tuple[BlockContract, Mapping[str, OutcomeSymbol]]:
        if not _ID.fullmatch(block.workflow):
            self.fail("LSW304", "call workflow name is invalid", f"{pointer}/call/workflow", "use a logical workflow name")
        contract = self.catalog.contract_for(block.workflow)
        if contract is None:
            self.fail("LSW304", f"no child workflow contract is available for {block.workflow!r}", f"{pointer}/call/workflow", "compile and validate the child workflow first")
        if set(contract.outcomes) != set(_TERMINAL_VALUES) or len(contract.outcomes) != len(_TERMINAL_VALUES):
            self.fail("LSW304", "child workflow contracts must expose pass, fail, and error outcomes", f"{pointer}/call/workflow", "use a validated child terminal contract")
        if block.id is None and block.artifacts:
            self.fail("LSW304", "a call with artifacts requires an explicit id", f"{pointer}/call/id", "add a unique call id")
        self.handlers(block.on_failure, block.on_error, f"{pointer}/call")
        if contract.non_artifact_writes:
            if parallel:
                self.fail("LSP102", "parallel child calls may have no non-artifact writes", f"{pointer}/call", "use a child with only declared fixed artifact exports")
            self.fail("LSW304", "call contracts may expose only their declared parent artifacts", f"{pointer}/call", "remove child non-artifact writes or declare a fixed exported artifact")
        effect = EffectContract()
        for handle, destination in block.artifacts.items():
            export = contract.exports.get(handle)
            if export is None:
                self.fail("LSW304", f"child {block.workflow!r} does not export artifact {handle!r}", f"{pointer}/call/artifacts/{_escape(handle)}", "select a declared child export handle")
            if parallel:
                self.parallel_destination(destination, f"{pointer}/call/artifacts/{_escape(handle)}")
            qualified = self.qualified_handle(block.id or "", handle, pointer, parallel)
            if qualified in self.artifacts:
                self.fail("LSW304", f"duplicate qualified artifact handle {qualified!r}", f"{pointer}/call/artifacts/{_escape(handle)}", "use a unique call and artifact handle")
            artifact = ArtifactContract(qualified, export.fixed_source, destination)
            self.artifacts[qualified] = artifact
            effect = effect.union(EffectContract((destination,)))
        outcomes: Mapping[str, OutcomeSymbol] = {}
        if block.id is not None:
            outcomes = {block.id: OutcomeSymbol(block.id, tuple(contract.outcomes), OutcomeProvenance.CHILD)}
        return BlockContract(block, effect, self.retry(None, f"{pointer}/call/retry")), outcomes

    def qualified_handle(self, call_id: str, handle: str, pointer: str, parallel: bool) -> str:
        # Branch qualification is applied by parallel(), which has the branch
        # identity.  Calls in a regular flow are already globally unique.
        if not _ID.fullmatch(handle):
            self.fail("LSW304", "artifact export handle is invalid", f"{pointer}/call/artifacts/{_escape(handle)}", "use a logical export handle")
        return call_id + "." + handle

    def accept(self, block: AcceptIR, pointer: str) -> BlockContract:
        if block.verdict != "PASS":
            self.fail("LSW108", "accept verdict must be PASS", f"{pointer}/accept/verdict", "use verdict: PASS")
        if not isinstance(block.artifact_from, str) or not block.artifact_from:
            self.fail("LSW108", "accept.artifact_from must be non-empty", f"{pointer}/accept/artifact_from", "provide a resolved artifact handle")
        if block.artifact_from not in self.artifacts:
            self.fail("LSW304", "accept.artifact_from must reference a resolved parallel artifact", f"{pointer}/accept/artifact_from", "reference a joined qualified artifact handle")
        return BlockContract(block, EffectContract())

    def parallel(
        self, block: ParallelIR, pointer: str, symbols: Mapping[str, OutcomeSymbol]
    ) -> tuple[BlockContract, Mapping[str, OutcomeSymbol]]:
        if block.id is None:
            self.fail("LSP101", "parallel requires an explicit id", f"{pointer}/parallel/id", "add a unique parallel id")
        if block.join != "all":
            self.fail("LSP101", "only join: all is available in Workflow DSL v1", f"{pointer}/parallel/join", "use join: all")
        if not 2 <= len(block.branches) <= 8:
            self.fail("LSP101", "parallel requires between 2 and 8 branches", f"{pointer}/parallel/branches", "declare 2 through 8 branches")
        self.handlers(block.on_failure, block.on_error, f"{pointer}/parallel")
        base_artifacts = dict(self.artifacts)
        published_artifacts: dict[str, ArtifactContract] = {}
        effects: list[EffectContract] = []
        branch_contracts: dict[str, FlowContract] = {}
        for name, branch in block.branches.items():
            if not _ID.fullmatch(name):
                self.fail("LSP101", "parallel branch name is invalid", f"{pointer}/parallel/branches/{_escape(name)}", "use a lowercase branch identifier")
            # Artifact registration is shared so downstream accepts resolve;
            # rename any branch-local call handles into the required namespace.
            self.artifacts = dict(base_artifacts)
            branch_flow = self.flow(branch, f"{pointer}/parallel/branches/{_escape(name)}", symbols, parallel=True)
            branch_contracts[name] = branch_flow
            for handle in tuple(handle for handle in self.artifacts if handle not in base_artifacts):
                artifact = self.artifacts.pop(handle)
                qualified = f"{block.id}.{name}.{handle}"
                if qualified in published_artifacts:
                    self.fail("LSP102", f"duplicate parallel artifact handle {qualified!r}", f"{pointer}/parallel/branches/{_escape(name)}", "use unique branch/call/export identities")
                published_artifacts[qualified] = ArtifactContract(qualified, artifact.source, artifact.destination)
            effects.append(branch_flow.effects)
        self.artifacts = {**base_artifacts, **published_artifacts}
        # Work is isolated while branches run.  Effects become engine-owned
        # publications at join, so no branch write surface is granted here.
        return BlockContract(block, EffectContract(), branches=branch_contracts, reconverges=True), {
            block.id: OutcomeSymbol(block.id, _TERMINAL_VALUES, OutcomeProvenance.PARALLEL)
        }

    def parallel_destination(self, destination: str, pointer: str) -> None:
        parts = destination.split("/")
        if not destination or destination.startswith("/") or any(part in {"", ".", ".."} for part in parts):
            self.fail("LSP102", "parallel artifact destinations must be safe relative subpaths", pointer, "use a non-empty relative artifact subpath")

    def graph_effects(self, block: GraphIR, pointer: str) -> tuple[str, ...]:
        if block.kind == "include":
            resolver = getattr(self.catalog, "fragment_for", None)
            resolved = resolver(block.path) if callable(resolver) else None
            if resolved is None:
                self.fail(
                    "LSG201",
                    "include_graph requires a resolved closed fragment",
                    f"{pointer}/include_graph/path",
                    "resolve the contained fragment before semantic validation",
                )
                return ()
            graph = resolved.fragment.document
            fragment_metadata = graph.get("fragment", {})
            exits = (
                fragment_metadata.get("exits", {})
                if isinstance(fragment_metadata, Mapping)
                else {}
            )
            unknown_routes = set(block.authored_on) - set(exits)
            if unknown_routes:
                self.fail(
                    "LSW108",
                    "include_graph on names an undeclared fragment exit",
                    f"{pointer}/include_graph/on",
                    "remove handlers for exits the resolved fragment does not declare",
                )
        else:
            graph = block.graph or {}
        fragment = graph.get("fragment", {}) if isinstance(graph, Mapping) else {}
        effects = fragment.get("effects", {}) if isinstance(fragment, Mapping) else {}
        writes = effects.get("writes", ()) if isinstance(effects, Mapping) else ()
        return tuple(writes) if isinstance(writes, (list, tuple)) else ()


def validate_semantics(workflow: WorkflowIR, catalog: WorkflowCatalog) -> ValidatedWorkflow:
    """Validate trust, structured control flow, closed effects, and child contracts."""
    return _Validator(workflow, catalog).validate()
