"""Read-only projection from native checkpoint facts to the public vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_descriptor,
)
from lockstep.runtime.effects.models import EffectDescriptor
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot
from lockstep.runtime.providers.codex import CodexProviderError
from lockstep.runtime.providers.pinned import PinnedCommandSpec

PUBLIC_STATUSES = frozenset(
    {"starting", "awaiting", "running", "completed", "escalated", "aborted"}
)


@dataclass(frozen=True)
class ScenarioStatus:
    status: str
    run_id: str
    owner: str
    next_action: str | None
    step: str | None = None
    annotations: tuple[tuple[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "run_id": self.run_id,
            "owner": self.owner,
            "next_action": self.next_action,
        }
        if self.step is not None:
            result["step"] = self.step
        result.update(self.annotations)
        return result


def _descriptor(interrupt: NativeInterrupt) -> dict[str, Any] | None:
    value = interrupt.value
    if not isinstance(value, dict):
        return None
    descriptor = value.get("lockstep_effect")
    if (
        not isinstance(descriptor, dict)
        or descriptor.get("schema") != "lockstep.effect/v1"
    ):
        return None
    return descriptor


def project_status(
    binding: RunBinding,
    snapshot: NativeSnapshot,
    leases: object,
    effects: object,
) -> ScenarioStatus:
    del leases
    outcome = snapshot.values.get("lockstep_outcome")
    if snapshot.task_errors:
        return ScenarioStatus("escalated", binding.public_run_id, "engine", None)
    if snapshot.pending:
        descriptor = _descriptor(snapshot.pending[0])
        if descriptor is None:
            value = snapshot.pending[0].value
            step = value.get("step") if isinstance(value, dict) else None
            return ScenarioStatus(
                "awaiting",
                binding.public_run_id,
                "worker",
                "edit_then_scenario_done",
                step=step,
            )
        if descriptor.get("kind") == "manual":
            try:
                parsed = parse_effect_descriptor(descriptor)
                if not isinstance(parsed, EffectDescriptor):
                    raise TypeError("manual descriptor is not an ordinary effect")
                effect_id = derive_effect_id(
                    snapshot.pending[0].coordinate, parsed.digest
                )
                record = effects.get(effect_id)
            except (AttributeError, KeyError, TypeError, ValueError):
                return ScenarioStatus(
                    "running",
                    binding.public_run_id,
                    "engine",
                    "scenario_wait",
                    step=str(descriptor.get("logical_id") or "") or None,
                    annotations=(("manual_handoff", "preparing"),),
                )
            if (
                record.coordinate != snapshot.pending[0].coordinate
                or record.descriptor_digest != parsed.digest
                or record.effect_kind != "manual"
                or record.phase != "prepared"
            ):
                return ScenarioStatus(
                    "running",
                    binding.public_run_id,
                    "engine",
                    "scenario_wait",
                    step=parsed.logical_id,
                    annotations=(("manual_handoff", "not_ready"),),
                )
            value = snapshot.pending[0].value
            step = value.get("step") if isinstance(value, dict) else None
            return ScenarioStatus(
                "awaiting",
                binding.public_run_id,
                "worker",
                "edit_then_scenario_done",
                step=step or parsed.logical_id,
            )
        if descriptor.get("kind") == "pinned":
            try:
                parsed = parse_effect_descriptor(descriptor)
                if not isinstance(parsed, EffectDescriptor):
                    raise TypeError("pinned descriptor is not an ordinary effect")
                effect_id = derive_effect_id(
                    snapshot.pending[0].coordinate, parsed.digest
                )
                record = effects.get(effect_id)
                if (
                    record.coordinate != snapshot.pending[0].coordinate
                    or record.descriptor_digest != parsed.digest
                    or record.effect_kind != "pinned"
                ):
                    raise ValueError("pinned effect record mismatch")
                selectors = dict(parsed.inputs)
                command = PinnedCommandSpec.parse(
                    snapshot.values[selectors["command"].state_key]
                )
            except (
                AttributeError,
                KeyError,
                TypeError,
                ValueError,
                CodexProviderError,
            ):
                return ScenarioStatus(
                    "running",
                    binding.public_run_id,
                    "engine",
                    "scenario_wait",
                    step=str(descriptor.get("logical_id") or "") or None,
                )
            gate_execution = {
                "operation_id": effect_id,
                "execution_class": "pinned-validator",
                "logical_argv": list(command.logical_argv),
                "logical_cwd": command.logical_cwd,
                "phase": record.phase,
            }
            return ScenarioStatus(
                "running",
                binding.public_run_id,
                "engine",
                "scenario_wait",
                step=parsed.logical_id,
                annotations=(("gate_execution", gate_execution),),
            )
        return ScenarioStatus(
            "running",
            binding.public_run_id,
            "engine",
            "scenario_wait",
            step=str(descriptor.get("logical_id") or "") or None,
        )
    if snapshot.next:
        return ScenarioStatus(
            "running", binding.public_run_id, "engine", "scenario_wait"
        )
    if outcome == "ABORTED":
        return ScenarioStatus("aborted", binding.public_run_id, "engine", None)
    if outcome in {"FAIL", "ERROR"}:
        return ScenarioStatus("escalated", binding.public_run_id, "engine", None)
    if outcome == "PASS":
        return ScenarioStatus("completed", binding.public_run_id, "engine", None)
    if outcome is not None:
        return ScenarioStatus(
            "escalated",
            binding.public_run_id,
            "engine",
            None,
            annotations=(("integrity_error", "unknown_terminal_outcome"),),
        )
    if not snapshot.values and not snapshot.checkpoint_id:
        return ScenarioStatus(
            "starting", binding.public_run_id, "engine", "scenario_wait"
        )
    return ScenarioStatus("completed", binding.public_run_id, "engine", None)
