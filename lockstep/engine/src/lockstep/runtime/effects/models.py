"""Immutable data-only values at the protected-effect boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal


@dataclass(frozen=True)
class StateSelector:
    state_key: str


@dataclass(frozen=True)
class RuntimeInputSelector:
    runtime_key: Literal["run_start_project_snapshot", "current_project_snapshot"]


InputSelector = StateSelector | RuntimeInputSelector


@dataclass(frozen=True)
class PinnedCommandSpec:
    logical_argv: tuple[str, ...]
    logical_cwd: str
    result_source: Literal["exit", "file", "junit"]

    @classmethod
    def build(
        cls,
        *,
        logical_argv: tuple[str, ...],
        logical_cwd: str,
        result_source: Literal["exit", "file", "junit"] = "exit",
    ) -> "PinnedCommandSpec":
        if (
            not isinstance(logical_argv, tuple)
            or not logical_argv
            or len(logical_argv) > 128
            or any(
                not isinstance(item, str)
                or not item
                or "\x00" in item
                or len(item.encode()) > 4096
                for item in logical_argv
            )
        ):
            raise ValueError("pinned argv must be a bounded non-empty array")
        if (
            not isinstance(logical_cwd, str)
            or not logical_cwd
            or "\x00" in logical_cwd
        ):
            raise ValueError("pinned cwd must be a bounded relative path")
        cwd = PurePosixPath(logical_cwd)
        if cwd.is_absolute() or any(part in {"", ".."} for part in cwd.parts):
            raise ValueError("pinned cwd must remain inside its workspace")
        if result_source not in {"exit", "file", "junit"}:
            raise ValueError("unknown pinned result source")
        return cls(logical_argv, logical_cwd, result_source)

    @classmethod
    def parse(cls, value: object) -> "PinnedCommandSpec":
        if not isinstance(value, Mapping) or set(value) != {
            "schema",
            "logical_argv",
            "logical_cwd",
            "result_source",
        }:
            raise ValueError("invalid closed pinned command spec")
        if value["schema"] != "lockstep.pinned-command/v1":
            raise ValueError("unsupported pinned command spec")
        argv = value["logical_argv"]
        if not isinstance(argv, list):
            raise ValueError("pinned argv must be an array")
        return cls.build(
            logical_argv=tuple(argv),
            logical_cwd=value["logical_cwd"],
            result_source=value["result_source"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "lockstep.pinned-command/v1",
            "logical_argv": list(self.logical_argv),
            "logical_cwd": self.logical_cwd,
            "result_source": self.result_source,
        }


@dataclass(frozen=True)
class RunnerDescriptor:
    selector: str
    required_capabilities: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactDescriptor:
    name: str
    source_path: str
    media_type: str
    required: bool


@dataclass(frozen=True)
class ManualParallelContract:
    """Immutable cooperative write surface of one authored parallel block."""

    id: str
    branch: str
    writes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "branch": self.branch, "writes": list(self.writes)}


@dataclass(frozen=True)
class EffectDescriptor:
    schema: str
    kind: str
    logical_id: str
    runner: RunnerDescriptor | None
    inputs: tuple[tuple[str, InputSelector], ...]
    writes: tuple[str, ...]
    artifacts: tuple[ArtifactDescriptor, ...]
    deadline_seconds: int | None
    scope_state_keys: tuple[str, ...]
    result_schema: str
    canonical_json: bytes
    digest: str
    parallel: ManualParallelContract | None = None

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
                name: (
                    {"state_key": selector.state_key}
                    if isinstance(selector, StateSelector)
                    else {"runtime_key": selector.runtime_key}
                )
                for name, selector in self.inputs
            },
            "writes": list(self.writes),
            "artifacts": [
                {
                    "name": artifact.name,
                    "source_path": artifact.source_path,
                    "media_type": artifact.media_type,
                    "required": artifact.required,
                }
                for artifact in self.artifacts
            ],
            "deadline_seconds": self.deadline_seconds,
            "scope_state_keys": list(self.scope_state_keys),
            "result_schema": self.result_schema,
            **({"parallel": self.parallel.to_dict()} if self.parallel is not None else {}),
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
class DecisionCase:
    label: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class DecisionSpec:
    decision_type: Literal["changed-paths"]
    since: Literal["start"]
    cases: tuple[DecisionCase, ...]
    default: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.decision_type,
            "since": self.since,
            "cases": [
                {"label": case.label, "paths": list(case.paths)}
                for case in self.cases
            ],
            "default": self.default,
        }


@dataclass(frozen=True)
class DecisionDescriptor:
    schema: str
    kind: Literal["decide"]
    logical_id: str
    decision: DecisionSpec
    inputs: tuple[tuple[str, RuntimeInputSelector], ...]
    result_schema: str
    canonical_json: bytes
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "logical_id": self.logical_id,
            "decision": self.decision.to_dict(),
            "inputs": {
                name: {"runtime_key": selector.runtime_key}
                for name, selector in self.inputs
            },
            "result_schema": self.result_schema,
        }


@dataclass(frozen=True)
class AcceptDescriptor:
    schema: str
    kind: Literal["accept"]
    logical_id: str
    artifact_handle: str
    producer_result_state_key: str
    declared_name: str
    destination: str
    transformation: Literal["identity"]
    audience: Literal["local-project"]
    verdict: Literal["PASS"]
    result_schema: str
    canonical_json: bytes
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "logical_id": self.logical_id,
            "artifact_handle": self.artifact_handle,
            "producer_result_state_key": self.producer_result_state_key,
            "declared_name": self.declared_name,
            "destination": self.destination,
            "transformation": self.transformation,
            "audience": self.audience,
            "verdict": self.verdict,
            "result_schema": self.result_schema,
        }


@dataclass(frozen=True)
class PublishItem:
    qualified_handle: str
    producer_result_state_key: str
    declared_name: str
    acceptance_result_state_key: str
    destination: str
    transformation: Literal["identity"]
    audience: Literal["local-project"]


@dataclass(frozen=True)
class PublishDescriptor:
    schema: str
    kind: Literal["publish"]
    logical_id: str
    items: tuple[PublishItem, ...]
    result_schema: str
    canonical_json: bytes
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "logical_id": self.logical_id,
            "items": [
                {
                    "qualified_handle": item.qualified_handle,
                    "producer_result_state_key": item.producer_result_state_key,
                    "declared_name": item.declared_name,
                    "acceptance_result_state_key": item.acceptance_result_state_key,
                    "destination": item.destination,
                    "transformation": item.transformation,
                    "audience": item.audience,
                }
                for item in self.items
            ],
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


@dataclass(frozen=True)
class DecisionResult:
    schema: str
    effect_id: str
    outcome: Literal["PASS", "ERROR"]
    decision_digest: str
    value: str | None = None
    fixed_error_code: Literal["decision_input_invalid"] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": self.schema,
            "effect_id": self.effect_id,
            "outcome": self.outcome,
            "decision_digest": self.decision_digest,
        }
        if self.outcome == "PASS":
            result["value"] = self.value
        else:
            result["fixed_error_code"] = self.fixed_error_code
        return result


@dataclass(frozen=True)
class AcceptanceResult:
    schema: str
    effect_id: str
    outcome: Literal["PASS"]
    artifact_ref: str
    artifact_digest: str
    destination: str
    transformation: Literal["identity"]
    audience: Literal["local-project"]
    consent_ref: str
    approval_generation: int
    receipt_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "effect_id": self.effect_id,
            "outcome": self.outcome,
            "artifact_ref": self.artifact_ref,
            "artifact_digest": self.artifact_digest,
            "destination": self.destination,
            "transformation": self.transformation,
            "audience": self.audience,
            "consent_ref": self.consent_ref,
            "approval_generation": self.approval_generation,
            "receipt_digest": self.receipt_digest,
        }
