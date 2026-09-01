"""Owner runtime admission decisions over exact requirements and grants."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from lockstep.runtime.effects._owner_policy_requirements import (
    RuntimeRequirement,
    RuntimeRequirementIndex,
    _BoundRuntimeRequirementIndex as _BoundRuntimeRequirementIndex,
)
from lockstep.runtime.effects._owner_policy_values import (
    OwnerRuntimeGrant,
    OwnerRuntimeSnapshot,
    _RuntimeBindingFacts,
    _lower_hex,
)


class _RuntimeAdmissionChanged(RuntimeError):
    """The owner policy no longer matches an admitted immutable decision."""


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

    def assert_current(self, state_dir: Path) -> AbstractContextManager[None]:
        """Hold the owner snapshot boundary while this decision is admitted."""

        from lockstep.runtime.effects.owner_snapshot_store import (
            hold_runtime_snapshot_current,
        )

        return hold_runtime_snapshot_current(
            state_dir,
            expected_digest=self.snapshot_digest,
            expected_snapshot=self.snapshot,
        )


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
