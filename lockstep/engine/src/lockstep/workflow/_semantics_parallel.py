"""Static parallel-branch validation and artifact publication projection."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from lockstep.runtime.owner_state import StorageLimitExceeded
from lockstep.runtime.project_paths import (
    PortablePathError,
    PortableProjectPath,
    ProjectTreeLimits,
    portable_collision_key,
    validate_portable_project_paths,
)

from ._semantics_common import handlers
from ._semantics_contracts import (
    _ID,
    _TERMINAL_VALUES,
    ArtifactContract,
    BlockContract,
    EffectContract,
    FlowContract,
    OutcomeProvenance,
    OutcomeSymbol,
    _escape,
)
from ._semantics_validation import _ValidationState, fail
from .ir import ParallelIR, StepIR


def parallel(
    state: _ValidationState,
    block: ParallelIR,
    pointer: str,
    symbols: Mapping[str, OutcomeSymbol],
    *,
    flow: Callable[..., FlowContract],
) -> tuple[BlockContract, Mapping[str, OutcomeSymbol]]:
    parallel_header(state, block, pointer)
    base_artifacts = dict(state.artifacts)
    published_artifacts, branch_destinations, branch_contracts = collect_parallel_branches(
        state, block, pointer, symbols, base_artifacts, flow=flow
    )
    validate_parallel_destinations(state, branch_destinations, pointer)
    validate_parallel_writes(state, branch_contracts, pointer)
    effects = EffectContract().union(*(
        manual_effects(branch) for branch in branch_contracts.values()
    ))
    state.artifacts = {**base_artifacts, **published_artifacts}
    return BlockContract(block, effects, branches=branch_contracts, reconverges=True), {
        block.id: OutcomeSymbol(block.id, _TERMINAL_VALUES, OutcomeProvenance.PARALLEL)
    }


def manual_effects(flow: FlowContract, *, include_artifacts: bool = False) -> EffectContract:
    """Live manual writes exclude child artifact destinations awaiting acceptance."""
    effects = EffectContract()
    for contract in flow.blocks:
        if isinstance(contract.block, StepIR):
            effects = effects.union(
                EffectContract(contract.block.writes) if include_artifacts else contract.effects
            )
        for branch in contract.branches.values():
            effects = effects.union(
                manual_effects(branch, include_artifacts=include_artifacts)
            )
        if contract.default is not None:
            effects = effects.union(
                manual_effects(contract.default, include_artifacts=include_artifacts)
            )
    return effects


def validate_parallel_writes(
    state: _ValidationState,
    branches: Mapping[str, FlowContract],
    pointer: str,
) -> None:
    """Sequential writes may overlap; distinct branches must remain disjoint."""
    seen: list[tuple[str, str]] = []
    effects = EffectContract()
    try:
        for branch, contract in branches.items():
            branch_effects = contract.effects.union(
                manual_effects(contract, include_artifacts=True)
            )
            effects = effects.union(branch_effects)
            for write in branch_effects.writes:
                path = PortableProjectPath.parse(
                    write, "prefix" if write.endswith("/") else "file"
                )
                key = portable_collision_key(path.relative.as_posix())
                for other_branch, other in seen:
                    if other_branch != branch and (
                        key == other
                        or key.startswith(other + "/")
                        or other.startswith(key + "/")
                    ):
                        raise PortablePathError(
                            f"{branch!r} and {other_branch!r} overlap at {write!r}"
                        )
                seen.append((branch, key))
        validate_portable_project_paths(
            ((write, "prefix" if write.endswith("/") else "file")
             for write in effects.writes),
            limits=ProjectTreeLimits(), label="parallel writes",
        )
    except (PortablePathError, StorageLimitExceeded) as exc:
        fail(
            state, "LSP102", f"parallel writes overlap or alias: {exc}",
            f"{pointer}/parallel/branches",
            "use disjoint portable write paths across branches",
        )


def parallel_header(state: _ValidationState, block: ParallelIR, pointer: str) -> None:
    if block.id is None:
        fail(state, "LSP101", "parallel requires an explicit id", f"{pointer}/parallel/id", "add a unique parallel id")
    if block.join != "all":
        fail(state, "LSP101", "only join: all is available in Workflow DSL v1", f"{pointer}/parallel/join", "use join: all")
    if not 2 <= len(block.branches) <= 8:
        fail(state, "LSP101", "parallel requires between 2 and 8 branches", f"{pointer}/parallel/branches", "declare 2 through 8 branches")
    handlers(state, block.on_failure, block.on_error, f"{pointer}/parallel")


def collect_parallel_branches(
    state: _ValidationState,
    block: ParallelIR,
    pointer: str,
    symbols: Mapping[str, OutcomeSymbol],
    base_artifacts: dict[str, ArtifactContract],
    *,
    flow: Callable[..., FlowContract],
) -> tuple[
    dict[str, ArtifactContract],
    list[tuple[str, str]],
    dict[str, FlowContract],
]:
    published_artifacts: dict[str, ArtifactContract] = {}
    branch_destinations: list[tuple[str, str]] = []
    branch_contracts: dict[str, FlowContract] = {}
    for name, branch in block.branches.items():
        if not _ID.fullmatch(name):
            fail(state, "LSP101", "parallel branch name is invalid", f"{pointer}/parallel/branches/{_escape(name)}", "use a lowercase branch identifier")
        state.artifacts = dict(base_artifacts)
        branch_flow = flow(state, branch, f"{pointer}/parallel/branches/{_escape(name)}", symbols, parallel=True)
        branch_contracts[name] = branch_flow
        for handle in tuple(handle for handle in state.artifacts if handle not in base_artifacts):
            artifact = state.artifacts.pop(handle)
            qualified = f"{block.id}.{name}.{handle}"
            if qualified in published_artifacts:
                fail(state, "LSP102", f"duplicate parallel artifact handle {qualified!r}", f"{pointer}/parallel/branches/{_escape(name)}", "use unique branch/call/export identities")
            published_artifacts[qualified] = ArtifactContract(qualified, artifact.source, artifact.destination)
            branch_destinations.append((name, artifact.destination))
    return published_artifacts, branch_destinations, branch_contracts


def validate_parallel_destinations(
    state: _ValidationState,
    branch_destinations: list[tuple[str, str]],
    pointer: str,
) -> None:
    try:
        validate_portable_project_paths(
            ((destination, "file") for _branch, destination in branch_destinations),
            limits=ProjectTreeLimits(max_entries=64),
            label="parallel artifact destinations",
        )
    except (PortablePathError, StorageLimitExceeded) as exc:
        fail(
            state, "LSP102", f"parallel artifact destinations overlap or alias: {exc}",
            f"{pointer}/parallel/branches",
            "use statically non-overlapping portable artifact destinations",
        )


def parallel_destination(state: _ValidationState, destination: str, pointer: str) -> None:
    parts = destination.split("/")
    if not destination or destination.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        fail(state, "LSP102", "parallel artifact destinations must be safe relative subpaths", pointer, "use a non-empty relative artifact subpath")
