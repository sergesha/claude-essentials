"""Flow traversal and block dispatch for workflow semantics."""

from __future__ import annotations

import shlex
from collections.abc import Mapping

from lockstep.runtime.effects.models import PinnedCommandSpec
from lockstep.runtime.project_paths import PortablePathError, PortableProjectPath

from ._semantics_calls import call
from ._semantics_catalog import ChildArtifactContract
from ._semantics_common import handlers, retry
from ._semantics_contracts import (
    _ID,
    _TERMINAL_VALUES,
    BlockContract,
    EffectContract,
    FlowContract,
    OutcomeProvenance,
    OutcomeSymbol,
    RepeatContract,
)
from ._semantics_decisions import choose, decision_symbol
from ._semantics_parallel import parallel as validate_parallel
from ._semantics_repeats import repeat
from ._semantics_validation import _ValidationState, fail
from .ir import (
    AcceptIR,
    BlockIR,
    CallIR,
    ChooseIR,
    DecideIR,
    EscalateIR,
    ExportedArtifactIR,
    GraphIR,
    ParallelIR,
    RepeatIR,
    StepIR,
    VerifyIR,
)


def _artifact_path(
    state: _ValidationState, artifact: ExportedArtifactIR, pointer: str
) -> PortableProjectPath:
    try:
        return PortableProjectPath.parse(artifact.path, "file")
    except PortablePathError as exc:
        fail(
            state,
            "LSW305",
            f"artifact path must be one safe exact project-relative file: {exc}",
            f"{pointer}/artifact/path",
            "use a canonical contained project-relative file",
        )


def _write_covers_artifact(write: str, exported_path: PortableProjectPath) -> bool:
    try:
        declared = PortableProjectPath.parse(
            write, "prefix" if write.endswith("/") else "file"
        )
    except PortablePathError:
        return False
    if declared.kind == "file":
        return declared.relative == exported_path.relative
    return (
        declared.relative == exported_path.relative
        or declared.relative in exported_path.relative.parents
    )


def _require_artifact_write(
    state: _ValidationState,
    item: StepIR,
    exported_path: PortableProjectPath,
    pointer: str,
) -> None:
    if any(_write_covers_artifact(write, exported_path) for write in item.writes):
        return
    fail(
        state,
        "LSW305",
        "artifact path must be covered by the same step's writes",
        f"{pointer}/artifact/path",
        "add the exact artifact path or a containing write prefix",
    )


def _register_artifact_export(
    state: _ValidationState,
    item: StepIR,
    artifact: ExportedArtifactIR,
    pointer: str,
) -> None:
    logical = item.id or item.step
    collisions = (
        (
            artifact.handle in state.exports,
            f"duplicate exported artifact handle {artifact.handle!r}",
            f"{pointer}/artifact/handle",
            "use a unique artifact handle",
        ),
        (
            artifact.path in state.export_paths,
            f"duplicate exported artifact path {artifact.path!r}",
            f"{pointer}/artifact/path",
            "use a unique exported artifact path",
        ),
        (
            logical in state.export_producers,
            f"artifact producer {logical!r} is already claimed",
            f"{pointer}/artifact",
            "use a unique step id or producer",
        ),
    )
    for collided, message, location, remedy in collisions:
        if collided:
            fail(state, "LSW304", message, location, remedy)
    result_key = f"{logical.replace('-', '_')}_result"
    state.exports[artifact.handle] = ChildArtifactContract(
        artifact.handle,
        artifact.path,
        artifact.handle,
        "text/markdown",
        logical,
        result_key,
    )
    state.export_paths.add(artifact.path)
    state.export_producers.add(logical)


