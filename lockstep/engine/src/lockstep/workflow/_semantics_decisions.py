"""Trusted decision and choose validation."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ._semantics_contracts import (
    _ID,
    ArtifactContract,
    BlockContract,
    EffectContract,
    FlowContract,
    OutcomeProvenance,
    OutcomeSymbol,
    _escape,
)
from ._semantics_validation import _ValidationState, fail
from .ir import ChooseIR, DecideIR


def decision_symbol(
    state: _ValidationState, block: DecideIR, pointer: str
) -> Mapping[str, OutcomeSymbol]:
    using = dict(block.using)
    cases, default = decision_header(state, block, pointer, using)
    decision_cases(state, pointer, cases, default)
    values = tuple(cases) + (default,)
    return {block.id: OutcomeSymbol(block.id, values, OutcomeProvenance.DECISION)}


def decision_header(
    state: _ValidationState,
    block: DecideIR,
    pointer: str,
    using: dict[object, object],
) -> tuple[Mapping[object, object], str]:
    allowed = {"type", "since", "cases", "default"}
    if set(using) != allowed or using.get("type") != "changed-paths" or using.get("since") != "start":
        fail(state, "LSW301", "decision providers must be Lockstep-owned changed-paths since start", f"{pointer}/decide/using", "remove project commands, evidence, and untrusted provider options")
    if block.id is None:
        fail(state, "LSW301", "a trusted decision requires an explicit id", f"{pointer}/decide/id", "add a unique decision id")
    cases = using.get("cases")
    default = using.get("default")
    if (
        not isinstance(cases, Mapping)
        or not isinstance(default, str)
        or not _ID.fullmatch(default)
    ):
        fail(state, "LSW301", "changed-paths requires typed cases and a default", f"{pointer}/decide/using", "provide case labels and a default")
    return cases, default


def decision_cases(
    state: _ValidationState,
    pointer: str,
    cases: Mapping[object, object],
    default: str,
) -> None:
    for label, paths in cases.items():
        if (
            not isinstance(label, str)
            or not _ID.fullmatch(label)
            or not isinstance(paths, tuple)
            or not paths
            or any(not isinstance(path, str) or not path for path in paths)
        ):
            fail(state, "LSW301", "changed-paths cases must have logical labels and non-empty path lists", f"{pointer}/decide/using/cases", "use logical labels with at least one path")
    values = tuple(cases) + (default,)
    if len(set(values)) != len(values):
        fail(state, "LSW301", "decision outcome labels must be unique", f"{pointer}/decide/using", "do not repeat the default as a case")


def choose(
    state: _ValidationState,
    block: ChooseIR,
    pointer: str,
    symbols: Mapping[str, OutcomeSymbol],
    *,
    parallel: bool,
    flow: Callable[..., FlowContract],
) -> tuple[BlockContract, Mapping[str, OutcomeSymbol]]:
    symbol = symbols.get(block.value)
    if symbol is None:
        fail(state, "LSW301", "choose.value must reference a prior trusted outcome", f"{pointer}/choose/value", "use a decision, validator, child, or parallel result")
    unknown = [label for label in block.cases if label not in symbol.values]
    if unknown:
        fail(state, "LSW302", f"choose case {unknown[0]!r} is not an outcome of {block.value!r}", f"{pointer}/choose/cases/{_escape(unknown[0])}", "use one of the declared outcome values")
    missing = [value for value in symbol.values if value not in block.cases]
    if missing and block.default is None:
        fail(state, "LSW302", "choose cases must exhaust the trusted outcome enum or declare default", f"{pointer}/choose/cases", "add the missing cases or a default flow")
    base_artifacts = dict(state.artifacts)
    branch_effects: list[EffectContract] = []
    branch_contracts: dict[str, FlowContract] = {}
    branch_artifacts: list[dict[str, ArtifactContract]] = []
    for label, branch in block.cases.items():
        state.artifacts = dict(base_artifacts)
        branch_flow = flow(state, branch, f"{pointer}/choose/cases/{_escape(label)}", symbols, parallel=parallel)
        branch_contracts[label] = branch_flow
        branch_effects.append(branch_flow.effects)
        branch_artifacts.append(dict(state.artifacts))
    default_contract: FlowContract | None = None
    if block.default is not None:
        state.artifacts = dict(base_artifacts)
        default_contract = flow(state, block.default, f"{pointer}/choose/default", symbols, parallel=parallel)
        branch_effects.append(default_contract.effects)
        branch_artifacts.append(dict(state.artifacts))
    shared_handles = (
        tuple(handle for handle in branch_artifacts[0] if all(handle in view for view in branch_artifacts[1:]))
        if branch_artifacts else ()
    )
    state.artifacts = {
        **base_artifacts,
        **{handle: branch_artifacts[0][handle] for handle in shared_handles if handle not in base_artifacts},
    }
    effect = EffectContract().union(*branch_effects)
    return BlockContract(block, effect, branches=branch_contracts, default=default_contract, reconverges=True), {}
