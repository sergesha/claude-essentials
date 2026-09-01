"""Static runtime requirements derived from authorized recipe closures."""

# ruff: noqa: F401 - the owner-policy identity boundary re-exports this module.

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from lockstep.runtime.effects._owner_policy_values import (
    OwnerRuntimeSnapshot,
    _lower_hex,
    _RuntimeBindingFacts,
)
from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.effects.models import EffectDescriptor

if TYPE_CHECKING:
    from lockstep.recipe.authority import AuthorizedRecipe


def _canonical_digest(value: dict[str, object]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_bounded_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{label} must be a tuple")
    if len(value) > 256:
        raise ValueError(f"{label} exceeds 256 entries")
    if any(
        not isinstance(item, str)
        or not item
        or len(item.encode("utf-8")) > 512
        for item in value
    ):
        raise ValueError(f"{label} entries must be non-empty UTF-8 strings up to 512 bytes")
    if value != tuple(sorted(value)) or len(set(value)) != len(value):
        raise ValueError(f"{label} must be sorted and unique")
    return value


def _canonical_uses(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, tuple):
        raise TypeError("runtime requirement uses must be a tuple")
    if len(value) > 256:
        raise ValueError("runtime requirement uses exceed 256 entries")
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("each runtime requirement use must be a two-string tuple")
        if any(
            not isinstance(part, str) or len(part.encode("utf-8")) > 512
            for part in item
        ):
            raise ValueError("runtime requirement use strings must not exceed 512 bytes")
    if value != tuple(sorted(value)) or len(set(value)) != len(value):
        raise ValueError("runtime requirement uses must be sorted and unique")
    return value


def grant_selection_key(
    *,
    project_identity: str,
    definition_digest: str,
    protected_descriptor_digest: str,
    runner_selector: str,
    required_capabilities: tuple[str, ...],
    required_authorities: tuple[str, ...],
) -> str:
    capabilities = _canonical_bounded_tuple(
        required_capabilities, "required capabilities"
    )
    authorities = _canonical_bounded_tuple(
        required_authorities, "required authorities"
    )
    return _canonical_digest(
        {
            "schema": "lockstep.runtime-grant-selection/v1",
            "project_identity": project_identity,
            "definition_digest": definition_digest,
            "protected_descriptor_digest": protected_descriptor_digest,
            "runner_selector": runner_selector,
            "required_capabilities": capabilities,
            "required_authorities": authorities,
        }
    )


def requirement_digest(
    *,
    grant_selection_key: str,
    runner_binding_digest: str,
    config_generation: int,
) -> str:
    return _canonical_digest(
        {
            "schema": "lockstep.runtime-requirement/v1",
            "grant_selection_key": grant_selection_key,
            "runner_binding_digest": runner_binding_digest,
            "config_generation": config_generation,
        }
    )


@dataclass(frozen=True, slots=True)
class RuntimeRequirement:
    """One static runtime authority requirement from an authorized closure."""

    grant_selection_key: str
    project_identity: str
    definition_digest: str
    protected_descriptor_digest: str
    runner_selector: str
    required_capabilities: tuple[str, ...]
    required_authorities: tuple[str, ...]
    uses: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _canonical_uses(self.uses)
        expected = grant_selection_key(
            project_identity=self.project_identity,
            definition_digest=self.definition_digest,
            protected_descriptor_digest=self.protected_descriptor_digest,
            runner_selector=self.runner_selector,
            required_capabilities=self.required_capabilities,
            required_authorities=self.required_authorities,
        )
        if self.grant_selection_key != expected:
            raise ValueError("runtime requirement selection key does not match")


def _stable_requirement_facts(requirement: RuntimeRequirement) -> tuple[object, ...]:
    return (
        requirement.grant_selection_key,
        requirement.project_identity,
        requirement.definition_digest,
        requirement.protected_descriptor_digest,
        requirement.runner_selector,
        requirement.required_capabilities,
        requirement.required_authorities,
    )


def _merge_requirement(
    requirements: dict[str, RuntimeRequirement],
    requirement: RuntimeRequirement,
) -> None:
    existing = requirements.get(requirement.grant_selection_key)
    if existing is None:
        requirements[requirement.grant_selection_key] = requirement
    elif _stable_requirement_facts(existing) != _stable_requirement_facts(
        requirement
    ):
        raise ValueError("runtime requirement selection key collision")
    else:
        requirements[requirement.grant_selection_key] = replace(
            existing,
            uses=tuple(sorted(set(existing.uses + requirement.uses))),
        )


def _runtime_descriptors(encoded: bytes) -> tuple[EffectDescriptor, ...]:
    document = json.loads(encoded)
    known_state_keys = set(document.get("state") or {})
    descriptors: list[EffectDescriptor] = []
    for node in (document.get("nodes") or {}).values():
        if not isinstance(node, dict) or node.get("type") != "interrupt":
            continue
        message = node.get("message")
        if not isinstance(message, dict) or "lockstep_effect" not in message:
            continue
        descriptor = parse_effect_descriptor(
            message["lockstep_effect"],
            known_state_keys=known_state_keys,
        )
        if (
            isinstance(descriptor, EffectDescriptor)
            and descriptor.runner is not None
        ):
            descriptors.append(descriptor)
    return tuple(descriptors)


@dataclass(frozen=True, slots=True)
class RuntimeRequirementIndex:
    """Pure static inventory for an authorized recipe closure."""

    project_identity: str
    requirements: tuple[RuntimeRequirement, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.project_identity, str) or not self.project_identity:
            raise ValueError("runtime requirement project identity must not be empty")
        if not isinstance(self.requirements, tuple):
            raise TypeError("runtime requirements must be a tuple")
        keys = tuple(item.grant_selection_key for item in self.requirements)
        if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
            raise ValueError("runtime requirements must be sorted and unique")
        if any(item.project_identity != self.project_identity for item in self.requirements):
            raise ValueError("runtime requirement project identity mismatch")

    @classmethod
    def for_authorized_closure(
        cls,
        authorized: AuthorizedRecipe,
        *,
        project_identity: str,
    ) -> RuntimeRequirementIndex:
        return cls.for_authorized_closures(
            (authorized,),
            project_identity=project_identity,
        )

    @classmethod
    def for_authorized_closures(
        cls,
        authorized_closures: tuple[AuthorizedRecipe, ...],
        *,
        project_identity: str,
    ) -> RuntimeRequirementIndex:
        requirements: dict[str, RuntimeRequirement] = {}
        for authorized in authorized_closures:
            derived = cls._for_recipe_documents(
                tuple((file.path, file.bytes) for file in authorized.files),
                definition_digest=authorized.definition_sha256,
                project_identity=project_identity,
            )
            for requirement in derived.requirements:
                _merge_requirement(requirements, requirement)
        return cls(
            project_identity=project_identity,
            requirements=tuple(requirements[key] for key in sorted(requirements)),
        )

    @classmethod
    def _for_recipe_documents(
        cls,
        documents: tuple[tuple[str, bytes], ...],
        *,
        definition_digest: str,
        project_identity: str,
    ) -> RuntimeRequirementIndex:
        """Derive inventory from already verified immutable recipe bytes."""

        requirements: dict[str, RuntimeRequirement] = {}
        for logical_path, encoded in documents:
            for descriptor in _runtime_descriptors(encoded):
                assert descriptor.runner is not None
                if descriptor.runner.selector not in {"codex", "pinned"}:
                    raise ValueError(
                        "runtime requirement has an unsupported runner selector"
                    )
                capabilities = tuple(
                    sorted(descriptor.runner.required_capabilities)
                )
                authorities = ("os_user_execution",)
                selection_key = grant_selection_key(
                    project_identity=project_identity,
                    definition_digest=definition_digest,
                    protected_descriptor_digest=descriptor.digest,
                    runner_selector=descriptor.runner.selector,
                    required_capabilities=capabilities,
                    required_authorities=authorities,
                )
                requirement = RuntimeRequirement(
                    grant_selection_key=selection_key,
                    project_identity=project_identity,
                    definition_digest=definition_digest,
                    protected_descriptor_digest=descriptor.digest,
                    runner_selector=descriptor.runner.selector,
                    required_capabilities=capabilities,
                    required_authorities=authorities,
                    uses=((logical_path, descriptor.logical_id),),
                )
                _merge_requirement(requirements, requirement)
        return cls(
            project_identity=project_identity,
            requirements=tuple(requirements[key] for key in sorted(requirements)),
        )

    def bind(self, snapshot: OwnerRuntimeSnapshot) -> _BoundRuntimeRequirementIndex:
        """Return a deterministic private view over one normalized snapshot."""

        if not isinstance(snapshot, OwnerRuntimeSnapshot):
            raise TypeError("runtime requirement binding requires an owner snapshot")
        grants = {grant.grant_selection_key: grant for grant in snapshot.grants}
        entries: list[tuple[RuntimeRequirement, str]] = []
        for requirement in self.requirements:
            if requirement.runner_selector == "codex":
                binding_digest = snapshot.codex.binding_digest
            elif requirement.runner_selector == "pinned":
                binding_digest = snapshot.pinned.binding_digest
            else:  # construction and static derivation already reject this
                raise ValueError("runtime requirement has an unsupported runner selector")
            digest = requirement_digest(
                grant_selection_key=requirement.grant_selection_key,
                runner_binding_digest=binding_digest,
                config_generation=snapshot.config_generation,
            )
            grant = grants.get(requirement.grant_selection_key)
            if grant is not None and grant.requirement_digest != digest:
                raise ValueError(
                    "owner runtime grant requirement digest does not match binding"
                )
            entries.append((requirement, digest))
        return _BoundRuntimeRequirementIndex(snapshot, tuple(entries))

    def listing_document(self) -> dict[str, object]:
        """Return the canonical product-visible static inventory document."""

        return {
            "schema": "lockstep.runtime-requirements/v1",
            "project_identity": self.project_identity,
            "requirements": [
                {
                    "grant_selection_key": requirement.grant_selection_key,
                    "definition_digest": requirement.definition_digest,
                    "protected_descriptor_digest": (
                        requirement.protected_descriptor_digest
                    ),
                    "runner_selector": requirement.runner_selector,
                    "required_capabilities": list(
                        requirement.required_capabilities
                    ),
                    "required_authorities": list(requirement.required_authorities),
                    "uses": [
                        {"logical_file": logical_file, "logical_id": logical_id}
                        for logical_file, logical_id in requirement.uses
                    ],
                }
                for requirement in self.requirements
            ],
        }


@dataclass(frozen=True, slots=True)
class RuntimeProvisioningInventory:
    """Closed union of exact per-project indexes for one owner snapshot."""

    project_identities: tuple[str, ...]
    requirements: tuple[RuntimeRequirement, ...]

    def __post_init__(self) -> None:
        if (
            not self.project_identities
            or self.project_identities != tuple(sorted(set(self.project_identities)))
        ):
            raise ValueError("runtime provisioning projects must be sorted and unique")
        keys = tuple(item.grant_selection_key for item in self.requirements)
        if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
            raise ValueError("runtime provisioning requirements must be sorted and unique")
        projects = set(self.project_identities)
        if any(item.project_identity not in projects for item in self.requirements):
            raise ValueError("runtime provisioning requirement project is absent")

    @classmethod
    def combine(
        cls, indexes: tuple[RuntimeRequirementIndex, ...]
    ) -> RuntimeProvisioningInventory:
        if not indexes or any(
            not isinstance(index, RuntimeRequirementIndex) for index in indexes
        ):
            raise TypeError("runtime provisioning requires per-project indexes")
        requirements: dict[str, RuntimeRequirement] = {}
        for index in indexes:
            for requirement in index.requirements:
                _merge_requirement(requirements, requirement)
        return cls(
            tuple(sorted({index.project_identity for index in indexes})),
            tuple(requirements[key] for key in sorted(requirements)),
        )


@dataclass(frozen=True, slots=True)
class _BoundRuntimeRequirementIndex:
    snapshot: OwnerRuntimeSnapshot
    entries: tuple[tuple[RuntimeRequirement, str], ...]

    def __post_init__(self) -> None:
        keys = tuple(item.grant_selection_key for item, _digest in self.entries)
        if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
            raise ValueError("bound runtime requirements must be sorted and unique")
        for _requirement, digest in self.entries:
            _lower_hex(digest, label="bound runtime requirement digest")
