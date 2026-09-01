"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from lockstep.runtime.effects._coordinator_values import (
    ProviderContractViolation,
    _Context,
)
from lockstep.runtime.effects.descriptors import (
    parse_effect_result,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.effects.models import (
    EffectDescriptor,
    EffectResult,
    RuntimeInputSelector,
    ScopeDescriptor,
    ScopeResult,
)
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot
from lockstep.runtime.providers.base import (
    EffectRequest,
    PreparedLaunch,
    RunnerAdapter,
    RunnerObservation,
    TerminalSafetyObservation,
)


class _EffectCoordinatorValidation:
    @staticmethod
    def _raw_descriptor(interrupt: NativeInterrupt) -> object | None:
        if not isinstance(interrupt.value, dict):
            return None
        return interrupt.value.get("lockstep_effect")

    def _protected(self, snapshot: NativeSnapshot) -> list[NativeInterrupt]:
        return [
            interrupt
            for interrupt in snapshot.pending
            if isinstance(self._raw_descriptor(interrupt), dict)
            and self._raw_descriptor(interrupt).get("schema") == "lockstep.effect/v1"
        ]

    @staticmethod
    def _deadline_candidates(
        descriptor: EffectDescriptor | ScopeDescriptor,
        ancestors: Sequence[ScopeResult],
        now: datetime,
    ) -> list[datetime]:
        candidates = [
            item.absolute_deadline
            for item in ancestors
            if item.absolute_deadline is not None
        ]
        if any(item.outcome == "ERROR" for item in ancestors):
            candidates.append(now)
        duration = (
            descriptor.deadline_seconds
            if isinstance(descriptor, EffectDescriptor)
            else descriptor.duration_seconds
        )
        if duration is not None:
            candidates.append(now + timedelta(seconds=duration))
        return candidates

    @staticmethod
    def _check_reconciliation_boundary(runner: RunnerAdapter) -> None:
        if runner.reconciliation_boundary != "local_durable_handle":
            raise ProviderContractViolation(
                "runner reconciliation must use only a local durable handle"
            )

    @staticmethod
    def _check_launch(request: EffectRequest, launch: PreparedLaunch) -> None:
        if (
            launch.effect_id != request.effect_id
            or launch.request_digest != request.request_digest
            or launch.runner_binding_digest != request.runner_binding_digest
        ):
            raise ProviderContractViolation(
                "prepared launch does not match the immutable effect request"
            )
        for label, value, optional in (
            ("launch_ref", launch.launch_ref, False),
            ("workspace_ref", launch.workspace_ref, True),
        ):
            if value is None and optional:
                continue
            if (
                not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > 4096
            ):
                raise ProviderContractViolation(
                    f"provider {label} must be a bounded non-empty string"
                )
        if launch.workspace_ref != request.workspace_ref:
            raise ProviderContractViolation(
                "prepared launch workspace differs from the exact effect grant"
            )

    @staticmethod
    def _closed_result(value: object) -> EffectResult:
        if not isinstance(value, EffectResult):
            raise ProviderContractViolation(
                "provider result must be a closed bounded EffectResult"
            )
        try:
            parsed = parse_effect_result(value.to_dict())
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ProviderContractViolation(
                "provider result must be a closed bounded EffectResult"
            ) from exc
        if parsed != value:
            raise ProviderContractViolation(
                "provider result must be a canonical closed bounded EffectResult"
            )
        return parsed

    @staticmethod
    def _check_observation(
        binding: EffectRequest | EffectRecord,
        observation: RunnerObservation | TerminalSafetyObservation,
    ) -> None:
        if (
            observation.effect_id != binding.effect_id
            or observation.request_digest != binding.request_digest
            or observation.runner_binding_digest != binding.runner_binding_digest
        ):
            raise ProviderContractViolation(
                "runner observation does not match the immutable effect request"
            )
        if isinstance(observation, RunnerObservation):
            if observation.state == "terminal":
                EffectCoordinator._closed_result(observation.result)  # noqa: F821
            elif observation.result is not None:
                raise ProviderContractViolation(
                    "nonterminal runner observation cannot carry a result"
                )
        elif observation.state == "pending" and (
            observation.result_stable
            or observation.rollover_snapshot_ref is not None
            or observation.workspace_quarantined
        ):
            raise ProviderContractViolation(
                "pending terminal-safety observation cannot carry proof fields"
            )

    def _validate_runtime_input_boundary(
        self,
        descriptor: EffectDescriptor | ScopeDescriptor,
    ) -> None:
        if isinstance(descriptor, EffectDescriptor) and any(
            isinstance(selector, RuntimeInputSelector)
            for _name, selector in descriptor.inputs
        ) and self._snapshot_resolver is None:
            raise ProviderContractViolation(
                "runtime snapshot selectors require the dedicated durable "
                "snapshot resolver"
            )

    @staticmethod
    def _timeout_result(effect_id: str) -> EffectResult:
        return parse_effect_result(
            {
                "schema": "lockstep.effect-result/v1",
                "effect_id": effect_id,
                "outcome": "ERROR",
                "result_ref": None,
                "artifact_refs": [],
                "snapshot_ref": None,
                "diff_ref": None,
                "fixed_error_code": "deadline_timeout",
                "evidence_refs": [],
            }
        )

    def _terminal_safety(
        self,
        context: _Context,
        safety: TerminalSafetyObservation,
        *,
        result: EffectResult | None,
        binding: EffectRequest | EffectRecord,
    ) -> bool:
        assert isinstance(context.descriptor, EffectDescriptor)
        assert context.descriptor.runner is not None
        self._check_observation(binding, safety)
        if safety.state == "pending":
            return False
        if safety.state != "proven":
            raise ProviderContractViolation("terminal-safety proof is incomplete")
        if (
            "result_stability" in context.descriptor.runner.required_capabilities
            and not safety.result_stable
        ):
            raise ProviderContractViolation("required result-stability proof is absent")
        if context.descriptor.kind == "managed":
            if result is not None and result.snapshot_ref is None:
                if result.outcome != "ERROR":
                    raise ProviderContractViolation(
                        "managed PASS/FAIL requires an exact rollover snapshot"
                    )
                if (
                    safety.rollover_snapshot_ref is None
                    and not safety.workspace_quarantined
                ):
                    raise ProviderContractViolation(
                        "managed error requires rollover or quarantine proof"
                    )
            elif safety.rollover_snapshot_ref is None:
                raise ProviderContractViolation(
                    "managed completion requires independent snapshot rollover"
                )
            if (
                result is not None
                and result.snapshot_ref is not None
                and safety.rollover_snapshot_ref != result.snapshot_ref
            ):
                raise ProviderContractViolation(
                    "managed rollover does not match the sealed result snapshot"
                )
        return True

    @staticmethod
    def _interrupt_values(
        snapshot: NativeSnapshot, interrupt: NativeInterrupt
    ) -> Mapping[str, object]:
        return snapshot.values if interrupt.state_values is None else interrupt.state_values

    @staticmethod
    def _publication_result(effect_id: str, journal_digest: str) -> EffectResult:
        return parse_effect_result(
            {
                "schema": "lockstep.effect-result/v1",
                "effect_id": effect_id,
                "outcome": "PASS",
                "result_ref": f"publication:{journal_digest}",
                "artifact_refs": [],
                "snapshot_ref": None,
                "diff_ref": None,
                "fixed_error_code": None,
                "evidence_refs": [],
            }
        )

    @staticmethod
    def _publication_error_result(
        effect_id: str, journal_digest: str
    ) -> EffectResult:
        return parse_effect_result(
            {
                "schema": "lockstep.effect-result/v1",
                "effect_id": effect_id,
                "outcome": "ERROR",
                "result_ref": f"publication:{journal_digest}",
                "artifact_refs": [],
                "snapshot_ref": None,
                "diff_ref": None,
                "fixed_error_code": "provider_error",
                "evidence_refs": [],
            }
        )
