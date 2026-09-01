"""Closed Codex installation, policy, and attestation values."""

from __future__ import annotations
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from threading import RLock
from typing import Literal
from lockstep.runtime.owner_state import verify_owner_directory, verify_owner_file
from lockstep.runtime.payload_limits import bounded_json
from lockstep.runtime.sandbox import SandboxAttestation, SandboxPolicy


class CodexProviderError(RuntimeError):
    """A trusted Codex launch commitment cannot be proven current."""


@dataclass(frozen=True)
class CodexCaptureLimits:
    max_stdout_bytes: int = 16 * 1024 * 1024
    max_stderr_bytes: int = 1024 * 1024
    max_json_records: int = 10_000
    max_result_bytes: int = 1024 * 1024
    max_retained_attempts: int = 1_000

    def __post_init__(self) -> None:
        if (
            min(
                self.max_stdout_bytes,
                self.max_stderr_bytes,
                self.max_json_records,
                self.max_result_bytes,
                self.max_retained_attempts,
            )
            <= 0
        ):
            raise ValueError("Codex capture limits must be positive")


def _canonical(value: object) -> bytes:
    admitted = bounded_json(value, label="Codex launch commitment")
    return json.dumps(
        admitted,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise CodexProviderError("Codex executable is not a regular file")
        identity = _stat_identity(info)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if _stat_identity(os.fstat(descriptor)) != identity:
            raise CodexProviderError("bound file changed while hashing")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def _capture_executable(path: Path) -> tuple[os.stat_result, str]:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o111:
            raise CodexProviderError(
                "Codex executable must be an executable regular file"
            )
        identity = _stat_identity(before)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if _stat_identity(os.fstat(descriptor)) != identity:
            raise CodexProviderError("Codex executable identity changed")
    finally:
        os.close(descriptor)
    try:
        current = path.stat()
    except OSError as exc:
        raise CodexProviderError("Codex executable identity changed") from exc
    if _stat_identity(current) != identity:
        raise CodexProviderError("Codex executable identity changed")
    return before, digest.hexdigest()


def _credential_identity(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    verify_owner_file(path)
    before = path.lstat()
    values = {
        "schema": "lockstep.codex-credential/v1",
        "device": before.st_dev,
        "inode": before.st_ino,
        "mode": before.st_mode,
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "sha256": _sha256_file(path),
        "audience": "openai-codex",
    }
    if _stat_identity(path.lstat()) != _stat_identity(before):
        raise CodexProviderError("Codex credential changed while binding")
    return hashlib.sha256(_canonical(values)).hexdigest()


def _managed_argv(
    executable: Path,
    *,
    model: str,
    workspace: Path,
    permission_profile: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    """Construct the sole Codex-specific launch authority from bound values."""

    if not executable.is_absolute() or not workspace.is_absolute():
        raise CodexProviderError(
            "managed Codex executable and workspace must be absolute"
        )
    if not model or "\x00" in model:
        raise CodexProviderError("managed Codex model must be explicit")
    permissions = dict(permission_profile)
    return (
        str(executable),
        "--ask-for-approval",
        permissions["approval"],
        "exec",
        "--json",
        "--sandbox",
        permissions["sandbox"],
        "--model",
        model,
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
        str(workspace),
        "-",
    )


@dataclass(frozen=True)
class CodexInstallationBinding:
    executable_path: Path
    executable_device: int
    executable_inode: int
    executable_size: int
    executable_mtime_ns: int
    executable_sha256: str
    model: str
    cli_version: str
    permission_profile: tuple[tuple[str, str], ...]
    codex_home: Path
    credential_identity_digest: str | None
    environment: tuple[tuple[str, str], ...]
    deployment_profile: Literal["local_unsandboxed"]
    digest: str

    @classmethod
    def capture(
        cls,
        *,
        executable: str | Path,
        model: str,
        cli_version: str,
        permission_profile: Mapping[str, object],
        codex_home: str | Path,
        environment: Mapping[str, str],
    ) -> CodexInstallationBinding:
        supplied = Path(executable)
        if not supplied.is_absolute():
            raise CodexProviderError("Codex executable path must be absolute")
        resolved = supplied.resolve(strict=True)
        info, executable_sha256 = _capture_executable(resolved)
        if not model or not cli_version:
            raise CodexProviderError("Codex model and CLI version must be explicit")
        if (
            set(permission_profile) != {"sandbox", "approval"}
            or permission_profile.get("sandbox") != "workspace-write"
            or permission_profile.get("approval") != "never"
        ):
            raise CodexProviderError(
                "Codex permission profile must exactly require workspace-write and never approval"
            )
        captured_profile = tuple(
            sorted((key, str(value)) for key, value in permission_profile.items())
        )
        supplied_home = Path(codex_home)
        if not supplied_home.is_absolute() or supplied_home.is_symlink():
            raise CodexProviderError(
                "CODEX_HOME must be an absolute non-symlink directory"
            )
        home = supplied_home.resolve(strict=True)
        if not home.is_dir():
            raise CodexProviderError("CODEX_HOME must be an owner-selected directory")
        verify_owner_directory(home)
        for entry in home.iterdir():
            if entry.name != "auth.json":
                raise CodexProviderError(
                    "managed CODEX_HOME may contain only the owner auth.json credential"
                )
            verify_owner_file(entry)
        credential_identity_digest = _credential_identity(home / "auth.json")
        allowed_environment = {"PATH", "LANG", "LC_ALL", "TMPDIR"}
        if set(environment) != allowed_environment:
            raise CodexProviderError(
                "Codex environment must define exactly PATH, LANG, LC_ALL, and TMPDIR"
            )
        checked_environment: list[tuple[str, str]] = []
        for key, value in environment.items():
            if not isinstance(value, str) or not value or "\x00" in value:
                raise CodexProviderError("Codex environment contains an invalid value")
            checked_environment.append((key, value))
        values = {
            "schema": "lockstep.codex-installation/v1",
            "executable_path": str(resolved),
            "executable_device": info.st_dev,
            "executable_inode": info.st_ino,
            "executable_size": info.st_size,
            "executable_mtime_ns": info.st_mtime_ns,
            "executable_sha256": executable_sha256,
            "model": model,
            "cli_version": cli_version,
            "permission_profile": [list(item) for item in captured_profile],
            "codex_home": str(home),
            "credential_identity_digest": credential_identity_digest,
            "environment": [list(item) for item in sorted(checked_environment)],
            "deployment_profile": "local_unsandboxed",
        }
        return cls(
            executable_path=resolved,
            executable_device=info.st_dev,
            executable_inode=info.st_ino,
            executable_size=info.st_size,
            executable_mtime_ns=info.st_mtime_ns,
            executable_sha256=values["executable_sha256"],
            model=model,
            cli_version=cli_version,
            permission_profile=captured_profile,
            codex_home=home,
            credential_identity_digest=credential_identity_digest,
            environment=tuple(sorted(checked_environment)),
            deployment_profile="local_unsandboxed",
            digest=hashlib.sha256(_canonical(values)).hexdigest(),
        )

    def revalidate(self) -> None:
        info, executable_sha256 = _capture_executable(self.executable_path)
        identity = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            executable_sha256,
        )
        expected = (
            self.executable_device,
            self.executable_inode,
            self.executable_size,
            self.executable_mtime_ns,
            self.executable_sha256,
        )
        if identity != expected:
            raise CodexProviderError("Codex executable identity changed")
        if (
            _credential_identity(self.codex_home / "auth.json")
            != self.credential_identity_digest
        ):
            raise CodexProviderError("Codex credential identity changed")


class CodexLaunchDecisionGate:
    """Owner configuration fence used only at the provider commitment point."""

    def __init__(self, binding_digest: str, *, generation: int) -> None:
        self._lock = RLock()
        self._binding_digest = binding_digest
        self._generation = generation
        self._revoked = False

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def revoke(self) -> None:
        with self._lock:
            self._revoked = True
            self._generation += 1

    @contextmanager
    def commitment(self, binding_digest: str, generation: int):
        with self._lock:
            if (
                self._revoked
                or binding_digest != self._binding_digest
                or generation != self._generation
            ):
                raise CodexProviderError("Codex launcher decision is revoked or stale")
            yield


class CodexSandboxAttestor:
    """Attest the exact local Codex mechanics selected by the owner binding.

    This does not claim a Constrained-runner isolation boundary.  It records that
    the local-unsandboxed adapter requested Codex's audited workspace-write mode,
    denied VCS writes in its Lockstep manifest gate, and closed inherited FDs.
    """

    def __init__(self, *, cli_version: str) -> None:
        if not cli_version:
            raise ValueError("Codex CLI version must be explicit")
        self._cli_version = cli_version

    def preflight(self, policy: SandboxPolicy) -> SandboxAttestation:
        argv = policy.argv
        adjacent = set(pairwise(argv))
        managed = {
            ("--ask-for-approval", "never"),
            ("--sandbox", "workspace-write"),
        }.issubset(adjacent)
        pinned = (
            len(argv) >= 9
            and argv[1] == "sandbox"
            and ("--permission-profile", argv[3]) in adjacent
            and ("--cd", str(policy.cwd)) in adjacent
            and "--include-managed-config" in argv
            and "--" in argv
        )
        if (
            not (managed or pinned)
            or (managed and policy.cwd != policy.write_root)
            or (
                pinned
                and policy.cwd != policy.write_root
                and policy.write_root not in policy.cwd.parents
            )
            or policy.denied_vcs_roots != (policy.write_root / ".git",)
            or not policy.close_fds
            or policy.inherited_fds
        ):
            raise CodexProviderError(
                "Codex sandbox policy is not the audited managed profile"
            )
        return SandboxAttestation(
            provider_id="codex-cli-requested-mechanics",
            provider_version=self._cli_version,
            policy_digest=policy.digest,
            denies_outside_workspace=False,
            denies_vcs_write=False,
            denies_symlink_escape=False,
            evidence_scope="requested_mechanics",
        )


def _attestation_digest(attestation: SandboxAttestation) -> str:
    return hashlib.sha256(_canonical(asdict(attestation))).hexdigest()
