"""Thin orchestration owner for workflow lowering."""

from __future__ import annotations

from ._lowering_contracts import _Fragment
from .ir import CallIR
from .semantics import BlockContract


class _LoweringCall:
    def call(self, contract: BlockContract, pointer: str) -> _Fragment:
        block = contract.block
        if not isinstance(block, CallIR):
            raise TypeError("call lowering requires CallIR")
        resolved = self._resolved_call_child(block)
        (
            call_digest,
            scope_key,
            child_outcome,
            saved_context,
            reserved_child_channels,
        ) = self._call_identity(block, resolved, pointer)
        namespace = f"call_{call_digest}"
        self._declare_call_context_channels(
            child_outcome=child_outcome,
            saved_context=saved_context,
        )
        child_contract = resolved.contract
        artifact_specs = self._bind_call_artifacts(block, child_contract)
        producer_bindings = self._artifact_producers(resolved, artifact_specs)
        self._declare_call_contract_state(child_contract, namespace)

        scope = self._call_scope_fragment(
            block,
            pointer,
            call_digest,
            scope_key,
        )
        context, pre = self._call_context_nodes(
            pointer=pointer,
            namespace=namespace,
            child_contract=child_contract,
            saved_context=saved_context,
        )
        generated_path, specialized_members = self._specialize_call_members(
            block=block,
            pointer=pointer,
            resolved=resolved,
            call_digest=call_digest,
            namespace=namespace,
            scope_key=scope_key,
            child_outcome=child_outcome,
            reserved_child_channels=reserved_child_channels,
            producer_bindings=producer_bindings,
        )
        self._record_call_workflow_dependency(
            block=block,
            pointer=pointer,
            resolved=resolved,
            generated_path=generated_path,
            specialized_members=specialized_members,
        )
        self._record_call_transitive_dependencies(
            resolved=resolved,
            specialized_members=specialized_members,
            namespace=namespace,
            pointer=pointer,
        )
        return self._finish_call_graph(
            pointer=pointer,
            generated_path=generated_path,
            namespace=namespace,
            child_contract=child_contract,
            artifact_specs=artifact_specs,
            producer_bindings=producer_bindings,
            saved_context=saved_context,
            child_outcome=child_outcome,
            context=context,
            scope=scope,
            pre=pre,
        )
