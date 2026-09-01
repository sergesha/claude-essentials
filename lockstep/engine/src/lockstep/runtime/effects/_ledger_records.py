"""Closed effect-ledger records and canonical scalar projections."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from lockstep.runtime.effects.models import (
    AcceptanceResult,
    EffectResult,
    ScopeResult,
)
from lockstep.runtime.native_models import NativeCoordinate


@dataclass(frozen=True, slots=True)
class RunDriveWatch:
    """Durable v2 discovery record without workflow or scheduling state."""

    admission_seq: int
    public_run_id: str
    input_blob_sha256: str | None
    input_blob_size: int | None
    admitted_at: datetime

    def __post_init__(self) -> None:
        if type(self.admission_seq) is not int or self.admission_seq <= 0:
            raise ValueError("admission_seq must be a positive integer")
        if type(self.public_run_id) is not str or not self.public_run_id:
            raise ValueError("public_run_id must be a non-empty string")
        if (self.input_blob_sha256 is None) != (self.input_blob_size is None):
            raise ValueError(
                "input blob digest and size must both be null or both be non-null"
            )
        if self.input_blob_sha256 is not None and (
            type(self.input_blob_sha256) is not str
            or len(self.input_blob_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.input_blob_sha256)
        ):
            raise ValueError(
                "input_blob_sha256 must be a lowercase SHA-256 digest"
            )
        if self.input_blob_size is not None and (
            type(self.input_blob_size) is not int
            or self.input_blob_size < 0
            or self.input_blob_size > 64 * 1024 * 1024
        ):
            raise ValueError(
                "input_blob_size must be a non-negative integer "
                "not exceeding 64 MiB"
            )
        if (
            not isinstance(self.admitted_at, datetime)
            or self.admitted_at.tzinfo is None
            or self.admitted_at.utcoffset() is None
        ):
            raise ValueError("admitted_at must be a timezone-aware datetime")
        object.__setattr__(self, "admitted_at", self.admitted_at.astimezone(UTC))


@dataclass(frozen=True)
class EffectRecord:
    effect_id: str
    coordinate: NativeCoordinate
    descriptor_digest: str
    effect_kind: str
    deadline_at: datetime | None
    phase: str
    lease_epoch: int
    runner_binding_digest: str | None
    workspace_ref: str | None
    request_digest: str | None
    grant_digest: str | None
    launch_commitment_digest: str | None
    result_ref: str | None
    fixed_error_code: str | None
    created_at: datetime
    updated_at: datetime
    revision: int
    result: EffectResult | ScopeResult | AcceptanceResult | None = None


@dataclass(frozen=True)
class _PreparedEffectFacts:
    effect_id: str
    coordinate: NativeCoordinate
    descriptor_digest: str
    effect_kind: str
    deadline_at: datetime | None
    runner_binding_digest: str | None
    workspace_ref: str | None
    request_digest: str | None
    grant_digest: str | None
    created_at: datetime

    def insert_values(self) -> dict[str, object]:
        timestamp = _dump(self.created_at)
        return {
            "effect_id": self.effect_id,
            "thread_id": self.coordinate.thread_id,
            "checkpoint_ns": self.coordinate.checkpoint_ns,
            "checkpoint_id": self.coordinate.checkpoint_id,
            "task_id": self.coordinate.task_id,
            "interrupt_id": self.coordinate.interrupt_id,
            "descriptor_digest": self.descriptor_digest,
            "effect_kind": self.effect_kind,
            "deadline_at": None if self.deadline_at is None else _dump(self.deadline_at),
            "phase": "prepared",
            "lease_epoch": 0,
            "runner_binding_digest": self.runner_binding_digest,
            "workspace_ref": self.workspace_ref,
            "request_digest": self.request_digest,
            "grant_digest": self.grant_digest,
            "launch_commitment_digest": None,
            "result_ref": None,
            "fixed_error_code": None,
            "created_at": timestamp,
            "updated_at": timestamp,
            "revision": 0,
        }

    def immutable_values(self) -> dict[str, object]:
        return {
            "deadline_at": self.deadline_at,
            "workspace_ref": self.workspace_ref,
            "request_digest": self.request_digest,
            "grant_digest": self.grant_digest,
            "effect_kind": self.effect_kind,
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _dump(value: datetime) -> str:
    return _utc(value).isoformat()


def _load(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value).astimezone(UTC)


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


def _binding_digest(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("runner binding must be a lowercase SHA-256 digest")
    return value


def _clock_now(clock: Callable[[], datetime]) -> datetime:
    return _utc(clock())
