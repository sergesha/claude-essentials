"""Internal workflow-lowering responsibility owner."""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from ._lowering_contracts import (
    LoweredDependency,
    LoweredGeneratedFile,
    _Exit,
    _Fragment,
)
from .semantics import FlowContract, RepeatContract


class _LoweringFlow:
    def repeat(self, contract: RepeatContract, pointer: str) -> _Fragment:
        gate = self.node(
            pointer,
            "repeat",
            "attempt",
            {"type": "passthrough", "output": {"lockstep_continue": True}},
        )
        exhausted = self.node(pointer, "repeat", "exhausted", {"type": "passthrough"})
        self.edge(exhausted, self.outcome_target("FAIL"))
        self.loop_limits[gate] = contract.limit
        self.loop_exits[gate] = exhausted
        blocks = list(contract.body.blocks)
        if not blocks:
            raise ValueError("repeat body must not be empty")
        fragments: list[_Fragment] = []
        for index, item in enumerate(blocks):
            item_pointer = f"{pointer}/repeat/do/{index}"
            failure = gate if index == len(blocks) - 1 else None
            if isinstance(item, RepeatContract):
                fragments.append(self.repeat(item, item_pointer))
            else:
                fragments.append(self.block(item, item_pointer, failure_target=failure))
        for left, right in pairwise(fragments):
            self.connect(left.exits, right.entry)
        self.edge(gate, fragments[0].entry, "lockstep_continue == true")
        return _Fragment(gate, fragments[-1].exits)

    def flow_contract(self, flow: FlowContract, pointer: str = "/flow") -> _Fragment:
        if not flow.blocks:
            empty = self.node(pointer, "flow", "empty", {"type": "passthrough"})
            return _Fragment(empty, [_Exit(empty)])
        fragments: list[_Fragment] = []
        for index, item in enumerate(flow.blocks):
            item_pointer = f"{pointer}/{index}"
            fragments.append(
                self.repeat(item, item_pointer)
                if isinstance(item, RepeatContract)
                else self.block(item, item_pointer)
            )
        for left, right in pairwise(fragments):
            self.connect(left.exits, right.entry)
        return _Fragment(fragments[0].entry, fragments[-1].exits)

    def build(
        self,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        tuple[LoweredGeneratedFile, ...],
        tuple[LoweredDependency, ...],
    ]:
        flow = self.flow_contract(self.validated.flow)
        self.edge("START", flow.entry)
        self.connect(flow.exits, self.terminals["PASS"])
        for terminal in self.terminals.values():
            self.edge(terminal, "END")
        source = f"../workflows/{self.workflow.name}.workflow.yaml"
        document: dict[str, Any] = {
            "version": "1.0",
            "name": self.workflow.name,
            "description": self.workflow.description,
            "x-lockstep-generated": {
                "schema": "lockstep.generated/v1",
                "compiler_version": "1",
                "workflow_version": self.workflow.version,
                "source": source,
                "source_sha256": self.workflow.source_sha256,
            },
            "state": self.state,
            "nodes": self.nodes,
            "edges": self.edges,
        }
        if self.loop_limits:
            document["loop_limits"] = self.loop_limits
            document["loop_exits"] = self.loop_exits
        source_map = {
            "schema": "lockstep.source-map/v1",
            "compiler_version": "1",
            "source": source,
            "nodes": self.source_nodes,
        }
        return (
            document,
            source_map,
            tuple(self.generated_files),
            tuple(self.dependencies),
        )
