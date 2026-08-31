"""Closed owner-selected runtime policy values."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal


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
