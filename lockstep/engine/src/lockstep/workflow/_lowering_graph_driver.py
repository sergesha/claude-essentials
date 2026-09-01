"""Thin orchestration owner for workflow lowering."""

from __future__ import annotations

from ._lowering_contracts import _Fragment, _FragmentNames
from .ir import GraphIR
from .semantics import BlockContract


class _LoweringGraphDriver:
    def graph(self, contract: BlockContract, pointer: str) -> _Fragment:
        block = contract.block
        if not isinstance(block, GraphIR):
            raise TypeError("graph lowering requires GraphIR")
        plan = self._graph_plan(block, pointer)
        fragment = plan.fragment
        state = plan.state
        namespace = plan.namespace
        names = _FragmentNames(namespace, state)

        (
            fragment_state_keys,
            interrupt_outcomes,
            declared_writes,
        ) = self._install_fragment_nodes(plan, names, pointer)
        self._validate_fragment_effects(fragment, declared_writes)
        adjacency, edges_by_source = self._install_fragment_edges(plan, names)
        self._validate_fragment_edge_routes(edges_by_source, interrupt_outcomes)
        loop_limits, loop_exits, analysis_adjacency = self._fragment_loop_analysis(
            plan, adjacency
        )
        self._validate_fragment_effect_outcomes(plan, interrupt_outcomes, loop_exits)
        self._validate_fragment_topology(
            plan=plan,
            names=names,
            adjacency=adjacency,
            analysis_adjacency=analysis_adjacency,
            loop_limits=loop_limits,
            loop_exits=loop_exits,
        )
        return self._finish_graph_fragment(
            plan=plan,
            names=names,
            pointer=pointer,
            fragment_state_keys=fragment_state_keys,
        )
