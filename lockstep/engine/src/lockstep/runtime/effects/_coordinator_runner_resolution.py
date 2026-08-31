"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from lockstep.runtime.effects._coordinator_values import (
    ProviderContractViolation,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.effects.models import (
    EffectDescriptor,
)
from lockstep.runtime.providers.base import (
    RunnerAdapter,
)


class _EffectCoordinatorRunnerResolution:
    def _runner_for(self, selector: str) -> RunnerAdapter:
        try:
            runner = self._runners[selector]
        except KeyError as exc:
            raise ProviderContractViolation(
                f"no trusted runner is bound for selector {selector!r}"
            ) from exc
        self._check_reconciliation_boundary(runner)
        return runner

    def _runner_for_binding(self, binding_digest: str) -> RunnerAdapter:
        try:
            runner = self._runner_bindings[binding_digest]
        except KeyError as exc:
            raise ProviderContractViolation(
                "durably bound runner is unavailable for recovery"
            ) from exc
        self._check_reconciliation_boundary(runner)
        return runner

    def _effect_runner_and_scopes(
        self,
        descriptor: EffectDescriptor,
        record: EffectRecord | None,
        verified_ancestors,
    ) -> tuple[RunnerAdapter, tuple[object, ...]]:
        runner = (
            self._runner_for(descriptor.runner.selector)
            if record is None
            else self._runner_for_binding(record.runner_binding_digest)
        )
        scope_bindings = []
        for ancestor, scope_binding in verified_ancestors:
            if ancestor.scope_kind == "call" and descriptor.kind == "managed" and (
                ancestor.runner_selector != descriptor.runner.selector
                or ancestor.runner_binding_digest != runner.binding_digest
            ):
                raise ProviderContractViolation(
                    "call scope runner binding does not match the selected adapter"
                )
            scope_bindings.append(scope_binding)
        return runner, tuple(scope_bindings)
