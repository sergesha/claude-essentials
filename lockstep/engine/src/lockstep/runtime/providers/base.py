"""Closed, provider-neutral values at the external execution boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from lockstep.runtime.effects.models import ArtifactDescriptor, EffectResult
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.payload_limits import bounded_json

if TYPE_CHECKING:
    from lockstep.runtime.effects.authority import EffectGrant


class DefinitiveProviderFailure(RuntimeError):
    """A bounded provider-neutral rejection that is safe to seal without retry."""

    def __init__(self, result: EffectResult) -> None:
        self.result = result
        super().__init__("provider definitively rejected the prepared effect")


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

    state_key: str
    producer_effect_id: str
    producer_coordinate: NativeCoordinate
    scope_digest: str
    scope_result_digest: str
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
    artifacts: tuple[ArtifactDescriptor, ...]
    deadline_at: datetime | None
    scope_bindings: tuple[ScopeBinding, ...]
    intent_digest: str
    grant_digest: str | None
    workspace_ref: str | None
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
        artifacts: tuple[ArtifactDescriptor, ...] = (),
    ) -> EffectRequest:
        detached_inputs = tuple(
            (
                _text(name, "effect input name"),
                bounded_json(value, label=f"effect input {name}"),
            )
            for name, value in inputs
        )
        deadline = _utc(deadline_at)
        checked_scope_bindings = tuple(
            ScopeBinding(
                state_key=_text(binding.state_key, "scope state_key"),
                producer_effect_id=_text(
                    binding.producer_effect_id, "scope producer_effect_id"
                ),
                producer_coordinate=binding.producer_coordinate,
                scope_digest=_hex(binding.scope_digest, "scope_digest"),
                scope_result_digest=_hex(
                    binding.scope_result_digest, "scope_result_digest"
                ),
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
            "artifacts": [
                {
                    "name": artifact.name,
                    "source_path": artifact.source_path,
                    "media_type": artifact.media_type,
                    "required": artifact.required,
                }
                for artifact in artifacts
            ],
            "deadline_at": None if deadline is None else deadline.isoformat(),
            "scope_bindings": [
                {
                    "state_key": binding.state_key,
                    "producer_effect_id": binding.producer_effect_id,
                    "producer_coordinate": {
                        "thread_id": binding.producer_coordinate.thread_id,
                        "checkpoint_ns": binding.producer_coordinate.checkpoint_ns,
                        "checkpoint_id": binding.producer_coordinate.checkpoint_id,
                        "task_id": binding.producer_coordinate.task_id,
                        "interrupt_id": binding.producer_coordinate.interrupt_id,
                    },
                    "scope_digest": binding.scope_digest,
                    "scope_result_digest": binding.scope_result_digest,
                    "runner_binding_digest": binding.runner_binding_digest,
                }
                for binding in checked_scope_bindings
            ],
        }
        admitted_commitment = bounded_json(commitment, label="aggregate effect request")
        encoded = json.dumps(
            admitted_commitment,
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
            artifacts=tuple(artifacts),
            deadline_at=deadline,
            scope_bindings=checked_scope_bindings,
            intent_digest=hashlib.sha256(encoded).hexdigest(),
            grant_digest=None,
            workspace_ref=None,
            request_digest=hashlib.sha256(encoded).hexdigest(),
        )

    def bind_grant(self, grant: EffectGrant) -> EffectRequest:
        """Materialize this draft as the immutable request for one exact grant."""

        if self.grant_digest is not None:
            raise ValueError("effect request is already bound to a grant")
        grant.validate_for(self)
        commitment = {
            "schema": "lockstep.effect-request/v1",
            "intent_digest": self.intent_digest,
            "grant_digest": grant.digest,
            "workspace_ref": grant.workspace_ref,
        }
        admitted = bounded_json(commitment, label="granted effect request")
        encoded = json.dumps(
            admitted,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return EffectRequest(
            effect_id=self.effect_id,
            public_run_id=self.public_run_id,
            project_identity=self.project_identity,
            definition_digest=self.definition_digest,
            coordinate=self.coordinate,
            descriptor_digest=self.descriptor_digest,
            effect_kind=self.effect_kind,
            runner_selector=self.runner_selector,
            runner_binding_digest=self.runner_binding_digest,
            required_capabilities=self.required_capabilities,
            inputs=self.inputs,
            writes=self.writes,
            artifacts=self.artifacts,
            deadline_at=self.deadline_at,
            scope_bindings=self.scope_bindings,
            intent_digest=self.intent_digest,
            grant_digest=grant.digest,
            workspace_ref=grant.workspace_ref,
            request_digest=hashlib.sha256(encoded).hexdigest(),
        )


@dataclass(frozen=True)
class PreparedLaunch:
    effect_id: str
    request_digest: str
    runner_binding_digest: str
    launch_ref: str
    workspace_ref: str | None


def launch_commitment_digest(request: EffectRequest, launch: PreparedLaunch) -> str:
    commitment = bounded_json(
        {
            "schema": "lockstep.launch-commitment/v1",
            "effect_id": launch.effect_id,
            "request_digest": launch.request_digest,
            "runner_binding_digest": launch.runner_binding_digest,
            "launch_ref": launch.launch_ref,
            "workspace_ref": launch.workspace_ref,
            "grant_digest": request.grant_digest,
        },
        label="prepared launch commitment",
    )
    encoded = json.dumps(
        commitment,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    workspace_quarantined: bool = False

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
        workspace_quarantined: bool = False,
    ) -> TerminalSafetyObservation:
        return cls(
            launch.effect_id,
            launch.request_digest,
            launch.runner_binding_digest,
            "proven",
            result_stable=result_stable,
            rollover_snapshot_ref=rollover_snapshot_ref,
            workspace_quarantined=workspace_quarantined,
        )


class RunnerAdapter(Protocol):
    binding_digest: str
    required_authorities: tuple[str, ...]
    reconciliation_boundary: Literal["local_durable_handle"]

    def prepare(self, request: EffectRequest) -> PreparedLaunch:
        """Idempotently recover or create one durable preparation for request."""
        ...

    def ensure_started(self, launch: PreparedLaunch) -> RunnerObservation:
        """Idempotently start or adopt this exact durable preparation after restart."""
        ...

    def inspect(self, effect_id: str) -> RunnerObservation:
        """Observe only an existing local durable handle; never launch/contact remote."""
        ...

    def cancel(self, effect_id: str) -> RunnerObservation:
        """Signal only an existing local durable handle; never use network/credentials."""
        ...

    def quiesce(self, effect_id: str) -> TerminalSafetyObservation:
        """Prove local quiescence without network, credentials, or a new effect."""
        ...
