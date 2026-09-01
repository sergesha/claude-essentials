"""Closed values and canonical projections for owner publication consent."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.models import AcceptDescriptor
from lockstep.runtime.native_models import NativeCoordinate


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{label} must be bounded non-empty text")
    return value

def _digest(value: object, label: str) -> str:
    checked = _text(value, label)
    if len(checked) != 64 or any(char not in "0123456789abcdef" for char in checked):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return checked

def _coordinate_data(source: NativeCoordinate) -> dict[str, str]:
    if not isinstance(source, NativeCoordinate):
        raise TypeError("consent source must be a NativeCoordinate")
    return {
        "thread_id": _text(source.thread_id, "source thread_id"),
        "checkpoint_ns": source.checkpoint_ns,
        "checkpoint_id": _text(source.checkpoint_id, "source checkpoint_id"),
        "task_id": _text(source.task_id, "source task_id"),
        "interrupt_id": _text(source.interrupt_id, "source interrupt_id"),
    }

def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value.astimezone(UTC)

@dataclass(frozen=True)
class PublicationConsentCommitment:
    schema: Literal["lockstep.publication-consent-commitment/v1"]
    public_run_id: str
    project_identity: str
    definition_digest: str
    source: NativeCoordinate
    effect_id: str
    descriptor_digest: str
    producer_effect_id: str
    artifact_ref: str
    artifact_digest: str
    destination: str
    transformation: Literal["identity"]
    audience: Literal["local-project"]
    digest: str

    @classmethod
    def build(
        cls,
        *,
        binding: RunBinding,
        source: NativeCoordinate,
        effect_id: str,
        descriptor: AcceptDescriptor,
        producer_effect_id: str,
        artifact_ref: str,
        artifact_digest: str,
    ) -> "PublicationConsentCommitment":
        if not isinstance(binding, RunBinding):
            raise TypeError("consent binding must be a RunBinding")
        if not isinstance(descriptor, AcceptDescriptor):
            raise TypeError("consent descriptor must be an AcceptDescriptor")
        data = {
            "schema": "lockstep.publication-consent-commitment/v1",
            "public_run_id": _text(binding.public_run_id, "public_run_id"),
            "project_identity": _text(binding.project_identity, "project_identity"),
            "definition_digest": _digest(binding.recipe_digest, "definition_digest"),
            "source": _coordinate_data(source),
            "effect_id": _text(effect_id, "effect_id"),
            "descriptor_digest": _digest(
                descriptor.digest, "descriptor_digest"
            ),
            "producer_effect_id": _text(producer_effect_id, "producer_effect_id"),
            "artifact_ref": _text(artifact_ref, "artifact_ref"),
            "artifact_digest": _digest(artifact_digest, "artifact_digest"),
            "destination": descriptor.destination,
            "transformation": descriptor.transformation,
            "audience": descriptor.audience,
        }
        digest = hashlib.sha256(_canonical(data)).hexdigest()
        return cls(
            data["schema"],
            data["public_run_id"],
            data["project_identity"],
            data["definition_digest"],
            source,
            data["effect_id"],
            data["descriptor_digest"],
            data["producer_effect_id"],
            data["artifact_ref"],
            data["artifact_digest"],
            data["destination"],
            data["transformation"],
            data["audience"],
            digest,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "public_run_id": self.public_run_id,
            "project_identity": self.project_identity,
            "definition_digest": self.definition_digest,
            "source": _coordinate_data(self.source),
            "effect_id": self.effect_id,
            "descriptor_digest": self.descriptor_digest,
            "producer_effect_id": self.producer_effect_id,
            "artifact_ref": self.artifact_ref,
            "artifact_digest": self.artifact_digest,
            "destination": self.destination,
            "transformation": self.transformation,
            "audience": self.audience,
            "digest": self.digest,
        }

@dataclass(frozen=True)
class IssuedPublicationConsent:
    consent_ref: str
    token: str
    commitment_digest: str
    consent_epoch: int

@dataclass(frozen=True)
class StoredPublicationConsent:
    consent_ref: str
    commitment: PublicationConsentCommitment
    consent_epoch: int
    redeemed_at: datetime | None
    receipt_digest: str | None
