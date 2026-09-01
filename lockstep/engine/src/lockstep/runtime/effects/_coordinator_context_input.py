"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from datetime import datetime

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects._coordinator_values import (
    CoordinatorLineageError,
    ProviderContractViolation,
    _Context,
)
from lockstep.runtime.effects.descriptors import (
    build_scope_result,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.effects.models import (
    EffectDescriptor,
    RuntimeInputSelector,
    ScopeDescriptor,
    ScopeResult,
)
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot
from lockstep.runtime.providers.base import (
    EffectRequest,
    RunnerAdapter,
)


class _EffectCoordinatorContextInput:
    def _scope_context(
        self,
        *,
        interrupt: NativeInterrupt,
        descriptor: ScopeDescriptor,
        effect_id: str,
        record: EffectRecord | None,
        ancestors: tuple[ScopeResult, ...],
        deadline_at: datetime | None,
        now: datetime,
    ) -> _Context:
        runner = (
            self._runner_for(descriptor.runner_selector)
            if descriptor.runner_selector is not None
            else None
        )
        binding_digest = None if runner is None else runner.binding_digest
        scope_result = build_scope_result(
            effect_id=effect_id,
            scope_digest=descriptor.digest,
            scope_kind=descriptor.scope_kind,
            now=now,
            duration_seconds=(
                None if record is not None else descriptor.duration_seconds
            ),
            ancestors=ancestors,
            runner_selector=descriptor.runner_selector,
            runner_binding_digest=binding_digest,
        )
        if record is not None and scope_result.outcome == "PASS":
            if deadline_at is not None and deadline_at <= now:
                scope_result = ScopeResult(
                    schema="lockstep.scope-result/v1",
                    effect_id=effect_id,
                    outcome="ERROR",
                    scope_kind=descriptor.scope_kind,
                    scope_digest=descriptor.digest,
                    fixed_error_code="scope_timeout",
                )
            else:
                scope_result = ScopeResult(
                    scope_result.schema,
                    scope_result.effect_id,
                    scope_result.outcome,
                    scope_result.scope_kind,
                    scope_result.scope_digest,
                    deadline_at,
                    scope_result.runner_selector,
                    scope_result.runner_binding_digest,
                )
        return _Context(
            interrupt=interrupt,
            descriptor=descriptor,
            effect_id=effect_id,
            deadline_at=deadline_at,
            scope_result=scope_result,
            request=None,
            runner=runner,
            grant=None,
        )

    def _effect_intent(
        self,
        *,
        binding: RunBinding,
        snapshot: NativeSnapshot,
        interrupt: NativeInterrupt,
        descriptor: EffectDescriptor,
        effect_id: str,
        deadline_at: datetime | None,
        runner: RunnerAdapter,
        scope_bindings: tuple[object, ...],
    ) -> EffectRequest:
        runtime_inputs = (
            {}
            if self._snapshot_resolver is None
            else self._snapshot_resolver.inputs_for(
                binding, interrupt, descriptor, effect_id
            )
        )
        state_values = (
            snapshot.values
            if interrupt.state_values is None
            else interrupt.state_values
        )
        return EffectRequest.build(
            effect_id=effect_id,
            public_run_id=binding.public_run_id,
            project_identity=binding.project_identity,
            definition_digest=binding.recipe_digest,
            coordinate=interrupt.coordinate,
            descriptor_digest=descriptor.digest,
            effect_kind=descriptor.kind,
            runner_selector=descriptor.runner.selector,
            runner_binding_digest=runner.binding_digest,
            required_capabilities=descriptor.runner.required_capabilities,
            inputs=tuple(
                (
                    name,
                    runtime_inputs[name]
                    if isinstance(selector, RuntimeInputSelector)
                    else state_values[selector.state_key],
                )
                for name, selector in descriptor.inputs
            ),
            writes=descriptor.writes,
            artifacts=descriptor.artifacts,
            deadline_at=deadline_at,
            scope_bindings=scope_bindings,
        )

    def _resolved_effect_context(
        self,
        *,
        interrupt: NativeInterrupt,
        descriptor: EffectDescriptor,
        effect_id: str,
        deadline_at: datetime | None,
        runner: RunnerAdapter,
        intent: EffectRequest,
        record: EffectRecord | None,
    ) -> _Context:
        grant = self._authority.resolve(intent)
        if grant.required_authorities != runner.required_authorities:
            raise ProviderContractViolation(
                "effect grant authorities differ from trusted runner requirements"
            )
        request = intent.bind_grant(grant)
        if record is not None and (
            record.request_digest != request.request_digest
            or record.grant_digest != grant.digest
            or record.workspace_ref != grant.workspace_ref
        ):
            raise CoordinatorLineageError(
                "current request or grant differs from durable effect intent"
            )
        return _Context(
            interrupt=interrupt,
            descriptor=descriptor,
            effect_id=effect_id,
            deadline_at=deadline_at,
            scope_result=None,
            request=request,
            runner=runner,
            grant=grant,
        )
