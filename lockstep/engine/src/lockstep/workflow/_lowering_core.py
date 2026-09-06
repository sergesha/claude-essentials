"""Internal workflow-lowering responsibility owner."""

from __future__ import annotations

from typing import Any

from lockstep.runtime.effects.descriptors import parse_effect_descriptor

from ._lowering_contracts import _Exit, _Fragment
from ._lowering_identity import _stable_id


class _LoweringCore:
    def outcome_target(self, outcome: str) -> str:
        return self.outcome_targets[outcome]

    def declare_generated_state(self, name: str, state_type: str) -> None:
        """Register an internal channel without ever aliasing public state."""
        existing = self.state.get(name)
        if existing is not None and name not in self.generated_state_names:
            raise ValueError(f"generated state collision: {name}")
        if existing is not None and existing != state_type:
            raise ValueError(f"generated state type collision: {name}")
        self.state[name] = state_type
        self.generated_state_names.add(name)

    def node(self, pointer: str, kind: str, role: str, value: dict[str, Any]) -> str:
        name = _stable_id(pointer, kind, role)
        existing = self.nodes.get(name)
        if existing is not None:
            raise ValueError(f"stable generated node collision: {name}")
        self.nodes[name] = value
        mark = self.workflow.location_for(pointer)
        self.source_nodes[name] = {
            "pointer": pointer,
            "line": mark.line if mark else 1,
            "column": mark.column if mark else 1,
        }
        return name

    def edge(
        self, source: str | list[str], target: str | list[str], condition: str | None = None
    ) -> None:
        edge: dict[str, Any] = {"from": source, "to": target}
        if condition is not None:
            edge["condition"] = condition
        self.edges.append(edge)

    def connect(self, exits: list[_Exit], target: str) -> None:
        for item in exits:
            self.edge(item.source, target, item.condition)

    def descriptor_interrupt(
        self,
        pointer: str,
        kind: str,
        logical_id: str,
        descriptor: dict[str, Any],
        message: dict[str, Any],
        result_key: str,
        retry_limit: int | None,
        *,
        failure_target: str | None = None,
    ) -> _Fragment:
        parse_effect_descriptor(descriptor)
        request_key = f"{logical_id.replace('-', '_')}_request"
        self.declare_generated_state(request_key, "dict")
        self.declare_generated_state(result_key, "dict")
        interrupt = self.node(
            pointer,
            kind,
            "effect",
            {
                "type": "interrupt",
                "message": message,
                "state_key": request_key,
                "resume_key": result_key,
                "idempotent": False,
            },
        )
        entry = interrupt
        retry_gate = None
        if retry_limit is not None:
            retry_gate = self.node(
                pointer,
                kind,
                "attempt",
                {"type": "passthrough", "output": {"lockstep_continue": True}},
            )
            exhausted = self.node(pointer, kind, "exhausted", {"type": "passthrough"})
            self.edge(retry_gate, interrupt, "lockstep_continue == true")
            self.edge(exhausted, self.outcome_target("FAIL"))
            self.loop_limits[retry_gate] = retry_limit
            self.loop_exits[retry_gate] = exhausted
            entry = retry_gate
        if self.capture_aborted_effects:
            self.edge(
                interrupt,
                self.outcome_target("ABORTED"),
                f"{result_key}.fixed_error_code == 'cancelled'",
            )
            self.edge(
                interrupt,
                self.outcome_target("ERROR"),
                f"{result_key}.outcome == 'ERROR' and "
                f"{result_key}.fixed_error_code != 'cancelled'",
            )
        else:
            self.edge(
                interrupt,
                self.outcome_target("ABORTED"),
                f"{result_key}.fixed_error_code == 'cancelled'",
            )
            self.edge(
                interrupt,
                self.outcome_target("ERROR"),
                f"{result_key}.outcome == 'ERROR'",
            )
        fail_target = retry_gate or failure_target or self.outcome_target("FAIL")
        self.edge(interrupt, fail_target, f"{result_key}.outcome == 'FAIL'")
        return _Fragment(entry, [_Exit(interrupt, f"{result_key}.outcome == 'PASS'")])
