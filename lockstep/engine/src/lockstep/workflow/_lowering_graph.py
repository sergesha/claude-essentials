"""Internal workflow-lowering responsibility owner."""

from __future__ import annotations

import hashlib

from ._lowering_contracts import (
    LoweredDependency,
    _Exit,
    _Fragment,
    _FragmentNames,
    _GraphFragmentPlan,
)
from .canonical import canonical_yaml


class _LoweringGraph:
    def _finish_graph_fragment(
        self,
        *,
        plan: _GraphFragmentPlan,
        names: _FragmentNames,
        pointer: str,
        fragment_state_keys: set[str],
    ) -> _Fragment:
        entry_gate = self.node(pointer, "graph", "entry", {"type": "passthrough"})
        pass_gate = self.node(pointer, "graph", "pass", {"type": "passthrough"})
        self.edge(entry_gate, names.node(plan.entry))
        self.edge(names.node(plan.exits["pass"]), pass_gate)
        if "fail" in plan.exits:
            self.edge(names.node(plan.exits["fail"]), self.outcome_target("FAIL"))
        if "error" in plan.exits:
            self.edge(names.node(plan.exits["error"]), self.outcome_target("ERROR"))
        expansion = canonical_yaml(
            {
                "state": {
                    key: self.state[key]
                    for key in self.state
                    if key in fragment_state_keys
                },
                "nodes": {
                    key: self.nodes[key]
                    for key in self.nodes
                    if key.startswith(plan.namespace + ".")
                },
                "edges": [
                    edge
                    for edge in self.edges
                    if str(edge.get("from", "")).startswith(plan.namespace + ".")
                    or str(edge.get("to", "")).startswith(plan.namespace + ".")
                ],
            }
        )
        self.dependencies.append(
            LoweredDependency(
                "fragment",
                plan.logical_name,
                pointer,
                plan.source_definition_sha256,
                hashlib.sha256(expansion).hexdigest(),
                None,
            )
        )
        return _Fragment(entry_gate, [_Exit(pass_gate)])
