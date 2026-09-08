"""Closed local runner identities and honest requested-mechanics attestations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from lockstep.runtime.providers._codex_support import _canonical, _capture_executable
from lockstep.runtime.sandbox import SandboxAttestation, SandboxPolicy


class RunnerSelector(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    PINNED = "pinned"


class PinnedBackend(StrEnum):
    CODEX_SANDBOX = "codex-sandbox"
    DIRECT_LOCAL = "direct-local"


class AttemptProvider(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    DIRECT_LOCAL = "direct-local"


class AttemptInstallation(Protocol):
    @property
    def digest(self) -> str: ...

    @property
    def environment(self) -> tuple[tuple[str, str], ...]: ...

    def revalidate(self) -> None: ...


def local_environment(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or set(value) != {
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
    }:
        raise ValueError(
            "runtime environment must define exactly PATH, LANG, LC_ALL, and TMPDIR"
        )
    if any(
        not isinstance(item, str) or not item or "\x00" in item
        for item in value.values()
    ):
        raise ValueError("runtime environment contains an invalid value")
    if not Path(value["TMPDIR"]).is_absolute():
        raise ValueError("runtime TMPDIR must be absolute")
    return tuple(sorted(value.items()))


@dataclass(frozen=True)
class ExecutableIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str

    @classmethod
    def capture(cls, path: Path) -> ExecutableIdentity:
        info, digest = _capture_executable(path)
        return cls(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, digest)


LaunchDetails = tuple[
    Path,
    tuple[str, ...],
    tuple[tuple[str, str], ...],
    Path | None,
    str | None,
    ExecutableIdentity | None,
]


@dataclass(frozen=True)
class DirectInstallationBinding:
    environment: tuple[tuple[str, str], ...]
    digest: str

    @classmethod
    def capture(cls, *, environment: object) -> DirectInstallationBinding:
        captured = local_environment(environment)
        digest = hashlib.sha256(
            _canonical(
                {
                    "schema": "lockstep.direct-local-binding/v1",
                    "backend": PinnedBackend.DIRECT_LOCAL,
                    "environment": [list(item) for item in captured],
                    "execution_authority": "os_user_execution",
                    "deployment_profile": "local_unsandboxed",
                }
            )
        ).hexdigest()
        return cls(captured, digest)

    def revalidate(self) -> None:
        if self != self.capture(environment=dict(self.environment)):
            raise ValueError("direct-local binding changed")


@dataclass(frozen=True)
class ClaudeInstallationBinding:
    executable_path: Path
    executable_identity: ExecutableIdentity
    model: str
    home: Path
    environment: tuple[tuple[str, str], ...]
    digest: str

    @classmethod
    def capture(
        cls,
        *,
        executable: str | Path,
        model: str,
        home: str | Path,
        environment: object,
    ) -> ClaudeInstallationBinding:
        if not Path(executable).is_absolute() or not Path(home).is_absolute():
            raise ValueError("Claude executable and home paths must be absolute")
        if not isinstance(model, str) or not model or "\x00" in model:
            raise ValueError("Claude model must be explicit")
        path = Path(executable).resolve(strict=True)
        native_home = Path(home).resolve(strict=True)
        if not native_home.is_dir():
            raise ValueError("Claude home must be an existing native user home")
        identity = ExecutableIdentity.capture(path)
        captured = local_environment(environment)
        digest = hashlib.sha256(
            _canonical(
                {
                    "schema": "lockstep.claude-installation/v1",
                    "executable": str(path),
                    "identity": identity.__dict__,
                    "model": model,
                    "home": str(native_home),
                    "environment": [list(item) for item in captured],
                    "authentication": "ambient-os-user",
                    "deployment_profile": "local_unsandboxed",
                }
            )
        ).hexdigest()
        return cls(path, identity, model, native_home, captured, digest)

    def revalidate(self) -> None:
        if self != self.capture(
            executable=self.executable_path,
            model=self.model,
            home=self.home,
            environment=dict(self.environment),
        ):
            raise ValueError("Claude installation binding changed")


class LocalMechanicsAttestor:
    def preflight(self, policy: SandboxPolicy) -> SandboxAttestation:
        return SandboxAttestation(
            provider_id="local-unsandboxed",
            provider_version="1",
            policy_digest=policy.digest,
            denies_outside_workspace=False,
            denies_vcs_write=False,
            denies_symlink_escape=False,
            evidence_scope="requested_mechanics",
        )
