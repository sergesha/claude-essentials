"""Immutable data-only values at the protected-effect boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True)
class StateSelector:
    state_key: str


@dataclass(frozen=True)
class RunnerDescriptor:
    selector: str
    required_capabilities: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactDescriptor:
    name: str
    media_type: str
    required: bool


@dataclass(frozen=True)
class EffectDescriptor:
    schema: str
    kind: str
    logical_id: str
    runner: RunnerDescriptor | None
    inputs: tuple[tuple[str, StateSelector], ...]
    writes: tuple[str, ...]
    artifacts: tuple[ArtifactDescriptor, ...]
    deadline_seconds: int | None
    scope_state_keys: tuple[str, ...]
    result_schema: str
    canonical_json: bytes
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "logical_id": self.logical_id,
            "runner": (
                None
                if self.runner is None
                else {
                    "selector": self.runner.selector,
                    "required_capabilities": list(self.runner.required_capabilities),
                }
            ),
            "inputs": {
                name: {"state_key": selector.state_key}
                for name, selector in self.inputs
            },
            "writes": list(self.writes),
            "artifacts": [
                {
                    "name": artifact.name,
                    "media_type": artifact.media_type,
                    "required": artifact.required,
                }
                for artifact in self.artifacts
            ],
            "deadline_seconds": self.deadline_seconds,
            "scope_state_keys": list(self.scope_state_keys),
            "result_schema": self.result_schema,
        }


@dataclass(frozen=True)
class ScopeDescriptor:
    schema: str
    kind: Literal["scope"]
    logical_id: str
    scope_kind: Literal["call", "parallel"]
    duration_seconds: int | None
    runner_selector: str | None
    ancestor_deadline_state_keys: tuple[str, ...]
    result_state_key: str
    result_schema: str
    canonical_json: bytes
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "logical_id": self.logical_id,
            "scope_kind": self.scope_kind,
            "duration_seconds": self.duration_seconds,
            "runner_selector": self.runner_selector,
            "ancestor_deadline_state_keys": list(self.ancestor_deadline_state_keys),
            "result_state_key": self.result_state_key,
            "result_schema": self.result_schema,
        }


@dataclass(frozen=True)
class EffectResult:
    schema: str
    effect_id: str
    outcome: Literal["PASS", "FAIL", "ERROR"]
    result_ref: str | None
    artifact_refs: tuple[str, ...]
    snapshot_ref: str | None
    diff_ref: str | None
    fixed_error_code: str | None
    evidence_refs: tuple[str, ...]
    canonical_json: bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "effect_id": self.effect_id,
            "outcome": self.outcome,
            "result_ref": self.result_ref,
            "artifact_refs": list(self.artifact_refs),
            "snapshot_ref": self.snapshot_ref,
            "diff_ref": self.diff_ref,
            "fixed_error_code": self.fixed_error_code,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class ScopeResult:
    schema: str
    effect_id: str
    outcome: Literal["PASS", "ERROR"]
    scope_kind: Literal["call", "parallel"]
    scope_digest: str
    absolute_deadline: datetime | None = None
    runner_selector: str | None = None
    runner_binding_digest: str | None = None
    fixed_error_code: Literal["scope_timeout"] | None = None

    def to_dict(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "schema": self.schema,
            "effect_id": self.effect_id,
            "outcome": self.outcome,
            "scope_kind": self.scope_kind,
            "scope_digest": self.scope_digest,
        }
        if self.outcome == "ERROR":
            base["fixed_error_code"] = self.fixed_error_code
        else:
            base["absolute_deadline"] = (
                None
                if self.absolute_deadline is None
                else self.absolute_deadline.isoformat()
            )
            if self.runner_selector is not None:
                base["runner_selector"] = self.runner_selector
                base["runner_binding_digest"] = self.runner_binding_digest
        return base
