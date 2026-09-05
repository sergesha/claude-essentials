"""Read-only projection from native checkpoint facts to the public vocabulary."""

from __future__ import annotations

import hashlib
from copy import deepcopy
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
MAX_STATUS_PENDING = 128


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


def _child_annotations(
    binding: RunBinding, interrupt: NativeInterrupt
) -> tuple[tuple[str, Any], ...]:
    """Hash the parent public run and stable native subgraph task path only.

    Checkpoint and interrupt occurrence identifiers are deliberately excluded,
    so one direct child keeps a correlation across pause/resume checkpoints.
    """
    coordinate = interrupt.coordinate
    if not coordinate.checkpoint_ns:
        return ()
    digest = hashlib.sha256(b"lockstep.child-correlation/v1\0")
    for value in (
        binding.public_run_id,
        coordinate.checkpoint_ns,
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return (("child_run_id", f"child-{digest.hexdigest()}"),)


def _worker_brief(interrupt: NativeInterrupt) -> dict[str, Any]:
    """Project work instructions from the captured interrupt, not live sources."""
    value = interrupt.value
    if not isinstance(value, dict):
        return {}
    brief = {
        key: deepcopy(value[key])
        for key in (
            "task", "exit_criterion", "evidence_schema", "checks", "artifact_contract"
        )
        if key in value
    }
    descriptor = _descriptor(interrupt)
    if descriptor is not None and descriptor.get("kind") == "manual":
        brief["writes"] = deepcopy(descriptor.get("writes", []))
    return brief


def _parallel_projection(
    binding: RunBinding, snapshot: NativeSnapshot, effects: object
) -> ScenarioStatus | None:
    if len(snapshot.pending) <= 1:
        return None
    if len(snapshot.pending) > MAX_STATUS_PENDING:
        return ScenarioStatus(
            "running",
            binding.public_run_id,
            "engine",
            "scenario_wait",
            annotations=(("integrity_error", "pending_task_limit_exceeded"),),
        )
    phases: dict[str, int] = {}
    operations: list[str] = []
    deadlines: list[str | None] = []
    engine_owned = False
    worker_steps: list[str] = []
    briefs: list[dict[str, Any]] = []
    for interrupt in snapshot.pending:
        raw = _descriptor(interrupt)
        if raw is None:
            value = interrupt.value
            worker_steps.append(
                str(value.get("step") or "") if isinstance(value, dict) else ""
            )
            briefs.append({
                "step": worker_steps[-1],
                **_worker_brief(interrupt),
                **dict(_child_annotations(binding, interrupt)),
            })
            phases["worker"] = phases.get("worker", 0) + 1
            deadlines.append(None)
            continue
        try:
            descriptor = parse_effect_descriptor(raw)
            logical_id = str(getattr(descriptor, "logical_id", ""))
            effect_id = derive_effect_id(interrupt.coordinate, descriptor.digest)
            record = effects.get(effect_id)
            if (
                record.coordinate != interrupt.coordinate
                or record.descriptor_digest != descriptor.digest
            ):
                raise ValueError("effect record mismatch")
            phase = str(record.phase)
            deadline = record.deadline_at
        except (AttributeError, KeyError, TypeError, ValueError):
            logical_id = str(raw.get("logical_id") or "")
            phase = "unregistered"
            deadline = None
        operations.append(logical_id)
        phases[phase] = phases.get(phase, 0) + 1
        deadlines.append(None if deadline is None else deadline.isoformat())
        if raw.get("kind") != "manual" or phase != "prepared":
            engine_owned = True
        else:
            value = interrupt.value
            briefs.append({
                "step": value.get("step") or logical_id,
                **_worker_brief(interrupt),
                **dict(_child_annotations(binding, interrupt)),
            })
    progress = {
        "pending": len(snapshot.pending),
        "phases": {key: phases[key] for key in sorted(phases)},
        "operations": operations,
        "deadlines": deadlines,
    }
    if briefs:
        progress["steps"] = briefs
    if engine_owned:
        return ScenarioStatus(
            "running",
            binding.public_run_id,
            "engine",
            "scenario_wait",
            annotations=(("parallel_progress", progress),),
        )
    step = next((value for value in worker_steps if value), None)
    return ScenarioStatus(
        "awaiting",
        binding.public_run_id,
        "worker",
        "edit_then_scenario_done",
        step=step,
        annotations=(("parallel_progress", progress),),
    )


def _running_effect_status(
    binding: RunBinding,
    logical_id: object,
    annotations: tuple[tuple[str, Any], ...],
) -> ScenarioStatus:
    return ScenarioStatus(
        "running",
        binding.public_run_id,
        "engine",
        "scenario_wait",
        step=str(logical_id or "") or None,
        annotations=annotations,
    )


def _manual_projection(
    binding: RunBinding,
    interrupt: NativeInterrupt,
    descriptor: dict[str, Any],
    effects: object,
    child_annotations: tuple[tuple[str, Any], ...],
) -> ScenarioStatus:
    try:
        parsed = parse_effect_descriptor(descriptor)
        if not isinstance(parsed, EffectDescriptor):
            raise TypeError("manual descriptor is not an ordinary effect")
        effect_id = derive_effect_id(interrupt.coordinate, parsed.digest)
        record = effects.get(effect_id)
    except (AttributeError, KeyError, TypeError, ValueError):
        return _running_effect_status(
            binding,
            descriptor.get("logical_id"),
            (("manual_handoff", "preparing"), *child_annotations),
        )
    if (
        record.coordinate != interrupt.coordinate
        or record.descriptor_digest != parsed.digest
        or record.effect_kind != "manual"
        or record.phase != "prepared"
    ):
        return _running_effect_status(
            binding,
            parsed.logical_id,
            (("manual_handoff", "not_ready"), *child_annotations),
        )
    value = interrupt.value
    step = value.get("step") if isinstance(value, dict) else None
    return ScenarioStatus(
        "awaiting",
        binding.public_run_id,
        "worker",
        "edit_then_scenario_done",
        step=step or parsed.logical_id,
        annotations=(*child_annotations, *_worker_brief(interrupt).items()),
    )


def _pinned_projection(
    binding: RunBinding,
    snapshot: NativeSnapshot,
    interrupt: NativeInterrupt,
    descriptor: dict[str, Any],
    effects: object,
    child_annotations: tuple[tuple[str, Any], ...],
) -> ScenarioStatus:
    try:
        parsed = parse_effect_descriptor(descriptor)
        if not isinstance(parsed, EffectDescriptor):
            raise TypeError("pinned descriptor is not an ordinary effect")
        effect_id = derive_effect_id(interrupt.coordinate, parsed.digest)
        record = effects.get(effect_id)
        if (
            record.coordinate != interrupt.coordinate
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
        return _running_effect_status(
            binding, descriptor.get("logical_id"), child_annotations
        )
    gate_execution = {
        "operation_id": effect_id,
        "execution_class": "pinned-validator",
        "logical_argv": list(command.logical_argv),
        "logical_cwd": command.logical_cwd,
        "phase": str(record.phase),
    }
    return _running_effect_status(
        binding,
        parsed.logical_id,
        (("gate_execution", gate_execution), *child_annotations),
    )


def _single_pending_projection(
    binding: RunBinding,
    snapshot: NativeSnapshot,
    effects: object,
) -> ScenarioStatus:
    interrupt = snapshot.pending[0]
    child_annotations = _child_annotations(binding, interrupt)
    descriptor = _descriptor(interrupt)
    if descriptor is None:
        value = interrupt.value
        step = value.get("step") if isinstance(value, dict) else None
        return ScenarioStatus(
            "awaiting",
            binding.public_run_id,
            "worker",
            "edit_then_scenario_done",
            step=step,
            annotations=(*child_annotations, *_worker_brief(interrupt).items()),
        )
    kind = descriptor.get("kind")
    if kind == "manual":
        return _manual_projection(
            binding, interrupt, descriptor, effects, child_annotations
        )
    if kind == "pinned":
        return _pinned_projection(
            binding,
            snapshot,
            interrupt,
            descriptor,
            effects,
            child_annotations,
        )
    return _running_effect_status(
        binding, descriptor.get("logical_id"), child_annotations
    )


def _terminal_projection(
    binding: RunBinding,
    snapshot: NativeSnapshot,
    outcome: object,
) -> ScenarioStatus:
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
        parallel = _parallel_projection(binding, snapshot, effects)
        if parallel is not None:
            return parallel
        return _single_pending_projection(binding, snapshot, effects)
    return _terminal_projection(binding, snapshot, outcome)
