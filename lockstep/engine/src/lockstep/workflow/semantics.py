"""Semantic contracts for the structured Workflow DSL.

The parser deliberately accepts only structural data. This module is the
stable facade for the pure semantic phase and its immutable contracts.
"""

from __future__ import annotations

from . import _semantics_catalog as _catalog
from . import _semantics_contracts as _contracts
from ._semantics_blocks import flow
from ._semantics_validation import _ValidationState, validate
from .ir import WorkflowIR

BundleDependency = _catalog.BundleDependency
CanonicalCompiledBundle = _catalog.CanonicalCompiledBundle
CatalogFile = _catalog.CatalogFile
ChildArtifactContract = _catalog.ChildArtifactContract
ChildWorkflowContract = _catalog.ChildWorkflowContract
InMemoryWorkflowCatalog = _catalog.InMemoryWorkflowCatalog
ResolvedCatalog = _catalog.ResolvedCatalog
ResolvedChild = _catalog.ResolvedChild
ResolvedFragment = _catalog.ResolvedFragment
WorkflowCatalog = _catalog.WorkflowCatalog
YamlgraphStateType = _catalog.YamlgraphStateType
_canonical_relative_path = _catalog._canonical_relative_path
_exact_sha256 = _catalog._exact_sha256
_manifest_bundle_sha256 = _catalog._manifest_bundle_sha256

ArtifactContract = _contracts.ArtifactContract
BlockContract = _contracts.BlockContract
EffectContract = _contracts.EffectContract
FlowContract = _contracts.FlowContract
OutcomeProvenance = _contracts.OutcomeProvenance
OutcomeSymbol = _contracts.OutcomeSymbol
RepeatContract = _contracts.RepeatContract
RepeatControlContract = _contracts.RepeatControlContract
RepeatSimulation = _contracts.RepeatSimulation
RetryContract = _contracts.RetryContract
ValidatedWorkflow = _contracts.ValidatedWorkflow


class _Validator:
    """Compatibility wrapper around the single semantic validation context."""

    def __init__(self, workflow: WorkflowIR, catalog: WorkflowCatalog) -> None:
        self.state = _ValidationState(workflow, catalog)

    def validate(self) -> ValidatedWorkflow:
        return validate(self.state, flow)


def validate_semantics(
    workflow: WorkflowIR, catalog: WorkflowCatalog
) -> ValidatedWorkflow:
    """Validate trust, structured control flow, closed effects, and child contracts."""
    return _Validator(workflow, catalog).validate()
