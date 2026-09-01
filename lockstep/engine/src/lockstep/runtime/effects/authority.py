"""Provider-neutral exact grants and revocation serialization for effects."""

from __future__ import annotations

import hashlib
import json
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from lockstep.runtime.payload_limits import bounded_json
from lockstep.runtime.providers.base import EffectRequest, PreparedLaunch

if TYPE_CHECKING:
    from lockstep.runtime.publication import PreparedPublication


class EffectAuthorityDenied(RuntimeError):
    """No current exact grant authorizes this effect commitment."""


class EffectAuthorityUnavailable(RuntimeError):
    """The trusted grant/revocation authority cannot decide currentness."""


def _digest(value: str, label: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _generation(value: int | None, label: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _text(value: str, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


@dataclass(frozen=True)
class EffectGrant:
    schema: str
    intent_digest: str
    actor_binding_digest: str
    project_identity: str
    public_run_id: str
    definition_digest: str
    effect_id: str
    descriptor_digest: str
    runner_binding_digest: str
    required_authorities: tuple[str, ...]
    workspace_ref: str | None
    parent_capability_generation: int
    grant_generation: int
    policy_epoch: int
    config_epoch: int
    approval_generation: int | None
    expires_at: datetime
    digest: str

    def validate_for(self, intent: EffectRequest) -> None:
        """Reject a non-canonical or incompletely bound authority decision."""

        rebuilt = self.build(
            intent,
            actor_binding_digest=self.actor_binding_digest,
            required_authorities=self.required_authorities,
            workspace_ref=self.workspace_ref,
            parent_capability_generation=self.parent_capability_generation,
            grant_generation=self.grant_generation,
            policy_epoch=self.policy_epoch,
            config_epoch=self.config_epoch,
            approval_generation=self.approval_generation,
            expires_at=self.expires_at,
        )
        if rebuilt != self:
            raise ValueError("effect grant is not canonical for this exact intent")

    @classmethod
    def build(
        cls,
        intent: EffectRequest,
        *,
        actor_binding_digest: str,
        required_authorities: tuple[str, ...],
        workspace_ref: str | None,
        parent_capability_generation: int,
        grant_generation: int,
        policy_epoch: int,
        config_epoch: int,
        approval_generation: int | None,
        expires_at: datetime,
    ) -> EffectGrant:
        if intent.grant_digest is not None:
            raise ValueError("grant resolution requires an ungranted effect intent")
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("grant expiry must include a timezone")
        expiry = expires_at.astimezone(UTC)
        checked_authorities = []
        for authority in required_authorities:
            checked = _text(authority, "required authority")
            assert checked is not None
            checked_authorities.append(checked)
        authorities = tuple(checked_authorities)
        if not authorities or len(set(authorities)) != len(authorities):
            raise ValueError("grant authorities must be a non-empty unique tuple")
        checked_workspace_ref = _text(
            workspace_ref, "grant workspace_ref", optional=True
        )
        commitment = bounded_json(
            {
                "schema": "lockstep.effect-grant/v1",
                "intent_digest": _digest(intent.intent_digest, "intent_digest"),
                "actor_binding_digest": _digest(
                    actor_binding_digest, "actor_binding_digest"
                ),
                "project_identity": intent.project_identity,
                "public_run_id": intent.public_run_id,
                "definition_digest": intent.definition_digest,
                "effect_id": intent.effect_id,
                "descriptor_digest": intent.descriptor_digest,
                "runner_binding_digest": intent.runner_binding_digest,
                "required_authorities": list(authorities),
                "workspace_ref": checked_workspace_ref,
                "parent_capability_generation": _generation(
                    parent_capability_generation, "parent_capability_generation"
                ),
                "grant_generation": _generation(grant_generation, "grant_generation"),
                "policy_epoch": _generation(policy_epoch, "policy_epoch"),
                "config_epoch": _generation(config_epoch, "config_epoch"),
                "approval_generation": _generation(
                    approval_generation, "approval_generation", optional=True
                ),
                "expires_at": expiry.isoformat(),
            },
            label="effect grant",
        )
        encoded = json.dumps(
            commitment,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return cls(
            schema="lockstep.effect-grant/v1",
            intent_digest=intent.intent_digest,
            actor_binding_digest=actor_binding_digest,
            project_identity=intent.project_identity,
            public_run_id=intent.public_run_id,
            definition_digest=intent.definition_digest,
            effect_id=intent.effect_id,
            descriptor_digest=intent.descriptor_digest,
            runner_binding_digest=intent.runner_binding_digest,
            required_authorities=authorities,
            workspace_ref=checked_workspace_ref,
            parent_capability_generation=parent_capability_generation,
            grant_generation=grant_generation,
            policy_epoch=policy_epoch,
            config_epoch=config_epoch,
            approval_generation=approval_generation,
            expires_at=expiry,
            digest=hashlib.sha256(encoded).hexdigest(),
        )


class EffectAuthorityGate(Protocol):
    def resolve(self, intent: EffectRequest) -> EffectGrant: ...

    def commitment(
        self,
        grant: EffectGrant,
        request: EffectRequest,
        launch: PreparedLaunch | PreparedPublication,
    ) -> AbstractContextManager[None]: ...
