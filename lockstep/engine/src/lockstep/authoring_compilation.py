"""Shared captured-byte compilation semantics for authored workflows."""

from __future__ import annotations

import re
from typing import Mapping

from lockstep.errors import AuthoringError
from lockstep.workflow.compiler import CompilationResult, compile_workflow_document
from lockstep.workflow.schema import MarkedDocument
from lockstep.workflow.semantics import (
    ChildArtifactContract,
    ChildWorkflowContract,
    ResolvedCatalog,
    ResolvedChild,
    ValidatedWorkflow,
)

_WORKFLOW_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def validate_logical_name(name: str) -> str:
    """Validate a Workflow DSL logical name before it can reach a path."""

    if not isinstance(name, str) or not _WORKFLOW_NAME_RE.fullmatch(name):
        raise AuthoringError(
            f"invalid workflow name {name!r}; use lowercase letters, digits, and "
            "hyphens, beginning with a letter"
        )
    return name


def workflow_call_names(document: MarkedDocument) -> tuple[str, ...]:
    """Return unique child calls from one already-parsed workflow document."""

    calls: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            raw = value.get("call")
            if isinstance(raw, dict) and isinstance(raw.get("workflow"), str):
                calls.append(validate_logical_name(raw["workflow"]))
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(document.data)
    return tuple(dict.fromkeys(calls))


def _child_contract(validated: ValidatedWorkflow) -> ChildWorkflowContract:
    exports = {}
    for handle, artifact in validated.artifacts.items():
        logical = handle.rsplit(".", 1)[-1]
        exports[logical] = ChildArtifactContract(
            logical,
            artifact.source,
            logical,
            "application/octet-stream",
            handle.split(".", 1)[0],
            handle.replace(".", "_") + "_result",
        )
    return ChildWorkflowContract(
        ("pass", "fail", "error"),
        exports=exports,
        non_artifact_writes=validated.flow.effects.writes,
    )


def compile_captured_source(
    document: MarkedDocument,
    *,
    children: Mapping[str, tuple[ValidatedWorkflow, CompilationResult]] | None = None,
) -> tuple[ValidatedWorkflow, ResolvedCatalog, CompilationResult]:
    """Compile one already-captured source with canonical child semantics."""

    resolved_children = {
        name: ResolvedChild(
            name,
            _child_contract(validated),
            validated.workflow.source_sha256,
            compiled.as_catalog_bundle(),
        )
        for name, (validated, compiled) in (children or {}).items()
    }
    catalog = ResolvedCatalog(children=resolved_children)
    validated, compiled = compile_workflow_document(document, catalog)
    return validated, catalog, compiled
