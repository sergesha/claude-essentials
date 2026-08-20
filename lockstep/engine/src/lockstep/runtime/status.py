"""Read-only projection from native checkpoint facts to the public vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot

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
    if not isinstance(descriptor, dict) or descriptor.get("schema") != "lockstep.effect/v1":
        return None
    return descriptor


def project_status(
    binding: RunBinding,
    snapshot: NativeSnapshot,
    leases: object,
    effects: object,
) -> ScenarioStatus:
    del leases, effects  # Task 4 adds neutral annotations; neither may mutate state.
    outcome = snapshot.values.get("lockstep_outcome")
    if snapshot.task_errors:
        return ScenarioStatus("escalated", binding.public_run_id, "engine", None)
    if snapshot.pending:
        descriptor = _descriptor(snapshot.pending[0])
        if descriptor is None or descriptor.get("kind") == "manual":
            value = snapshot.pending[0].value
            step = value.get("step") if isinstance(value, dict) else None
            return ScenarioStatus(
                "awaiting",
                binding.public_run_id,
                "worker",
                "edit_then_scenario_done",
                step=step,
            )
        return ScenarioStatus(
            "running",
            binding.public_run_id,
            "engine",
            "scenario_wait",
            step=str(descriptor.get("logical_id") or "") or None,
        )
    if snapshot.next:
        return ScenarioStatus("running", binding.public_run_id, "engine", "scenario_wait")
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
        return ScenarioStatus("starting", binding.public_run_id, "engine", "scenario_wait")
    return ScenarioStatus("completed", binding.public_run_id, "engine", None)
