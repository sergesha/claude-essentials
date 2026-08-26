"""Static owner-selected runtime requirements and grants."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Literal

from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.effects.models import EffectDescriptor
from lockstep.runtime.effects.owner_policy_ingress import (
    parse_runtime_provision_documents,
)

if TYPE_CHECKING:
    from lockstep.recipe.authority import AuthorizedRecipe
    from lockstep.runtime.effects.owner_provisioning import (
        provision_runtime_snapshot,
        validate_runtime_provision_inputs,
    )


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


def _lower_hex(value: object, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _exact_generation(value: object, *, label: str, positive: bool = False) -> int:
    if type(value) is not int or (positive and value <= 0):
        suffix = " a positive integer" if positive else " an integer"
        raise TypeError(f"{label} must be{suffix}")
    return value


@dataclass(frozen=True, slots=True)
class _RuntimeBindingFacts:
    """Private immutable carrier for the normalized captured binding facts."""

    executable: str
    model: str
    cli_version: str
    permission_profile: tuple[tuple[str, str], ...]
    codex_home: str
    environment: tuple[tuple[str, str], ...]
    credential_identity_digest: str | None
    binding_digest: str
    pinned_permission_profile: str | None

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value or "\x00" in value
            for value in (
                self.executable,
                self.model,
                self.cli_version,
                self.codex_home,
            )
        ):
            raise ValueError("runtime binding identities must be non-empty strings")
        if not Path(self.executable).is_absolute() or not Path(
            self.codex_home
        ).is_absolute():
            raise ValueError("runtime binding paths must be absolute")
        if self.permission_profile != (
            ("approval", "never"),
            ("sandbox", "workspace-write"),
        ):
            raise ValueError("runtime binding permission profile is not normalized")
        if not isinstance(self.environment, tuple) or self.environment != tuple(
            sorted(self.environment)
        ):
            raise ValueError("runtime binding environment is not normalized")
        environment = dict(self.environment)
        if len(self.environment) != 4 or set(environment) != {
            "PATH",
            "LANG",
            "LC_ALL",
            "TMPDIR",
        } or any(
            not isinstance(value, str) or not value or "\x00" in value
            for value in environment.values()
        ):
            raise ValueError("runtime binding environment is invalid")
        if not Path(environment["TMPDIR"]).is_absolute():
            raise ValueError("runtime binding TMPDIR must be absolute")
        if self.credential_identity_digest is not None:
            _lower_hex(
                self.credential_identity_digest,
                label="runtime binding credential identity digest",
            )
        _lower_hex(self.binding_digest, label="runtime binding digest")
        if self.pinned_permission_profile is not None and (
            not isinstance(self.pinned_permission_profile, str)
            or not self.pinned_permission_profile
            or "\x00" in self.pinned_permission_profile
            or len(self.pinned_permission_profile.encode("utf-8")) > 4096
        ):
            raise ValueError("pinned permission profile must be owner-selected")


@dataclass(frozen=True, slots=True)
class OwnerRuntimeGrant:
    """One owner-selected grant for a current exact requirement."""

    grant_selection_key: str
    requirement_digest: str
    authority: Literal["os_user_execution"]
    grant_generation: int
    policy_generation: int
    config_generation: int

    def __post_init__(self) -> None:
        _lower_hex(self.grant_selection_key, label="grant selection key")
        _lower_hex(self.requirement_digest, label="requirement digest")
        if self.authority != "os_user_execution":
            raise ValueError("owner runtime grant authority is invalid")
        _exact_generation(
            self.grant_generation,
            label="grant generation",
            positive=True,
        )
        _exact_generation(self.policy_generation, label="policy generation")
        _exact_generation(self.config_generation, label="config generation")


@dataclass(frozen=True, slots=True)
class OwnerRuntimeSnapshot:
    """Normalized owner runtime configuration and complete grant set."""

    schema: str
    config_generation: int
    policy_generation: int
    codex: _RuntimeBindingFacts
    pinned: _RuntimeBindingFacts
    grants: tuple[OwnerRuntimeGrant, ...]

    def __post_init__(self) -> None:
        if self.schema != "lockstep.runtime-owner/v1":
            raise ValueError("owner runtime snapshot schema is invalid")
        _exact_generation(self.config_generation, label="config generation")
        _exact_generation(self.policy_generation, label="policy generation")
        if not isinstance(self.codex, _RuntimeBindingFacts) or not isinstance(
            self.pinned, _RuntimeBindingFacts
        ):
            raise TypeError("owner runtime snapshot bindings are invalid")
        if self.codex.pinned_permission_profile is not None:
            raise ValueError("owner runtime codex binding cannot be pinned")
        if self.pinned.pinned_permission_profile is None:
            raise ValueError("owner runtime pinned binding requires a permission profile")
        if self.codex.credential_identity_digest is None:
            raise ValueError("owner runtime codex binding requires credentials")
        if self.pinned.credential_identity_digest is not None:
            raise ValueError("owner runtime pinned binding must be credential-free")
        if Path(self.codex.codex_home).resolve() == Path(
            self.pinned.codex_home
        ).resolve():
            raise ValueError("owner runtime Codex homes must differ")
        if not isinstance(self.grants, tuple):
            raise TypeError("owner runtime grants must be a tuple")
        keys = tuple(grant.grant_selection_key for grant in self.grants)
        if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
            raise ValueError("owner runtime grants must be sorted and unique")
        if any(
            grant.config_generation != self.config_generation
            or grant.policy_generation != self.policy_generation
            for grant in self.grants
        ):
            raise ValueError("owner runtime grant does not match snapshot generations")


def __getattr__(name: str):
    """Resolve compatibility provisioning exports without an import cycle."""

    if name == "provision_runtime_snapshot":
        from lockstep.runtime.effects.owner_provisioning import (
            provision_runtime_snapshot,
        )

        return provision_runtime_snapshot
    if name == "validate_runtime_provision_inputs":
        from lockstep.runtime.effects.owner_provisioning import (
            validate_runtime_provision_inputs,
        )

        return validate_runtime_provision_inputs
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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


@dataclass(frozen=True, slots=True)
class RuntimeAdmissionDecision:
    """Immutable write-free proof of one exact static runtime admission."""

    snapshot_digest: str
    snapshot: OwnerRuntimeSnapshot
    requirements: tuple[
        tuple[RuntimeRequirement, str, OwnerRuntimeGrant], ...
    ]

    def __post_init__(self) -> None:
        _lower_hex(self.snapshot_digest, label="owner runtime snapshot digest")
        if not isinstance(self.snapshot, OwnerRuntimeSnapshot):
            raise TypeError("runtime admission snapshot is invalid")
        keys = tuple(
            requirement.grant_selection_key
            for requirement, _digest, _grant in self.requirements
        )
        if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
            raise ValueError("runtime admission requirements must be sorted and unique")
        for requirement, digest, grant in self.requirements:
            _lower_hex(digest, label="runtime admission requirement digest")
            if (
                grant.grant_selection_key != requirement.grant_selection_key
                or grant.requirement_digest != digest
            ):
                raise ValueError("runtime admission grant does not match requirement")


@dataclass(frozen=True, slots=True)
class OwnerRuntimeAuthority:
    """Fail-closed static runtime-policy decision boundary."""

    snapshot_digest: str
    snapshot: OwnerRuntimeSnapshot
    codex_binding: _RuntimeBindingFacts
    pinned_binding: _RuntimeBindingFacts

    def __post_init__(self) -> None:
        _lower_hex(self.snapshot_digest, label="owner runtime snapshot digest")
        if self.codex_binding != self.snapshot.codex:
            raise ValueError("owner runtime Codex binding changed after provisioning")
        if self.pinned_binding != self.snapshot.pinned:
            raise ValueError("owner runtime pinned binding changed after provisioning")

    def preflight(self, index: RuntimeRequirementIndex) -> RuntimeAdmissionDecision:
        """Authorize every bound entry without resolving or starting a runner."""

        bound = index.bind(self.snapshot)
        grants = {
            grant.grant_selection_key: grant for grant in self.snapshot.grants
        }
        admitted = []
        for requirement, digest in bound.entries:
            grant = grants.get(requirement.grant_selection_key)
            if grant is None or grant.requirement_digest != digest:
                raise ValueError("exact owner runtime grant is unavailable")
            admitted.append((requirement, digest, grant))
        return RuntimeAdmissionDecision(
            snapshot_digest=self.snapshot_digest,
            snapshot=self.snapshot,
            requirements=tuple(admitted),
        )
