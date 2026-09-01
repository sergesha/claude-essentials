"""Pure R1a owner-policy values and deterministic bound inventory."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from lockstep.runtime.effects import owner_policy


def _binding(*, selector: str) -> owner_policy._RuntimeBindingFacts:
    return owner_policy._RuntimeBindingFacts(
        executable="/bin/codex",
        model="model",
        cli_version="version",
        permission_profile=(
            ("approval", "never"),
            ("sandbox", "workspace-write"),
        ),
        codex_home=f"/owner/{selector}",
        environment=(
            ("LANG", "C.UTF-8"),
            ("LC_ALL", "C.UTF-8"),
            ("PATH", "/usr/bin:/bin"),
            ("TMPDIR", "/owner/tmp"),
        ),
        credential_identity_digest="a" * 64 if selector == "codex" else None,
        binding_digest=("b" if selector == "codex" else "c") * 64,
        pinned_permission_profile=(
            None if selector == "codex" else "owner-profile"
        ),
    )


def _requirement(selector: str, marker: str) -> owner_policy.RuntimeRequirement:
    stable = {
        "project_identity": "/project",
        "definition_digest": marker * 64,
        "protected_descriptor_digest": ("d" if marker == "1" else "e") * 64,
        "runner_selector": selector,
        "required_capabilities": ("workspace",),
        "required_authorities": ("os_user_execution",),
    }
    return owner_policy.RuntimeRequirement(
        grant_selection_key=owner_policy.grant_selection_key(**stable),
        **stable,
        uses=((f"{selector}.recipe.yaml", "work"),),
    )


def _grant(
    requirement: owner_policy.RuntimeRequirement,
    *,
    binding_digest: str,
    config_generation: int = 3,
    policy_generation: int = 4,
) -> owner_policy.OwnerRuntimeGrant:
    return owner_policy.OwnerRuntimeGrant(
        grant_selection_key=requirement.grant_selection_key,
        requirement_digest=owner_policy.requirement_digest(
            grant_selection_key=requirement.grant_selection_key,
            runner_binding_digest=binding_digest,
            config_generation=config_generation,
        ),
        authority="os_user_execution",
        grant_generation=1,
        policy_generation=policy_generation,
        config_generation=config_generation,
    )


def test_owner_runtime_grant_has_only_frozen_authority_facts() -> None:
    requirement = _requirement("codex", "1")
    grant = _grant(requirement, binding_digest="b" * 64)

    assert tuple(field.name for field in fields(grant)) == (
        "grant_selection_key",
        "requirement_digest",
        "authority",
        "grant_generation",
        "policy_generation",
        "config_generation",
    )
    assert not hasattr(grant, "__dict__")
    with pytest.raises(FrozenInstanceError):
        grant.authority = "ambient"


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"grant_selection_key": "A" * 64}, "selection key"),
        ({"requirement_digest": "short"}, "requirement digest"),
        ({"authority": "ambient"}, "authority"),
        ({"grant_generation": True}, "grant generation"),
        ({"grant_generation": 0}, "grant generation"),
        ({"policy_generation": False}, "policy generation"),
        ({"config_generation": 1.0}, "config generation"),
    ],
)
def test_owner_runtime_grant_rejects_invalid_frozen_facts(
    changes: dict[str, object], expected: str
) -> None:
    requirement = _requirement("codex", "1")
    values = {
        "grant_selection_key": requirement.grant_selection_key,
        "requirement_digest": "f" * 64,
        "authority": "os_user_execution",
        "grant_generation": 1,
        "policy_generation": 0,
        "config_generation": 0,
    }

    with pytest.raises((TypeError, ValueError), match=expected):
        owner_policy.OwnerRuntimeGrant(**{**values, **changes})


def test_owner_runtime_snapshot_is_exact_frozen_normalized_value() -> None:
    codex_requirement = _requirement("codex", "1")
    grant = _grant(codex_requirement, binding_digest="b" * 64)

    snapshot = owner_policy.OwnerRuntimeSnapshot(
        schema="lockstep.runtime-owner/v1",
        config_generation=3,
        policy_generation=4,
        codex=_binding(selector="codex"),
        pinned=_binding(selector="pinned"),
        grants=(grant,),
    )

    assert tuple(field.name for field in fields(snapshot)) == (
        "schema",
        "config_generation",
        "policy_generation",
        "codex",
        "pinned",
        "grants",
    )
    assert not hasattr(snapshot, "__dict__")


def test_owner_runtime_snapshot_rejects_stale_grant_generations() -> None:
    requirement = _requirement("codex", "1")
    stale = _grant(
        requirement,
        binding_digest="b" * 64,
        config_generation=2,
    )

    with pytest.raises(ValueError, match="snapshot generations"):
        owner_policy.OwnerRuntimeSnapshot(
            schema="lockstep.runtime-owner/v1",
            config_generation=3,
            policy_generation=4,
            codex=_binding(selector="codex"),
            pinned=_binding(selector="pinned"),
            grants=(stale,),
        )


def test_requirement_index_bind_is_pure_deterministic_and_grant_aware() -> None:
    codex_requirement = _requirement("codex", "1")
    pinned_requirement = _requirement("pinned", "2")
    grant = _grant(codex_requirement, binding_digest="b" * 64)
    snapshot = owner_policy.OwnerRuntimeSnapshot(
        schema="lockstep.runtime-owner/v1",
        config_generation=3,
        policy_generation=4,
        codex=_binding(selector="codex"),
        pinned=_binding(selector="pinned"),
        grants=(grant,),
    )
    index = owner_policy.RuntimeRequirementIndex(
        project_identity="/project",
        requirements=tuple(
            sorted(
                (pinned_requirement, codex_requirement),
                key=lambda item: item.grant_selection_key,
            )
        ),
    )

    bound = index.bind(snapshot)

    assert tuple(item.grant_selection_key for item, _digest in bound.entries) == tuple(
        sorted(
            (
                codex_requirement.grant_selection_key,
                pinned_requirement.grant_selection_key,
            )
        )
    )
    assert dict(
        (item.grant_selection_key, digest) for item, digest in bound.entries
    ) == {
        codex_requirement.grant_selection_key: grant.requirement_digest,
        pinned_requirement.grant_selection_key: owner_policy.requirement_digest(
            grant_selection_key=pinned_requirement.grant_selection_key,
            runner_binding_digest="c" * 64,
            config_generation=3,
        ),
    }


def test_requirement_index_bind_rejects_grant_for_old_binding() -> None:
    requirement = _requirement("codex", "1")
    stale = _grant(requirement, binding_digest="9" * 64)
    snapshot = owner_policy.OwnerRuntimeSnapshot(
        schema="lockstep.runtime-owner/v1",
        config_generation=3,
        policy_generation=4,
        codex=_binding(selector="codex"),
        pinned=_binding(selector="pinned"),
        grants=(stale,),
    )
    index = owner_policy.RuntimeRequirementIndex(
        project_identity="/project",
        requirements=(requirement,),
    )

    with pytest.raises(ValueError, match="requirement digest"):
        index.bind(snapshot)
