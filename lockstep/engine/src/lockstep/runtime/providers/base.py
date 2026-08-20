"""Closed, provider-neutral values at the external execution boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from lockstep.runtime.effects.models import EffectResult
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.payload_limits import bounded_json


def _hex(value: str, label: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("deadline must include a timezone")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class ScopeBinding:
    """Graph-owned scope authority committed into one runner request."""

    scope_digest: str
    runner_binding_digest: str | None


@dataclass(frozen=True)
class EffectRequest:
    effect_id: str
    public_run_id: str
    project_identity: str
    definition_digest: str
    coordinate: NativeCoordinate
    descriptor_digest: str
    effect_kind: str
    runner_selector: str
    runner_binding_digest: str
    required_capabilities: tuple[str, ...]
    inputs: tuple[tuple[str, object], ...]
    writes: tuple[str, ...]
    deadline_at: datetime | None
    scope_bindings: tuple[ScopeBinding, ...]
    request_digest: str

    @classmethod
    def build(
        cls,
        *,
        effect_id: str,
        public_run_id: str,
        project_identity: str,
        definition_digest: str,
        coordinate: NativeCoordinate,
        descriptor_digest: str,
        effect_kind: str,
        runner_selector: str,
        runner_binding_digest: str,
        required_capabilities: tuple[str, ...],
        inputs: tuple[tuple[str, object], ...],
        writes: tuple[str, ...],
        deadline_at: datetime | None,
        scope_bindings: tuple[ScopeBinding, ...] = (),
    ) -> EffectRequest:
        detached_inputs = tuple(
            (name, bounded_json(value, label=f"effect input {name}"))
            for name, value in inputs
        )
        deadline = _utc(deadline_at)
        checked_scope_bindings = tuple(
            ScopeBinding(
                scope_digest=_hex(binding.scope_digest, "scope_digest"),
                runner_binding_digest=(
                    None
                    if binding.runner_binding_digest is None
                    else _hex(
                        binding.runner_binding_digest, "scope runner_binding_digest"
                    )
                ),
            )
            for binding in scope_bindings
        )
        commitment = {
            "schema": "lockstep.effect-request/v1",
            "effect_id": _text(effect_id, "effect_id"),
            "public_run_id": _text(public_run_id, "public_run_id"),
            "project_identity": _text(project_identity, "project_identity"),
            "definition_digest": _hex(definition_digest, "definition_digest"),
            "coordinate": {
                "thread_id": coordinate.thread_id,
                "checkpoint_ns": coordinate.checkpoint_ns,
                "checkpoint_id": coordinate.checkpoint_id,
                "task_id": coordinate.task_id,
                "interrupt_id": coordinate.interrupt_id,
            },
            "descriptor_digest": _hex(descriptor_digest, "descriptor_digest"),
            "effect_kind": _text(effect_kind, "effect_kind"),
            "runner_selector": _text(runner_selector, "runner_selector"),
            "runner_binding_digest": _hex(
                runner_binding_digest, "runner_binding_digest"
            ),
            "required_capabilities": list(required_capabilities),
            "inputs": [[name, value] for name, value in detached_inputs],
            "writes": list(writes),
            "deadline_at": None if deadline is None else deadline.isoformat(),
            "scope_bindings": [
                {
                    "scope_digest": binding.scope_digest,
                    "runner_binding_digest": binding.runner_binding_digest,
                }
                for binding in checked_scope_bindings
            ],
        }
        encoded = json.dumps(
            commitment,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return cls(
            effect_id=effect_id,
            public_run_id=public_run_id,
            project_identity=project_identity,
            definition_digest=definition_digest,
            coordinate=coordinate,
            descriptor_digest=descriptor_digest,
            effect_kind=effect_kind,
            runner_selector=runner_selector,
            runner_binding_digest=runner_binding_digest,
            required_capabilities=required_capabilities,
            inputs=detached_inputs,
            writes=writes,
            deadline_at=deadline,
            scope_bindings=checked_scope_bindings,
            request_digest=hashlib.sha256(encoded).hexdigest(),
        )


@dataclass(frozen=True)
class PreparedLaunch:
    effect_id: str
    request_digest: str
    runner_binding_digest: str
    launch_ref: str
    workspace_ref: str | None


@dataclass(frozen=True)
class RunnerObservation:
    effect_id: str
    request_digest: str
    runner_binding_digest: str
    state: Literal["absent", "running", "terminal", "indeterminate"]
    result: EffectResult | object | None = None

    @classmethod
    def running_for(cls, launch: PreparedLaunch) -> RunnerObservation:
        return cls(
            launch.effect_id,
            launch.request_digest,
            launch.runner_binding_digest,
            "running",
        )


@dataclass(frozen=True)
class TerminalSafetyObservation:
    effect_id: str
    request_digest: str
    runner_binding_digest: str
    state: Literal["pending", "proven"]
    result_stable: bool = False
    rollover_snapshot_ref: str | None = None

    @classmethod
    def pending_for(cls, launch: PreparedLaunch) -> TerminalSafetyObservation:
        return cls(
            launch.effect_id,
            launch.request_digest,
            launch.runner_binding_digest,
            "pending",
        )

    @classmethod
    def proven_for(
        cls,
        launch: PreparedLaunch,
        *,
        result_stable: bool,
        rollover_snapshot_ref: str | None = None,
    ) -> TerminalSafetyObservation:
        return cls(
            launch.effect_id,
            launch.request_digest,
            launch.runner_binding_digest,
            "proven",
            result_stable=result_stable,
            rollover_snapshot_ref=rollover_snapshot_ref,
        )


class RunnerAdapter(Protocol):
    binding_digest: str

    def prepare(self, request: EffectRequest) -> PreparedLaunch: ...

    def ensure_started(self, launch: PreparedLaunch) -> RunnerObservation: ...

    def inspect(self, effect_id: str) -> RunnerObservation: ...

    def cancel(self, effect_id: str) -> RunnerObservation: ...

    def quiesce(self, effect_id: str) -> TerminalSafetyObservation: ...
