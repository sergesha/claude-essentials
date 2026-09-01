"""Closed child-call and artifact validation."""

from __future__ import annotations

from collections.abc import Mapping

from lockstep.runtime.owner_state import StorageLimitExceeded
from lockstep.runtime.project_paths import (
    PortablePathError,
    ProjectTreeLimits,
    validate_portable_project_paths,
)

from ._semantics_common import handlers, retry
from ._semantics_contracts import (
    _ID,
    _TERMINAL_VALUES,
    ArtifactContract,
    BlockContract,
    EffectContract,
    OutcomeProvenance,
    OutcomeSymbol,
    _escape,
)
from ._semantics_parallel import parallel_destination
from ._semantics_validation import _ValidationState, fail
from .ir import CallIR


def call(
    state: _ValidationState, block: CallIR, pointer: str, *, parallel: bool
) -> tuple[BlockContract, Mapping[str, OutcomeSymbol]]:
    if not _ID.fullmatch(block.workflow):
        fail(state, "LSW304", "call workflow name is invalid", f"{pointer}/call/workflow", "use a logical workflow name")
    contract = state.catalog.contract_for(block.workflow)
    if contract is None:
        fail(state, "LSW304", f"no child workflow contract is available for {block.workflow!r}", f"{pointer}/call/workflow", "compile and validate the child workflow first")
    if set(contract.outcomes) != set(_TERMINAL_VALUES) or len(contract.outcomes) != len(_TERMINAL_VALUES):
        fail(state, "LSW304", "child workflow contracts must expose pass, fail, and error outcomes", f"{pointer}/call/workflow", "use a validated child terminal contract")
    if block.id is None and block.artifacts:
        fail(state, "LSW304", "a call with artifacts requires an explicit id", f"{pointer}/call/id", "add a unique call id")
    handlers(state, block.on_failure, block.on_error, f"{pointer}/call")
    if contract.non_artifact_writes:
        if parallel:
            fail(state, "LSP102", "parallel child calls may have no non-artifact writes", f"{pointer}/call", "use a child with only declared fixed artifact exports")
        fail(state, "LSW304", "call contracts may expose only their declared parent artifacts", f"{pointer}/call", "remove child non-artifact writes or declare a fixed exported artifact")
    effect = EffectContract()
    for handle, destination in block.artifacts.items():
        export = contract.exports.get(handle)
        if export is None:
            fail(state, "LSW304", f"child {block.workflow!r} does not export artifact {handle!r}", f"{pointer}/call/artifacts/{_escape(handle)}", "select a declared child export handle")
        if parallel:
            parallel_destination(state, destination, f"{pointer}/call/artifacts/{_escape(handle)}")
        validate_destination(
            state, destination, f"{pointer}/call/artifacts/{_escape(handle)}"
        )
        qualified = qualified_handle(state, block.id or "", handle, pointer, parallel)
        if qualified in state.artifacts:
            fail(state, "LSW304", f"duplicate qualified artifact handle {qualified!r}", f"{pointer}/call/artifacts/{_escape(handle)}", "use a unique call and artifact handle")
        artifact = ArtifactContract(qualified, export.fixed_source, destination)
        state.artifacts[qualified] = artifact
        effect = effect.union(EffectContract((destination,)))
    outcomes: Mapping[str, OutcomeSymbol] = {}
    if block.id is not None:
        outcomes = {block.id: OutcomeSymbol(block.id, tuple(contract.outcomes), OutcomeProvenance.CHILD)}
    return BlockContract(block, effect, retry(state, None, f"{pointer}/call/retry")), outcomes


def validate_destination(
    state: _ValidationState, destination: str, pointer: str
) -> None:
    existing = [artifact.destination for artifact in state.artifacts.values()]
    try:
        validate_portable_project_paths(
            ((path, "file") for path in (*existing, destination)),
            limits=ProjectTreeLimits(),
            label="artifact destinations",
        )
    except (PortablePathError, StorageLimitExceeded) as exc:
        fail(
            state,
            "LSW304",
            f"artifact destination overlaps or aliases another destination: {exc}",
            pointer,
            "use one unique safe project-relative destination",
        )


def qualified_handle(
    state: _ValidationState, call_id: str, handle: str, pointer: str, parallel: bool
) -> str:
    if not _ID.fullmatch(handle):
        fail(state, "LSW304", "artifact export handle is invalid", f"{pointer}/call/artifacts/{_escape(handle)}", "use a logical export handle")
    return call_id + "." + handle
