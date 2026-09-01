"""Pure lowering from validated Workflow DSL contracts to yamlgraph data."""

from __future__ import annotations

from typing import Any

from ._lowering_artifacts import _LoweringArtifacts
from ._lowering_block_dispatch import _LoweringBlockDispatch
from ._lowering_blocks import _LoweringBlocks
from ._lowering_call import _LoweringCall
from ._lowering_call_bundle import _LoweringCallBundle
from ._lowering_call_planning import _LoweringCallPlanning
from ._lowering_contracts import LoweredDependency, LoweredGeneratedFile
from ._lowering_core import _LoweringCore
from ._lowering_descriptors import lower_accept_descriptor, lower_publish_descriptor
from ._lowering_flow import _LoweringFlow
from ._lowering_graph import _LoweringGraph
from ._lowering_graph_driver import _LoweringGraphDriver
from ._lowering_graph_plan import _LoweringGraphPlan
from ._lowering_graph_rewrite import _LoweringGraphRewrite
from ._lowering_graph_validation import _LoweringGraphValidation
from ._lowering_parallel import _LoweringParallel
from .semantics import ValidatedWorkflow, WorkflowCatalog

__all__ = (
    "LoweredDependency",
    "LoweredGeneratedFile",
    "lower_accept_descriptor",
    "lower_publish_descriptor",
    "lower_workflow",
)


class _Builder(
    _LoweringCore,
    _LoweringBlockDispatch,
    _LoweringBlocks,
    _LoweringParallel,
    _LoweringCallPlanning,
    _LoweringCall,
    _LoweringCallBundle,
    _LoweringArtifacts,
    _LoweringGraphPlan,
    _LoweringGraphRewrite,
    _LoweringGraphValidation,
    _LoweringGraphDriver,
    _LoweringGraph,
    _LoweringFlow,
):
    def __init__(
        self, validated: ValidatedWorkflow, catalog: WorkflowCatalog | None = None
    ) -> None:
        self.validated = validated
        self.workflow = validated.workflow
        self.catalog = catalog
        self.generated_files: list[LoweredGeneratedFile] = []
        self.dependencies: list[LoweredDependency] = []
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.state: dict[str, str] = {"lockstep_outcome": "str"}
        self.state["lockstep_continue"] = "bool"
        self.generated_state_names = {"lockstep_outcome", "lockstep_continue"}
        self.loop_limits: dict[str, int] = {}
        self.loop_exits: dict[str, str] = {}
        self.source_nodes: dict[str, dict[str, int | str]] = {}
        self.outcome_keys: dict[str, str] = {}
        self.artifact_state_keys: dict[str, tuple[str, str]] = {}
        self.terminals = {
            outcome: self.node(
                "/terminal",
                "terminal",
                outcome.lower(),
                {"type": "passthrough", "output": {"lockstep_outcome": outcome}},
            )
            for outcome in ("PASS", "FAIL", "ERROR", "ABORTED")
        }
        self.active_scope_state_keys: tuple[str, ...] = ()
        self.outcome_targets: dict[str, str] = dict(self.terminals)
        self.capture_aborted_effects = False
        self.inside_parallel_branch = False


def lower_workflow(
    validated: ValidatedWorkflow, catalog: WorkflowCatalog | None = None
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    tuple[LoweredGeneratedFile, ...],
    tuple[LoweredDependency, ...],
]:
    if not isinstance(validated, ValidatedWorkflow):
        raise TypeError("compile input must be a ValidatedWorkflow")
    return _Builder(validated, catalog).build()
