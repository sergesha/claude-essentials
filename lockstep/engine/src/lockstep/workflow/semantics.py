"""Semantic contracts for the structured Workflow DSL.

The parser deliberately accepts only structural data.  This module is the
next, pure phase: it turns that immutable tree into compiler-ready contracts
without reading project files or consulting runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import Mapping, Protocol

from .diagnostics import Diagnostic, DiagnosticError
from .ir import (
    AcceptIR, BlockIR, CallIR, ChooseIR, DecideIR, EscalateIR, GraphIR,
    ParallelIR, RepeatIR, RetryIR, StepIR, VerifyIR, WorkflowIR, freeze,
)


_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_TERMINAL_VALUES = ("pass", "fail", "error")


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcomes", tuple(self.outcomes))
        object.__setattr__(self, "exports", freeze(self.exports))
        object.__setattr__(self, "non_artifact_writes", tuple(self.non_artifact_writes))


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
            produced = self.validator_symbol(block, pointer)
            retry = block.retry or self.workflow.defaults.retry
            return BlockContract(block, EffectContract(block.writes), self.retry(retry, f"{pointer}/verify/retry")), produced
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
        if not isinstance(cases, Mapping) or not isinstance(default, str) or not default:
            self.fail("LSW301", "changed-paths requires typed cases and a default", f"{pointer}/decide/using", "provide case labels and a default")
        values = tuple(str(case) for case in cases) + (default,)
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
            self.fail("LSW302", f"choose case {unknown[0]!r} is not an outcome of {block.value!r}", f"{pointer}/choose/cases/{unknown[0]}", "use one of the declared outcome values")
        missing = [value for value in symbol.values if value not in block.cases]
        if missing and block.default is None:
            self.fail("LSW302", "choose cases must exhaust the trusted outcome enum or declare default", f"{pointer}/choose/cases", "add the missing cases or a default flow")
        base_artifacts = dict(self.artifacts)
        branch_effects: list[EffectContract] = []
        branch_contracts: dict[str, FlowContract] = {}
        branch_artifacts: list[dict[str, ArtifactContract]] = []
        for label, branch in block.cases.items():
            self.artifacts = dict(base_artifacts)
            branch_flow = self.flow(branch, f"{pointer}/choose/cases/{label}", symbols, parallel=parallel)
            branch_contracts[label] = branch_flow
            branch_effects.append(branch_flow.effects)
            branch_artifacts.append(dict(self.artifacts))
        default_contract: FlowContract | None = None
        if block.default is not None:
            self.artifacts = dict(base_artifacts)
            default_contract = self.flow(block.default, f"{pointer}/choose/default", symbols, parallel=parallel)
            branch_effects.append(default_contract.effects)
            branch_artifacts.append(dict(self.artifacts))
        shared_handles = set.intersection(*(set(view) for view in branch_artifacts)) if branch_artifacts else set()
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
                branches = list(item.cases.items())
                if item.default is not None:
                    branches.append(("default", item.default))
                branch_counts: list[int] = []
                for label, branch in branches:
                    branch_pointer = f"{item_pointer}/choose/{'default' if label == 'default' else f'cases/{label}'}"
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
                self.fail("LSW304", f"child {block.workflow!r} does not export artifact {handle!r}", f"{pointer}/call/artifacts/{handle}", "select a declared child export handle")
            if parallel:
                self.parallel_destination(destination, f"{pointer}/call/artifacts/{handle}")
            qualified = self.qualified_handle(block.id or "", handle, pointer, parallel)
            if qualified in self.artifacts:
                self.fail("LSW304", f"duplicate qualified artifact handle {qualified!r}", f"{pointer}/call/artifacts/{handle}", "use a unique call and artifact handle")
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
            self.fail("LSW304", "artifact export handle is invalid", f"{pointer}/call/artifacts/{handle}", "use a logical export handle")
        return call_id + "." + handle

    def accept(self, block: AcceptIR, pointer: str) -> BlockContract:
        if block.hash_from is not None:
            artifact = self.artifacts.get(block.hash_from)
            if artifact is None:
                self.fail("LSW304", "accept.hash_from must reference a resolved call artifact", f"{pointer}/accept/hash_from", "reference <call-id>.<export-handle>")
            if block.artifact != artifact.destination:
                self.fail("LSW304", "accept artifact must equal its resolved call destination", f"{pointer}/accept/artifact", "use the fixed destination declared by the call")
        elif block.artifact_from is not None and block.artifact_from not in self.artifacts:
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
                self.fail("LSP101", "parallel branch name is invalid", f"{pointer}/parallel/branches/{name}", "use a lowercase branch identifier")
            # Artifact registration is shared so downstream accepts resolve;
            # rename any branch-local call handles into the required namespace.
            self.artifacts = dict(base_artifacts)
            branch_flow = self.flow(branch, f"{pointer}/parallel/branches/{name}", symbols, parallel=True)
            branch_contracts[name] = branch_flow
            for handle in tuple(set(self.artifacts) - set(base_artifacts)):
                artifact = self.artifacts.pop(handle)
                qualified = f"{block.id}.{name}.{handle}"
                if qualified in published_artifacts:
                    self.fail("LSP102", f"duplicate parallel artifact handle {qualified!r}", f"{pointer}/parallel/branches/{name}", "use unique branch/call/export identities")
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
            # The resolver supplies include effects in the next compiler phase;
            # routing validation still has the typed parser contract here.
            return ()
        graph = block.graph or {}
        fragment = graph.get("fragment", {}) if isinstance(graph, Mapping) else {}
        effects = fragment.get("effects", {}) if isinstance(fragment, Mapping) else {}
        writes = effects.get("writes", ()) if isinstance(effects, Mapping) else ()
        return tuple(writes) if isinstance(writes, tuple) else ()


def validate_semantics(workflow: WorkflowIR, catalog: WorkflowCatalog) -> ValidatedWorkflow:
    """Validate trust, structured control flow, closed effects, and child contracts."""
    return _Validator(workflow, catalog).validate()
