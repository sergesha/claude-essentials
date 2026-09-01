"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects._coordinator_values import (
    _Context,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.effects.models import (
    EffectDescriptor,
    ScopeDescriptor,
)
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot


class _EffectCoordinatorContext:
    def _context(
        self,
        binding: RunBinding,
        snapshot: NativeSnapshot,
        interrupt: NativeInterrupt,
        *,
        descriptor: EffectDescriptor | ScopeDescriptor,
        effect_id: str,
        record: EffectRecord | None,
        resolve_grant: bool,
    ) -> _Context:
        self._validate_runtime_input_boundary(descriptor)
        now = self._now()
        verified_ancestors = self._ancestor_results(
            binding.public_run_id, binding, descriptor, snapshot, interrupt
        )
        ancestors = tuple(result for result, _scope in verified_ancestors)
        candidates = self._deadline_candidates(descriptor, ancestors, now)
        deadline_at = (
            record.deadline_at
            if record is not None
            else (min(candidates) if candidates else None)
        )
        if isinstance(descriptor, ScopeDescriptor):
            return self._scope_context(
                interrupt=interrupt,
                descriptor=descriptor,
                effect_id=effect_id,
                record=record,
                ancestors=ancestors,
                deadline_at=deadline_at,
                now=now,
            )

        if descriptor.runner is None:
            # Manual work has no external runner. Task 7 owns its session/result path.
            return _Context(
                interrupt=interrupt,
                descriptor=descriptor,
                effect_id=effect_id,
                deadline_at=None,
                scope_result=None,
                request=None,
                runner=None,
                grant=None,
            )
        runner, scope_bindings = self._effect_runner_and_scopes(
            descriptor, record, verified_ancestors
        )
        intent = self._effect_intent(
            binding=binding,
            snapshot=snapshot,
            interrupt=interrupt,
            descriptor=descriptor,
            effect_id=effect_id,
            deadline_at=deadline_at,
            runner=runner,
            scope_bindings=scope_bindings,
        )
        if not resolve_grant or (deadline_at is not None and deadline_at <= now):
            return _Context(
                interrupt=interrupt,
                descriptor=descriptor,
                effect_id=effect_id,
                deadline_at=deadline_at,
                scope_result=None,
                request=None,
                runner=runner,
                grant=None,
            )
        return self._resolved_effect_context(
            interrupt=interrupt,
            descriptor=descriptor,
            effect_id=effect_id,
            deadline_at=deadline_at,
            runner=runner,
            intent=intent,
            record=record,
        )
