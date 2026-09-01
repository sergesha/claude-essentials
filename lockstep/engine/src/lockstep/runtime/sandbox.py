"""Implementation-neutral sandbox contract used by managed runners."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


@dataclass(frozen=True)
class SandboxPolicy:
    read_roots: tuple[Path, ...]
    write_root: Path
    temp_root: Path
    argv: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str], ...]
    denied_vcs_roots: tuple[Path, ...] = ()
    network_allowed: bool = False
    close_fds: bool = True
    inherited_fds: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(part, str) or not part for part in self.argv):
            raise ValueError("sandbox policy requires a non-empty argv array")
        seen: set[str] = set()
        for key, value in self.environment:
            if not key or "=" in key or "\x00" in key or "\x00" in value or key in seen:
                raise ValueError("sandbox policy environment must be a unique sanitized mapping")
            seen.add(key)
        if not self.close_fds or self.inherited_fds:
            raise ValueError("managed sandbox must close all inherited file descriptors")
        object.__setattr__(self, "environment", tuple(sorted(self.environment)))

    @property
    def digest(self) -> str:
        canonical = {
            "read_roots": [str(path.resolve()) for path in self.read_roots],
            "write_root": str(self.write_root.resolve()),
            "temp_root": str(self.temp_root.resolve()),
            "denied_vcs_roots": [str(path.resolve()) for path in self.denied_vcs_roots],
            "network_allowed": self.network_allowed,
            "argv": list(self.argv),
            "cwd": str(self.cwd.resolve()),
            "environment": list(self.environment),
            "close_fds": self.close_fds,
            "inherited_fds": list(self.inherited_fds),
        }
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class SandboxAttestation:
    provider_id: str
    provider_version: str
    policy_digest: str
    denies_outside_workspace: bool
    denies_vcs_write: bool
    denies_symlink_escape: bool
    evidence_scope: Literal["requested_mechanics", "enforced_boundary"] = (
        "enforced_boundary"
    )


@dataclass(frozen=True)
class ProcessHandle:
    argv: tuple[str, ...]
    stdin: bytes
    policy_digest: str


class SandboxProvider(Protocol):
    def preflight(self, policy: SandboxPolicy) -> SandboxAttestation: ...

    def spawn(
        self, policy: SandboxPolicy, argv: Sequence[str], *, stdin: bytes = b""
    ) -> ProcessHandle: ...


class SandboxAttestor(Protocol):
    def preflight(self, policy: SandboxPolicy) -> SandboxAttestation: ...


def spawn_verified(
    provider: SandboxProvider,
    policy: SandboxPolicy,
    argv: Sequence[str],
    *,
    stdin: bytes = b"",
) -> ProcessHandle:
    """Preflight and verify a provider before any managed process starts."""
    if tuple(argv) != policy.argv:
        raise ValueError("sandbox argv does not match the attested policy")
    verify_attestation(policy, provider.preflight(policy))
    handle = provider.spawn(policy, argv, stdin=stdin)
    if handle.argv != policy.argv or handle.policy_digest != policy.digest:
        raise ValueError("sandbox process handle does not match the attested policy")
    return handle


def verify_attestation(
    policy: SandboxPolicy,
    attestation: SandboxAttestation,
    *,
    require_enforced: bool = True,
) -> SandboxAttestation:
    """Validate one adapter attestation without crossing a process boundary."""

    claims_enforcement = (
        attestation.denies_outside_workspace
        and attestation.denies_vcs_write
        and attestation.denies_symlink_escape
    )
    any_enforcement_claim = (
        attestation.denies_outside_workspace
        or attestation.denies_vcs_write
        or attestation.denies_symlink_escape
    )
    if attestation.policy_digest != policy.digest:
        raise ValueError("sandbox attestation does not satisfy the required policy")
    if attestation.evidence_scope == "enforced_boundary":
        if not claims_enforcement:
            raise ValueError("enforced sandbox attestation is incomplete")
    elif attestation.evidence_scope == "requested_mechanics":
        if any_enforcement_claim:
            raise ValueError("requested mechanics may not claim enforced confinement")
    else:
        raise ValueError("unknown sandbox attestation evidence scope")
    if require_enforced and attestation.evidence_scope != "enforced_boundary":
        raise ValueError("an enforced sandbox boundary is required")
    return attestation


class FakeSandboxProvider:
    """Test-only provider: records an argv array and never invokes a process."""

    def preflight(self, policy: SandboxPolicy) -> SandboxAttestation:
        return SandboxAttestation(
            provider_id="fake",
            provider_version="1",
            policy_digest=policy.digest,
            denies_outside_workspace=True,
            denies_vcs_write=True,
            denies_symlink_escape=True,
        )

    def spawn(
        self, policy: SandboxPolicy, argv: Sequence[str], *, stdin: bytes = b""
    ) -> ProcessHandle:
        if not argv or any(not isinstance(part, str) for part in argv):
            raise ValueError("sandbox argv must be a non-empty string sequence")
        if tuple(argv) != policy.argv:
            raise ValueError("sandbox argv does not match the attested policy")
        return ProcessHandle(tuple(argv), bytes(stdin), policy.digest)
