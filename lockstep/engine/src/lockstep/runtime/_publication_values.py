"""Closed values and canonical projections for project publication."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from lockstep.runtime.artifacts import ArtifactRef
from lockstep.runtime.blobs import BlobRef
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.owner_state import StorageLimitExceeded, take_bounded
from lockstep.runtime.project_paths import (
    PortableProjectPath,
    ProjectTreeLimits,
    validate_portable_project_paths,
)

_HEX = frozenset("0123456789abcdef")


class PublicationError(RuntimeError):
    pass


class PublicationConflict(PublicationError):
    pass


class PublicationJournalError(PublicationError):
    pass


@dataclass(frozen=True)
class PublicationLimits:
    max_entries: int = 32
    max_journal_bytes: int = 1024 * 1024
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        if min(
            self.max_entries,
            self.max_journal_bytes,
            self.max_file_bytes,
            self.max_total_bytes,
        ) <= 0:
            raise ValueError("publication limits must be positive")


@dataclass(frozen=True)
class PublicationEntry:
    artifact_ref: ArtifactRef
    destination: str
    transformation: str = "identity"

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_ref", ArtifactRef.parse(self.artifact_ref))
        if (
            not isinstance(self.destination, str)
            or len(self.destination.encode("utf-8")) > 4096
        ):
            raise ValueError("publication destination must be bounded text")
        path = PortableProjectPath.parse(self.destination, "file")
        if self.transformation != "identity":
            raise ValueError("only identity artifact publication is supported")
        object.__setattr__(self, "destination", path.value)


@dataclass(frozen=True)
class PublicationRequest:
    effect_id: str
    public_run_id: str
    project_identity: str
    definition_digest: str
    coordinate: NativeCoordinate
    descriptor_digest: str
    authority_request_digest: str
    grant_digest: str
    publisher_binding_digest: str
    consent_ref: str
    approval_generation: int
    policy_epoch: int
    config_epoch: int
    parent_capability_generation: int
    entries: tuple[PublicationEntry, ...]
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
        authority_request_digest: str,
        grant_digest: str,
        publisher_binding_digest: str,
        consent_ref: str,
        approval_generation: int,
        policy_epoch: int,
        config_epoch: int,
        parent_capability_generation: int,
        entries: Iterable[PublicationEntry],
    ) -> PublicationRequest:
        values = take_bounded(entries, 32, "publication entries")
        if not values:
            raise ValueError("publication requires at least one entry")
        if any(not isinstance(item, PublicationEntry) for item in values):
            raise TypeError("publication entries must be closed values")
        validate_portable_project_paths(
            ((item.destination, "file") for item in values),
            limits=ProjectTreeLimits(max_entries=32),
            label="publication destinations",
        )
        scalar = {
            "effect_id": _text(effect_id, "effect_id"),
            "public_run_id": _text(public_run_id, "public_run_id"),
            "project_identity": _text(project_identity, "project_identity"),
            "definition_digest": _digest(definition_digest, "definition digest"),
            "descriptor_digest": _digest(descriptor_digest, "descriptor digest"),
            "authority_request_digest": _digest(
                authority_request_digest, "authority request digest"
            ),
            "grant_digest": _digest(grant_digest, "grant digest"),
            "publisher_binding_digest": _digest(
                publisher_binding_digest, "publisher binding digest"
            ),
            "consent_ref": _text(consent_ref, "consent_ref"),
        }
        generations = {
            "approval_generation": _counter(approval_generation, "approval generation"),
            "policy_epoch": _counter(policy_epoch, "policy epoch"),
            "config_epoch": _counter(config_epoch, "config epoch"),
            "parent_capability_generation": _counter(
                parent_capability_generation, "parent capability generation"
            ),
        }
        coordinate_data = _coordinate_data(coordinate)
        data = {
            "schema": "lockstep.publication-request/v1",
            **scalar,
            **generations,
            "coordinate": coordinate_data,
            "entries": [_entry_data(item) for item in values],
        }
        encoded = _canonical(data)
        if len(encoded) > 1024 * 1024:
            raise StorageLimitExceeded("publication request exceeds admission limit")
        request_digest = hashlib.sha256(encoded).hexdigest()
        return cls(
            effect_id=scalar["effect_id"],
            public_run_id=scalar["public_run_id"],
            project_identity=scalar["project_identity"],
            definition_digest=scalar["definition_digest"],
            coordinate=coordinate,
            descriptor_digest=scalar["descriptor_digest"],
            authority_request_digest=scalar["authority_request_digest"],
            grant_digest=scalar["grant_digest"],
            publisher_binding_digest=scalar["publisher_binding_digest"],
            consent_ref=scalar["consent_ref"],
            approval_generation=generations["approval_generation"],
            policy_epoch=generations["policy_epoch"],
            config_epoch=generations["config_epoch"],
            parent_capability_generation=generations[
                "parent_capability_generation"
            ],
            entries=values,
            request_digest=request_digest,
        )


@dataclass(frozen=True)
class PreparedPublication:
    journal_digest: str
    request_digest: str
    publisher_binding_digest: str


class PublicationPhase(StrEnum):
    """Closed persisted lifecycle of one publication journal."""

    PREPARED = "prepared"
    APPLYING = "applying"
    ROLLBACK_PENDING = "rollback_pending"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class PublicationReceipt:
    journal_digest: str
    request_digest: str
    phase: str


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise ValueError(f"{label} must be bounded non-empty text")
    return value


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _counter(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _coordinate_data(value: NativeCoordinate) -> dict[str, str]:
    if not isinstance(value, NativeCoordinate):
        raise TypeError("publication coordinate must be NativeCoordinate")
    data = {
        "thread_id": value.thread_id,
        "checkpoint_ns": value.checkpoint_ns,
        "checkpoint_id": value.checkpoint_id,
        "task_id": value.task_id,
        "interrupt_id": value.interrupt_id,
    }
    for field, item in data.items():
        if not isinstance(item, str) or (field != "checkpoint_ns" and not item):
            raise ValueError(f"coordinate {field} must be bounded text")
        if len(item.encode()) > 4096:
            raise ValueError(f"coordinate {field} must be bounded text")
    return data


def _entry_data(entry: PublicationEntry) -> dict[str, str]:
    return {
        "artifact_ref": str(entry.artifact_ref),
        "destination": entry.destination,
        "transformation": entry.transformation,
    }


def _image_data(value: tuple[BlobRef, int] | None) -> dict[str, object] | None:
    if value is None:
        return None
    ref, mode = value
    return {"sha256": ref.sha256, "size": ref.size, "mode": mode}


def _image_from_data(value: object) -> tuple[BlobRef, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"sha256", "size", "mode"}:
        raise ValueError("invalid file image")
    digest = _digest(value["sha256"], "blob digest")
    if type(value["size"]) is not int or value["size"] < 0:
        raise ValueError("invalid blob size")
    if type(value["mode"]) is not int or value["mode"] < 0 or value["mode"] > 0o777:
        raise ValueError("invalid file mode")
    return BlobRef(digest, value["size"]), value["mode"]


def _same_image(
    left: tuple[BlobRef, int] | None, right: tuple[BlobRef, int] | None
) -> bool:
    return left == right


def _request_data(request: PublicationRequest) -> dict[str, object]:
    return {
        "schema": "lockstep.publication-request/v1",
        "effect_id": request.effect_id,
        "public_run_id": request.public_run_id,
        "project_identity": request.project_identity,
        "definition_digest": request.definition_digest,
        "coordinate": _coordinate_data(request.coordinate),
        "descriptor_digest": request.descriptor_digest,
        "authority_request_digest": request.authority_request_digest,
        "grant_digest": request.grant_digest,
        "publisher_binding_digest": request.publisher_binding_digest,
        "consent_ref": request.consent_ref,
        "approval_generation": request.approval_generation,
        "policy_epoch": request.policy_epoch,
        "config_epoch": request.config_epoch,
        "parent_capability_generation": request.parent_capability_generation,
        "entries": [_entry_data(item) for item in request.entries],
    }
