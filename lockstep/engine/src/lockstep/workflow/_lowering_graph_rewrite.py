"""Internal workflow-lowering responsibility owner."""

from __future__ import annotations

from typing import Any

from lockstep.runtime.effects.models import (
    AcceptDescriptor,
    DecisionDescriptor,
    EffectDescriptor,
    ScopeDescriptor,
)

from ._lowering_contracts import _FragmentNames, _GraphFragmentPlan
from ._lowering_graph_descriptor import (
    protected_fragment_descriptor,
    qualify_fragment_interrupt_channels,
    rewrite_fragment_descriptor,
)
from ._lowering_graph_nodes import (
    prepare_fragment_node,
    protected_fragment_resume_keys,
    store_fragment_node,
)


class _LoweringGraphRewrite:
    def _declare_fragment_state(
        self,
        plan: _GraphFragmentPlan,
        names: _FragmentNames,
    ) -> set[str]:
        fragment_state_keys: set[str] = set()
        for key, state_type in plan.state.items():
            qualified = names.state_key(key)
            if qualified in self.state:
                raise ValueError(f"fragment generated state collision: {qualified}")
            self.declare_generated_state(qualified, state_type)
            fragment_state_keys.add(qualified)
        return fragment_state_keys

    @staticmethod
    def _qualify_fragment_descriptor_state(
        descriptor: dict[str, Any],
        names: _FragmentNames,
    ) -> None:
        inputs = descriptor.get("inputs")
        if isinstance(inputs, dict):
            for selector in inputs.values():
                if isinstance(selector, dict) and isinstance(
                    selector.get("state_key"), str
                ):
                    selector["state_key"] = names.state_key(selector["state_key"])
        for field in ("scope_state_keys", "ancestor_deadline_state_keys"):
            if isinstance(descriptor.get(field), list):
                descriptor[field] = [names.state_key(key) for key in descriptor[field]]
        if isinstance(descriptor.get("result_state_key"), str):
            descriptor["result_state_key"] = names.state_key(
                descriptor["result_state_key"]
            )

    @staticmethod
    def _qualify_fragment_descriptor_artifacts(
        descriptor: dict[str, Any],
        names: _FragmentNames,
    ) -> None:
        artifacts = descriptor.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if isinstance(artifact, dict) and isinstance(artifact.get("name"), str):
                    artifact["name"] = names.identity("artifact", artifact["name"])
        if isinstance(descriptor.get("artifact_handle"), str):
            descriptor["artifact_handle"] = names.identity(
                "artifact", descriptor["artifact_handle"]
            )

    def _inherit_fragment_scopes(self, descriptor: dict[str, Any]) -> None:
        if not self.active_scope_state_keys:
            return
        if descriptor.get("kind") == "manual":
            raise ValueError(
                "unmanaged manual fragment effects cannot enter a bounded scope"
            )
        if descriptor.get("kind") == "scope":
            descriptor["ancestor_deadline_state_keys"] = [
                *self.active_scope_state_keys,
                *descriptor.get("ancestor_deadline_state_keys", []),
            ]
        elif isinstance(descriptor.get("scope_state_keys"), list):
            descriptor["scope_state_keys"] = [
                *self.active_scope_state_keys,
                *descriptor["scope_state_keys"],
            ]

    @staticmethod
    def _fragment_descriptor_outcomes(parsed: Any) -> tuple[str, ...]:
        if isinstance(parsed, EffectDescriptor):
            return ("pass", "fail", "error")
        if isinstance(parsed, (ScopeDescriptor, DecisionDescriptor)):
            return ("pass", "error")
        if isinstance(parsed, AcceptDescriptor):
            return ("pass",)
        raise TypeError("unknown protected fragment descriptor")

    def _rewrite_fragment_interrupt(
        self,
        copied: dict[str, Any],
        original: dict[str, Any],
        names: _FragmentNames,
        fragment_state_keys: set[str],
    ) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        message, descriptor = protected_fragment_descriptor(copied)
        qualify_fragment_interrupt_channels(self, copied, names, fragment_state_keys)
        parsed = rewrite_fragment_descriptor(self, message, descriptor, names)
        return (
            original["resume_key"],
            self._fragment_descriptor_outcomes(parsed),
            tuple(getattr(parsed, "writes", ())),
        )

    @staticmethod
    def _rewrite_fragment_output(
        copied: dict[str, Any],
        *,
        protected_resume_keys: set[Any],
        plan: _GraphFragmentPlan,
        names: _FragmentNames,
    ) -> None:
        output = copied.get("output")
        if not isinstance(output, dict):
            return
        overwritten_results = set(output) & protected_resume_keys
        if overwritten_results:
            raise ValueError(
                "fragment passthrough may not overwrite protected result "
                f"channels: {sorted(overwritten_results)}"
            )
        unknown_outputs = set(output) - set(plan.state)
        if unknown_outputs:
            raise ValueError(
                f"fragment output writes undeclared state: {sorted(unknown_outputs)}"
            )
        copied["output"] = {
            names.state_key(key): names.template(value) for key, value in output.items()
        }

    def _install_fragment_nodes(
        self,
        plan: _GraphFragmentPlan,
        names: _FragmentNames,
        pointer: str,
    ) -> tuple[set[str], dict[str, tuple[str, tuple[str, ...]]], list[str]]:
        fragment_state_keys = self._declare_fragment_state(plan, names)
        protected_resume_keys = protected_fragment_resume_keys(plan)
        interrupt_outcomes: dict[str, tuple[str, tuple[str, ...]]] = {}
        declared_writes: list[str] = []
        for name, node in plan.nodes.items():
            copied, outcome, writes = prepare_fragment_node(
                self,
                name=name,
                node=node,
                plan=plan,
                names=names,
                protected_resume_keys=protected_resume_keys,
                fragment_state_keys=fragment_state_keys,
            )
            if outcome is not None:
                interrupt_outcomes[name] = outcome
            for write in writes:
                if write not in declared_writes:
                    declared_writes.append(write)
            store_fragment_node(
                self, name=name, copied=copied, names=names, pointer=pointer
            )
        return fragment_state_keys, interrupt_outcomes, declared_writes
