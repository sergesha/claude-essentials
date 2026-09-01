"""Single mutable context and root workflow semantic validation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ._semantics_catalog import ChildArtifactContract, WorkflowCatalog
from ._semantics_contracts import ArtifactContract, OutcomeSymbol, ValidatedWorkflow
from .diagnostics import Diagnostic, DiagnosticError
from .ir import WorkflowIR


@dataclass
class _ValidationState:
    workflow: WorkflowIR
    catalog: WorkflowCatalog
    outcomes: dict[str, OutcomeSymbol] = field(default_factory=dict)
    artifacts: dict[str, ArtifactContract] = field(default_factory=dict)
    exports: dict[str, ChildArtifactContract] = field(default_factory=dict)
    export_paths: set[str] = field(default_factory=set)
    export_producers: set[str] = field(default_factory=set)
    ids: set[str] = field(default_factory=set)


def fail(state: _ValidationState, code: str, message: str, pointer: str, hint: str) -> None:
    location = state.workflow.location_for(pointer)
    raise DiagnosticError((Diagnostic(
        code, message, state.workflow.source_path or Path("<workflow>"),
        line=location.line if location else None,
        column=location.column if location else None,
        pointer=pointer, hint=hint,
    ),))


def validate(
    state: _ValidationState,
    flow: Callable[..., object],
) -> ValidatedWorkflow:
    if state.workflow.version != "1":
        fail(state, "LSW120", "only workflow_version '1' is supported", "/workflow_version", "use workflow_version: '1'")
    if state.workflow.protect != ("**",):
        fail(state, "LSW301", "v1 workflows must protect the complete project", "/protect", 'use protect: ["**"]')
    flow_contract = flow(state, state.workflow.flow, "/flow", {}, parallel=False)
    return ValidatedWorkflow(
        state.workflow,
        flow_contract,
        state.outcomes,
        state.artifacts,
        state.exports,
        flow_contract.effects.writes,
    )
