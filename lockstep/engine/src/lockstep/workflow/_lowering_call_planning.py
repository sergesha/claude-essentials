"""Internal workflow-lowering responsibility owner."""

from __future__ import annotations

import hashlib
from typing import Any

from ._lowering_contracts import _Fragment
from .canonical import canonical_json
from .ir import CallIR


class _LoweringCallPlanning:
    def _resolved_call_child(self, block: CallIR) -> Any:
        if self.catalog is None:
            raise ValueError("call lowering requires a resolved catalog")
        resolver = getattr(self.catalog, "child_for", None)
        resolved = resolver(block.workflow) if callable(resolver) else None
        if resolved is None:
            raise ValueError(
                f"resolved compiled child is unavailable for {block.workflow!r}"
            )
        return resolved

    def _call_identity(
        self,
        block: CallIR,
        resolved: Any,
        pointer: str,
    ) -> tuple[str, str, str, dict[str, str], frozenset[str]]:
        call_digest = hashlib.sha256(
            b"lockstep.call-specialization/v1\0"
            + pointer.encode("utf-8")
            + b"\0"
            + self.workflow.source_sha256.encode("ascii")
            + b"\0"
            + block.workflow.encode("utf-8")
            + b"\0"
            + block.runner.encode("utf-8")
            + b"\0"
            + str(resolved.source_definition_sha256).encode("ascii")
            + b"\0"
            + str(resolved.standalone.bundle_sha256).encode("ascii")
            + b"\0"
            + canonical_json(
                {
                    "state_inputs": dict(resolved.contract.state_inputs),
                    "state_exports": dict(resolved.contract.state_exports),
                }
            )
        ).hexdigest()
        namespace = f"call_{call_digest}"
        scope_key = f"{namespace}_scope_result"
        child_outcome = f"{namespace}_outcome"
        saved_context = {
            "current_step": f"{namespace}_parent_current_step",
            "_loop_counts": f"{namespace}_parent_loop_counts",
            "_loop_limit_reached": f"{namespace}_parent_loop_limit_reached",
        }
        scope_request_key = f"call_{call_digest}_scope_request"
        reserved_child_channels = frozenset(
            {
                scope_request_key,
                scope_key,
                child_outcome,
                *saved_context.values(),
            }
        )
        return (
            call_digest,
            scope_key,
            child_outcome,
            saved_context,
            reserved_child_channels,
        )

    def _declare_call_context_channels(
        self,
        *,
        child_outcome: str,
        saved_context: dict[str, str],
    ) -> None:
        self.declare_generated_state(child_outcome, "str")
        self.declare_generated_state(
            "current_step", {"type": "str", "reducer": "last_value"}
        )
        self.declare_generated_state(
            "_loop_counts", {"type": "dict", "reducer": "last_value"}
        )
        self.declare_generated_state(
            "_loop_limit_reached", {"type": "bool", "reducer": "last_value"}
        )
        self.declare_generated_state(saved_context["current_step"], "any")
        self.declare_generated_state(saved_context["_loop_counts"], "dict")
        self.declare_generated_state(saved_context["_loop_limit_reached"], "any")

    def _bind_call_artifacts(
        self,
        block: CallIR,
        child_contract: Any,
    ) -> dict[str, tuple[str, str, str, str, str]]:
        artifact_specs: dict[str, tuple[str, str, str, str, str]] = {}
        for handle, destination in block.artifacts.items():
            export = child_contract.exports[handle]
            matches = [
                artifact
                for qualified, artifact in self.validated.artifacts.items()
                if qualified.endswith(f".{block.id}.{handle}")
                or qualified == f"{block.id}.{handle}"
                if artifact.source == export.fixed_source
                and artifact.destination == destination
            ]
            if len(matches) != 1:
                raise ValueError(
                    "child artifact export is not uniquely bound in parent semantics"
                )
            qualified = matches[0].handle
            declared_name = export.declared_name
            channel = (
                "artifact_"
                + hashlib.sha256(
                    ("lockstep.artifact-channel/v1\0" + qualified).encode("utf-8")
                ).hexdigest()
            )
            self.declare_generated_state(channel, "dict")
            self.artifact_state_keys[qualified] = (channel, declared_name)
            artifact_specs[qualified] = (
                declared_name,
                export.fixed_source,
                export.media_type,
                export.producer_logical_id,
                export.producer_result_state_key,
            )
        return artifact_specs

    def _declare_call_contract_state(
        self,
        child_contract: Any,
        namespace: str,
    ) -> None:
        for key, state_type in {
            **dict(child_contract.state_inputs),
            **dict(child_contract.state_exports),
        }.items():
            existing = self.state.get(key)
            if key in self.generated_state_names:
                raise ValueError(f"call state collides with generated channel: {key}")
            if existing is not None and existing != state_type:
                raise ValueError(f"call state type collision: {key}")
            self.state[key] = state_type
            self.declare_generated_state(f"{namespace}_{key}", state_type)

    def _call_scope_fragment(
        self,
        block: CallIR,
        pointer: str,
        call_digest: str,
        scope_key: str,
    ) -> _Fragment:
        descriptor = {
            "schema": "lockstep.effect/v1",
            "kind": "scope",
            "logical_id": f"call-{call_digest}-scope",
            "scope_kind": "call",
            "duration_seconds": (
                block.timeout_minutes * 60
                if block.timeout_minutes is not None
                else None
            ),
            "runner_selector": block.runner,
            "ancestor_deadline_state_keys": list(self.active_scope_state_keys),
            "result_state_key": scope_key,
            "result_schema": "lockstep.scope-result/v1",
        }
        return self.descriptor_interrupt(
            pointer,
            "call",
            f"call-{call_digest}-scope",
            descriptor,
            {"step": block.id or block.workflow, "lockstep_effect": descriptor},
            scope_key,
            None,
        )

    def _call_context_nodes(
        self,
        *,
        pointer: str,
        namespace: str,
        child_contract: Any,
        saved_context: dict[str, str],
    ) -> tuple[str, str]:
        pre_output = {
            f"{namespace}_{key}": f"{{state.{key}}}"
            for key in child_contract.state_inputs
        }
        context_output = {
            saved_context["current_step"]: "{state.current_step}",
            saved_context["_loop_counts"]: "{state._loop_counts}",
            saved_context["_loop_limit_reached"]: "{state._loop_limit_reached}",
            "current_step": None,
            "_loop_counts": {},
            "_loop_limit_reached": False,
        }
        context = self.node(
            pointer,
            "call",
            "context",
            {"type": "passthrough", "output": context_output},
        )
        pre = self.node(
            pointer,
            "call",
            "pre",
            {"type": "passthrough", "output": pre_output},
        )
        return context, pre