def flow(
    state: _ValidationState,
    blocks: tuple[BlockIR, ...],
    pointer: str,
    inherited: Mapping[str, OutcomeSymbol],
    *,
    parallel: bool,
) -> FlowContract:
    symbols = dict(inherited)
    contracts: list[BlockContract | RepeatContract] = []
    effect = EffectContract()
    for index, item in enumerate(blocks):
        block_pointer = f"{pointer}/{index}"
        contract, produced = block(state, item, block_pointer, symbols, parallel=parallel)
        contracts.append(contract)
        effect = effect.union(contract.effects)
        symbols.update(produced)
        if not parallel:
            state.outcomes.update(produced)
    return FlowContract(tuple(contracts), effect)


def block(
    state: _ValidationState,
    item: BlockIR,
    pointer: str,
    symbols: Mapping[str, OutcomeSymbol],
    *,
    parallel: bool,
) -> tuple[BlockContract | RepeatContract, Mapping[str, OutcomeSymbol]]:
    known_blocks = (StepIR, VerifyIR, DecideIR, ChooseIR, RepeatIR, CallIR, AcceptIR, ParallelIR, GraphIR, EscalateIR)
    if not isinstance(item, known_blocks):
        code = "LSP101" if parallel else "LSW120"
        fail(state, code, "unsupported Workflow DSL v1 block", pointer, "remove the unsupported block")
    if parallel and not isinstance(item, (VerifyIR, ChooseIR, CallIR, GraphIR)):
        fail(state, "LSP101", "block is not permitted in a parallel branch", pointer, "use verify, choose, call, or a read-only graph")
    track_id(state, item, pointer)
    if isinstance(item, (StepIR, VerifyIR, DecideIR, ChooseIR, RepeatIR)):
        return control_block(state, item, pointer, symbols, parallel=parallel)
    return effect_block(state, item, pointer, symbols, parallel=parallel)


def control_block(
    state: _ValidationState, item: BlockIR, pointer: str,
    symbols: Mapping[str, OutcomeSymbol], *, parallel: bool,
) -> tuple[BlockContract | RepeatContract, Mapping[str, OutcomeSymbol]]:
    if isinstance(item, StepIR):
        handlers(state, item.on_failure, item.on_error, pointer)
        retry_ir = item.retry or state.workflow.defaults.retry
        writes = step_writes(state, item, pointer)
        return BlockContract(item, EffectContract(writes), retry(state, retry_ir, f"{pointer}/retry")), {}
    if isinstance(item, VerifyIR):
        handlers(state, item.on_failure, item.on_error, f"{pointer}/verify")
        try:
            argv = tuple(shlex.split(item.command))
            PinnedCommandSpec.build(logical_argv=argv, logical_cwd=item.cwd or ".", result_source="exit")
        except (TypeError, ValueError) as exc:
            fail(state, "LSW301", f"verify command contract is invalid: {exc}", f"{pointer}/verify", "use a bounded shell-free argv string and safe relative cwd")
        produced = validator_symbol(item)
        retry_ir = item.retry or state.workflow.defaults.retry
        return BlockContract(item, EffectContract(), retry(state, retry_ir, f"{pointer}/verify/retry")), produced
    if isinstance(item, DecideIR):
        handlers(state, item.on_failure, item.on_error, f"{pointer}/decide")
        return BlockContract(item, EffectContract(), None), decision_symbol(state, item, pointer)
    if isinstance(item, ChooseIR):
        return choose(state, item, pointer, symbols, parallel=parallel, flow=flow)
    if isinstance(item, RepeatIR):
        return repeat(state, item, pointer, symbols, flow=flow)
    fail(state, "LSW120", "unsupported Workflow DSL v1 block", pointer, "remove the unsupported block")


def step_writes(
    state: _ValidationState, item: StepIR, pointer: str
) -> tuple[str, ...]:
    artifact = item.artifact
    if artifact is None:
        return item.writes
    exported_path = _artifact_path(state, artifact, pointer)
    _require_artifact_write(state, item, exported_path, pointer)
    _register_artifact_export(state, item, artifact, pointer)
    return tuple(write for write in item.writes if write != artifact.path)


def effect_block(
    state: _ValidationState, item: BlockIR, pointer: str,
    symbols: Mapping[str, OutcomeSymbol], *, parallel: bool,
) -> tuple[BlockContract | RepeatContract, Mapping[str, OutcomeSymbol]]:
    if isinstance(item, CallIR):
        return call(state, item, pointer, parallel=parallel)
    if isinstance(item, AcceptIR):
        return accept(state, item, pointer), {}
    if isinstance(item, ParallelIR):
        return validate_parallel(state, item, pointer, symbols, flow=flow)
    if isinstance(item, GraphIR):
        if item.kind not in {"inline", "include"}:
            fail(state, "LSW120", "invalid graph block kind", pointer, "use an inline graph or include_graph")
        writes = graph_effects(state, item, pointer)
        if parallel and writes:
            fail(state, "LSP102", "parallel graph fragments must be read-only", pointer, "use a read-only graph fragment")
        return BlockContract(item, EffectContract(writes)), {}
    if isinstance(item, EscalateIR):
        return BlockContract(item, EffectContract()), {}
    fail(state, "LSW120", "unsupported Workflow DSL v1 block", pointer, "remove the unsupported block")


def track_id(state: _ValidationState, item: BlockIR, pointer: str) -> None:
    if item.id is None:
        return
    if not _ID.fullmatch(item.id):
        fail(state, "LSW110", f"invalid id {item.id!r}", pointer, "use a lowercase identifier")
    if item.id in state.ids:
        fail(state, "LSW110", f"duplicate id {item.id!r}", pointer, "use a unique explicit id")
    state.ids.add(item.id)


def validator_symbol(item: VerifyIR) -> Mapping[str, OutcomeSymbol]:
    if item.id is None:
        return {}
    return {
        item.id: OutcomeSymbol(item.id, _TERMINAL_VALUES, OutcomeProvenance.VALIDATOR),
        f"{item.id}.passed": OutcomeSymbol(f"{item.id}.passed", ("pass", "fail", "error"), OutcomeProvenance.VALIDATOR),
    }


def accept(state: _ValidationState, item: AcceptIR, pointer: str) -> BlockContract:
    if item.verdict != "PASS":
        fail(state, "LSW108", "accept verdict must be PASS", f"{pointer}/accept/verdict", "use verdict: PASS")
    if not isinstance(item.artifact_from, str) or not item.artifact_from:
        fail(state, "LSW108", "accept.artifact_from must be non-empty", f"{pointer}/accept/artifact_from", "provide a resolved artifact handle")
    if item.artifact_from not in state.artifacts:
        fail(state, "LSW304", "accept.artifact_from must reference a resolved parallel artifact", f"{pointer}/accept/artifact_from", "reference a joined qualified artifact handle")
    return BlockContract(item, EffectContract())


def graph_effects(state: _ValidationState, item: GraphIR, pointer: str) -> tuple[str, ...]:
    if item.kind == "include":
        resolver = getattr(state.catalog, "fragment_for", None)
        resolved = resolver(item.path) if callable(resolver) else None
        if resolved is None:
            fail(state, "LSG201", "include_graph requires a resolved closed fragment", f"{pointer}/include_graph/path", "resolve the contained fragment before semantic validation")
        graph = resolved.fragment.document
        fragment_metadata = graph.get("fragment", {})
        exits = fragment_metadata.get("exits", {}) if isinstance(fragment_metadata, Mapping) else {}
        unknown_routes = set(item.authored_on) - set(exits)
        if unknown_routes:
            fail(state, "LSW108", "include_graph on names an undeclared fragment exit", f"{pointer}/include_graph/on", "remove handlers for exits the resolved fragment does not declare")
    else:
        graph = item.graph or {}
    fragment = graph.get("fragment", {}) if isinstance(graph, Mapping) else {}
    effects = fragment.get("effects", {}) if isinstance(fragment, Mapping) else {}
    writes = effects.get("writes", ()) if isinstance(effects, Mapping) else ()
    return tuple(writes) if isinstance(writes, (list, tuple)) else ()
