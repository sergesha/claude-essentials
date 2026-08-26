"""Codex-specific implementation of the provider-neutral runner contract."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from threading import RLock
from typing import Literal

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.effects.descriptors import parse_effect_result
from lockstep.runtime.locking import file_lock
from lockstep.runtime.owner_state import (
    ensure_owner_directory,
    initialize_owner_state,
    verify_owner_directory,
    verify_owner_file,
)
from lockstep.runtime.payload_limits import bounded_json
from lockstep.runtime.providers.base import (
    DefinitiveProviderFailure,
    EffectRequest,
    PreparedLaunch,
    RunnerObservation,
    TerminalSafetyObservation,
)
from lockstep.runtime.providers.workspaces import (
    LocalGitWorkspaceProvider,
    WorkspaceError,
)
from lockstep.runtime.sandbox import (
    SandboxAttestation,
    SandboxAttestor,
    SandboxPolicy,
    verify_attestation,
)


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


@dataclass(frozen=True)
class CodexLaunchRecord:
    effect_id: str
    request_digest: str
    runner_binding_digest: str
    workspace_ref: str
    workspace_path: Path
    workspace_purpose: Literal["managed_output", "no_publish_operation"]
    execution_class: Literal["managed-agent", "pinned-command"]
    cwd: Path
    executable_path: Path
    executable_identity_digest: str
    inner_argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    codex_home: Path
    credential_identity_digest: str | None
    sandbox_policy_digest: str
    sandbox_attestation_digest: str
    launcher_decision_generation: int
    deadline_at: datetime
    launch_ref: str
    shell: bool = False
    close_fds: bool = True
    inherited_fds: tuple[int, ...] = ()
    deployment_profile: Literal["local_unsandboxed"] = "local_unsandboxed"


def _attestation_digest(attestation: SandboxAttestation) -> str:
    return hashlib.sha256(_canonical(asdict(attestation))).hexdigest()


class _CodexAttemptDriver:
    required_authorities = ("os_user_execution",)
    reconciliation_boundary = "local_durable_handle"
    effect_kind = "managed"
    required_capabilities = frozenset(
        {"workspace", "bounded_result", "sandbox", "network", "credentials"}
    )
    workspace_purpose: Literal["managed_output", "no_publish_operation"] = (
        "managed_output"
    )
    execution_class: Literal["managed-agent", "pinned-command"] = "managed-agent"

    def __init__(
        self,
        *,
        owner_state_dir: str | Path,
        installation: Callable[[], CodexInstallationBinding],
        decision_gate: CodexLaunchDecisionGate,
        workspaces: LocalGitWorkspaceProvider,
        blobs: BlobStore,
        sandbox: SandboxAttestor,
        limits: CodexCaptureLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.owner_state_dir = initialize_owner_state(owner_state_dir)
        self._attempts = ensure_owner_directory(self.owner_state_dir, "codex-attempts")
        self._installation = installation
        self._binding = installation()
        self.binding_digest = self._binding.digest
        self._decision_gate = decision_gate
        self._workspaces = workspaces
        self._blobs = blobs
        self._sandbox = sandbox
        self._limits = limits or CodexCaptureLimits()
        self._clock = clock or (lambda: datetime.now(UTC))
        self.spawn_count = 0

    def _directory(self, effect_id: str) -> Path:
        name = hashlib.sha256(effect_id.encode()).hexdigest()
        return ensure_owner_directory(self._attempts, name)

    def _admit_attempt(self, effect_id: str) -> None:
        name = hashlib.sha256(effect_id.encode()).hexdigest()
        target = self._attempts / name
        if target.exists() or target.is_symlink():
            return
        for count, _entry in enumerate(self._attempts.iterdir(), start=1):
            if count >= self._limits.max_retained_attempts:
                raise CodexProviderError("Codex retained-attempt quota is exhausted")

    @staticmethod
    def _input(request: EffectRequest, name: str) -> object:
        values = dict(request.inputs)
        if name not in values:
            raise CodexProviderError(f"managed Codex request is missing {name!r} input")
        return values[name]

    def _policy(self, record: CodexLaunchRecord) -> SandboxPolicy:
        return SandboxPolicy(
            read_roots=(record.workspace_path,),
            write_root=record.workspace_path,
            temp_root=Path(dict(record.environment)["TMPDIR"]),
            denied_vcs_roots=(record.workspace_path / ".git",),
            network_allowed=True,
            argv=record.inner_argv,
            cwd=record.cwd,
            environment=record.environment,
            close_fds=True,
            inherited_fds=(),
        )

    @staticmethod
    def _assert_no_project_control_surfaces(workspace: Path) -> None:
        for relative in (".codex", ".agents", ".mcp.json"):
            candidate = workspace / relative
            if candidate.exists() or candidate.is_symlink():
                raise CodexProviderError(
                    f"managed workspace contains forbidden Codex control surface: {relative}"
                )

    def _record_data(self, record: CodexLaunchRecord) -> dict[str, object]:
        return {
            "schema": "lockstep.codex-launch/v1",
            "effect_id": record.effect_id,
            "request_digest": record.request_digest,
            "runner_binding_digest": record.runner_binding_digest,
            "workspace_ref": record.workspace_ref,
            "workspace_path": str(record.workspace_path),
            "workspace_purpose": record.workspace_purpose,
            "execution_class": record.execution_class,
            "cwd": str(record.cwd),
            "executable_path": str(record.executable_path),
            "executable_identity_digest": record.executable_identity_digest,
            "inner_argv": list(record.inner_argv),
            "environment": [list(item) for item in record.environment],
            "codex_home": str(record.codex_home),
            "credential_identity_digest": record.credential_identity_digest,
            "sandbox_policy_digest": record.sandbox_policy_digest,
            "sandbox_attestation_digest": record.sandbox_attestation_digest,
            "launcher_decision_generation": record.launcher_decision_generation,
            "deadline_at": record.deadline_at.isoformat(),
            "launch_ref": record.launch_ref,
            "shell": False,
            "close_fds": True,
            "inherited_fds": [],
            "deployment_profile": "local_unsandboxed",
        }

    def _write_once(self, path: Path, data: bytes) -> None:
        if path.exists() or path.is_symlink():
            verify_owner_file(path)
            if path.read_bytes() != data:
                raise CodexProviderError(
                    f"immutable Codex record mismatch: {path.name}"
                )
            return
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            self._write_once(path, data)
            return
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def _atomic_json(self, path: Path, value: object) -> None:
        encoded = _canonical(value)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_record(self, effect_id: str) -> CodexLaunchRecord:
        path = self._directory(effect_id) / "launch.json"
        try:
            raw = json.loads(path.read_bytes())
            if raw["schema"] != "lockstep.codex-launch/v1":
                raise ValueError
            workspace_purpose = raw.get("workspace_purpose", "managed_output")
            if workspace_purpose not in {"managed_output", "no_publish_operation"}:
                raise ValueError
            execution_class = raw.get("execution_class", "managed-agent")
            if execution_class not in {"managed-agent", "pinned-command"}:
                raise ValueError
            record = CodexLaunchRecord(
                effect_id=raw["effect_id"],
                request_digest=raw["request_digest"],
                runner_binding_digest=raw["runner_binding_digest"],
                workspace_ref=raw["workspace_ref"],
                workspace_path=Path(raw["workspace_path"]),
                workspace_purpose=workspace_purpose,
                execution_class=execution_class,
                cwd=Path(raw.get("cwd", raw["workspace_path"])),
                executable_path=Path(raw["executable_path"]),
                executable_identity_digest=raw["executable_identity_digest"],
                inner_argv=tuple(raw["inner_argv"]),
                environment=tuple(tuple(item) for item in raw["environment"]),
                codex_home=Path(raw["codex_home"]),
                credential_identity_digest=raw["credential_identity_digest"],
                sandbox_policy_digest=raw["sandbox_policy_digest"],
                sandbox_attestation_digest=raw["sandbox_attestation_digest"],
                launcher_decision_generation=int(raw["launcher_decision_generation"]),
                deadline_at=datetime.fromisoformat(raw["deadline_at"]).astimezone(UTC),
                launch_ref=raw["launch_ref"],
            )
            if (
                record.execution_class != self.execution_class
                or record.runner_binding_digest != self.binding_digest
            ):
                raise ValueError
            return record
        except (
            FileNotFoundError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise CodexProviderError("invalid or missing Codex launch record") from exc

    def launch_record(self, effect_id: str) -> CodexLaunchRecord:
        return self._load_record(effect_id)

    def _recover_prepared_launch(
        self, record: CodexLaunchRecord, *, stdin_bytes: bytes
    ) -> None:
        directory = self._directory(record.effect_id)
        state_path = directory / "state.json"
        if (directory / "terminal.json").exists() or (
            directory / "result.json"
        ).exists():
            return
        if state_path.exists() or state_path.is_symlink():
            verify_owner_file(state_path)
            state = self._state(record.effect_id)
            if state.get("phase") == "running":
                return
            if state.get("phase") != "prepared":
                raise CodexProviderError("invalid Codex attempt state during recovery")
        else:
            possible_launch = (
                "supervisor.json",
                "supervisor-ready.json",
                "supervisor-alive.lock",
                "go",
                "cancel",
                "started.json",
            )
            if any(
                (directory / name).exists() or (directory / name).is_symlink()
                for name in possible_launch
            ):
                raise CodexProviderError(
                    "partial Codex launch cannot be recovered safely"
                )
        self._write_once(directory / "stdin.bin", stdin_bytes)
        if not state_path.exists():
            self._atomic_json(
                state_path,
                {"schema": "lockstep.codex-state/v1", "phase": "prepared"},
            )

    def _request_payload(self, request: EffectRequest) -> tuple[bytes, str]:
        brief = self._input(request, "brief")
        snapshot_ref = self._input(request, "snapshot")
        if not isinstance(brief, str) or not isinstance(snapshot_ref, str):
            raise CodexProviderError("Codex brief and snapshot inputs must be strings")
        return brief.encode(), snapshot_ref

    def _inner_argv(
        self,
        binding: CodexInstallationBinding,
        workspace: Path,
        request: EffectRequest,
    ) -> tuple[str, ...]:
        return _managed_argv(
            binding.executable_path,
            model=binding.model,
            workspace=workspace,
            permission_profile=binding.permission_profile,
        )

    def _execution_cwd(self, workspace: Path, request: EffectRequest) -> Path:
        del request
        return workspace

    def _launcher_binding_digest(
        self, binding: CodexInstallationBinding
    ) -> str:
        return binding.digest

    def _validate_prepare_request(self, request: EffectRequest) -> None:
        if request.grant_digest is None or request.workspace_ref is None:
            raise CodexProviderError(
                "Codex request requires an exact grant and workspace"
            )
        if request.effect_kind != self.effect_kind:
            raise CodexProviderError(
                f"Codex adapter accepts only {self.effect_kind} effects"
            )
        if request.runner_binding_digest != self.binding_digest:
            raise CodexProviderError(
                "Codex request uses a different runner binding"
            )
        if request.deadline_at is None or request.deadline_at <= self._clock():
            raise CodexProviderError("Codex request deadline has expired")
        if not self.required_capabilities.issubset(request.required_capabilities):
            raise CodexProviderError(
                "Codex request lacks required runner capabilities"
            )

    def _recover_existing_preparation(
        self,
        request: EffectRequest,
        *,
        launch_path: Path,
        stdin_bytes: bytes,
    ) -> PreparedLaunch | None:
        if not launch_path.exists():
            return None
        record = self._load_record(request.effect_id)
        observed = (
            record.request_digest,
            record.runner_binding_digest,
            record.workspace_ref,
            record.execution_class,
            record.workspace_purpose,
        )
        expected = (
            request.request_digest,
            request.runner_binding_digest,
            request.workspace_ref,
            self.execution_class,
            self.workspace_purpose,
        )
        if observed != expected:
            raise CodexProviderError("same effect has a different prepared launch")
        self._recover_prepared_launch(record, stdin_bytes=stdin_bytes)
        return PreparedLaunch(
            record.effect_id,
            record.request_digest,
            record.runner_binding_digest,
            record.launch_ref,
            record.workspace_ref,
        )

    def _prepare_workspace(
        self,
        request: EffectRequest,
        snapshot_ref: str,
    ):
        workspace = self._workspaces.materialize(
            effect_id=request.effect_id,
            request_digest=request.request_digest,
            workspace_ref=request.workspace_ref,
            input_snapshot_ref=snapshot_ref,
            declared_writes=request.writes,
            purpose=self.workspace_purpose,
        )
        try:
            self._assert_no_project_control_surfaces(workspace.workspace_path)
            return workspace
        except CodexProviderError as exc:
            try:
                self._workspaces.quarantine_and_rollover(workspace)
                self._workspaces.release(
                    self._workspaces.inspect(workspace.workspace_ref)
                )
            except WorkspaceError as cleanup_error:
                raise CodexProviderError(
                    "rejected Codex workspace could not be safely retired"
                ) from cleanup_error
            raise DefinitiveProviderFailure(
                self._error_result(request.effect_id, "prelaunch_failed")
            ) from exc

    def _current_workspace_binding(self, workspace_path: Path):
        binding = self._installation()
        if binding != self._binding:
            raise CodexProviderError(
                "Codex installation binding changed before preparation"
            )
        binding.revalidate()
        if (
            binding.executable_path == workspace_path
            or workspace_path in binding.executable_path.parents
        ):
            raise CodexProviderError(
                "Codex executable may not reside in its workspace"
            )
        return binding

    def _provisional_launch_record(
        self,
        request: EffectRequest,
        workspace,
        binding: CodexInstallationBinding,
    ) -> CodexLaunchRecord:
        environment = dict(binding.environment)
        environment["CODEX_HOME"] = str(binding.codex_home)
        environment["HOME"] = str(binding.codex_home)
        return CodexLaunchRecord(
            effect_id=request.effect_id,
            request_digest=request.request_digest,
            runner_binding_digest=request.runner_binding_digest,
            workspace_ref=request.workspace_ref,
            workspace_path=workspace.workspace_path,
            workspace_purpose=self.workspace_purpose,
            execution_class=self.execution_class,
            cwd=self._execution_cwd(workspace.workspace_path, request),
            executable_path=binding.executable_path,
            executable_identity_digest=binding.digest,
            inner_argv=self._inner_argv(
                binding, workspace.workspace_path, request
            ),
            environment=tuple(sorted(environment.items())),
            codex_home=binding.codex_home,
            credential_identity_digest=binding.credential_identity_digest,
            sandbox_policy_digest="",
            sandbox_attestation_digest="",
            launcher_decision_generation=self._decision_gate.generation,
            deadline_at=request.deadline_at,
            launch_ref="pending",
        )

    def _attested_launch_record(
        self,
        provisional: CodexLaunchRecord,
    ) -> CodexLaunchRecord:
        policy = self._policy(provisional)
        attestation = verify_attestation(
            policy, self._sandbox.preflight(policy), require_enforced=False
        )
        attestation_digest = _attestation_digest(attestation)
        commitment = {
            **self._record_data(provisional),
            "sandbox_policy_digest": policy.digest,
            "sandbox_attestation_digest": attestation_digest,
        }
        commitment.pop("launch_ref")
        launch_ref = "codex:" + hashlib.sha256(_canonical(commitment)).hexdigest()
        return CodexLaunchRecord(
            **{
                **provisional.__dict__,
                "sandbox_policy_digest": policy.digest,
                "sandbox_attestation_digest": attestation_digest,
                "launch_ref": launch_ref,
            }
        )

    def _commit_prepared_launch(
        self,
        *,
        directory: Path,
        launch_path: Path,
        stdin_bytes: bytes,
        record: CodexLaunchRecord,
    ) -> PreparedLaunch:
        self._write_once(launch_path, _canonical(self._record_data(record)))
        self._write_once(directory / "stdin.bin", stdin_bytes)
        self._atomic_json(
            directory / "state.json",
            {"schema": "lockstep.codex-state/v1", "phase": "prepared"},
        )
        return PreparedLaunch(
            record.effect_id,
            record.request_digest,
            record.runner_binding_digest,
            record.launch_ref,
            record.workspace_ref,
        )

    def prepare(self, request: EffectRequest) -> PreparedLaunch:
        self._validate_prepare_request(request)
        self._admit_attempt(request.effect_id)
        directory = self._directory(request.effect_id)
        stdin_bytes, snapshot_ref = self._request_payload(request)
        launch_path = directory / "launch.json"
        recovered = self._recover_existing_preparation(
            request,
            launch_path=launch_path,
            stdin_bytes=stdin_bytes,
        )
        if recovered is not None:
            return recovered
        workspace = self._prepare_workspace(request, snapshot_ref)
        binding = self._current_workspace_binding(workspace.workspace_path)
        provisional = self._provisional_launch_record(
            request, workspace, binding
        )
        record = self._attested_launch_record(provisional)
        return self._commit_prepared_launch(
            directory=directory,
            launch_path=launch_path,
            stdin_bytes=stdin_bytes,
            record=record,
        )

    def _state(self, effect_id: str) -> dict[str, object]:
        try:
            raw = json.loads((self._directory(effect_id) / "state.json").read_bytes())
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise CodexProviderError("invalid or missing Codex attempt state") from exc
        if not isinstance(raw, dict) or raw.get("schema") != "lockstep.codex-state/v1":
            raise CodexProviderError("invalid Codex attempt state")
        return raw

    def _launch_body(self, record: CodexLaunchRecord) -> tuple[Path, str]:
        directory = self._directory(record.effect_id)
        body = {
            "schema": "lockstep.codex-supervisor/v1",
            "argv": list(record.inner_argv),
            "cwd": str(record.cwd),
            "environment": dict(record.environment),
            "executable_identity": {
                "device": self._binding.executable_device,
                "inode": self._binding.executable_inode,
                "size": self._binding.executable_size,
                "mtime_ns": self._binding.executable_mtime_ns,
                "sha256": self._binding.executable_sha256,
            },
            "credential_identity_digest": record.credential_identity_digest,
            "stdin": str(directory / "stdin.bin"),
            "stdout": str(directory / "stdout.bin"),
            "stderr": str(directory / "stderr.bin"),
            "supervisor_ready": str(directory / "supervisor-ready.json"),
            "alive": str(directory / "supervisor-alive.lock"),
            "go": str(directory / "go"),
            "cancel": str(directory / "cancel"),
            "started": str(directory / "started.json"),
            "terminal": str(directory / "terminal.json"),
            "deadline_epoch": record.deadline_at.timestamp(),
            "max_stdout_bytes": self._limits.max_stdout_bytes,
            "max_stderr_bytes": self._limits.max_stderr_bytes,
        }
        encoded = _canonical(body)
        path = directory / "supervisor.json"
        self._write_once(path, encoded)
        return path, hashlib.sha256(encoded).hexdigest()

    def _validate_launch(
        self, launch: PreparedLaunch, record: CodexLaunchRecord
    ) -> None:
        if (
            launch.effect_id != record.effect_id
            or launch.request_digest != record.request_digest
            or launch.runner_binding_digest != record.runner_binding_digest
            or launch.launch_ref != record.launch_ref
            or launch.workspace_ref != record.workspace_ref
        ):
            raise CodexProviderError(
                "prepared launch does not match Codex launch record"
            )

    def _supervisor_ready(self, record: CodexLaunchRecord) -> int | None:
        path = self._directory(record.effect_id) / "supervisor-ready.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_bytes())
            if (
                not isinstance(raw, dict)
                or set(raw) != {"schema", "pid"}
                or raw["schema"] != "lockstep.codex-supervisor-ready/v1"
                or not isinstance(raw["pid"], int)
                or raw["pid"] <= 0
            ):
                raise ValueError
            return raw["pid"]
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CodexProviderError("invalid Codex supervisor receipt") from exc

    def _supervisor_alive(self, record: CodexLaunchRecord, pid: int) -> bool:
        path = self._directory(record.effect_id) / "supervisor-alive.lock"
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError:
            return False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self._alive(pid)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                return False
        finally:
            os.close(descriptor)

    def _commit_ready_supervisor(
        self, record: CodexLaunchRecord, supervisor_pid: int
    ) -> RunnerObservation:
        directory = self._directory(record.effect_id)
        self._write_once(directory / "go", b"start\n")
        self._atomic_json(
            directory / "state.json",
            {
                "schema": "lockstep.codex-state/v1",
                "phase": "running",
                "supervisor_pid": supervisor_pid,
            },
        )
        return self.inspect(record.effect_id)

    def ensure_started(self, launch: PreparedLaunch) -> RunnerObservation:
        record = self._load_record(launch.effect_id)
        self._validate_launch(launch, record)
        if self._clock() >= record.deadline_at:
            raise CodexProviderError("Codex launch deadline has expired")
        binding = self._installation()
        if (
            binding != self._binding
            or binding.digest != record.executable_identity_digest
        ):
            raise CodexProviderError("Codex installation binding changed before launch")
        binding.revalidate()
        policy = self._policy(record)
        attestation = verify_attestation(
            policy, self._sandbox.preflight(policy), require_enforced=False
        )
        if (
            policy.digest != record.sandbox_policy_digest
            or _attestation_digest(attestation) != record.sandbox_attestation_digest
        ):
            raise CodexProviderError("Codex sandbox attestation changed before launch")
        directory = self._directory(record.effect_id)
        with file_lock(directory / "decision", timeout=30, stale_after=300):
            state = self._state(record.effect_id)
            if state.get("phase") != "prepared":
                return self.inspect(record.effect_id)
            body_path, body_digest = self._launch_body(record)
            with self._decision_gate.commitment(
                self._launcher_binding_digest(binding),
                record.launcher_decision_generation,
            ):
                if self._clock() >= record.deadline_at:
                    raise CodexProviderError(
                        "Codex launch deadline expired at commitment"
                    )
                current_binding = self._installation()
                if current_binding != binding:
                    raise CodexProviderError(
                        "Codex installation binding changed at commitment"
                    )
                current_binding.revalidate()
                ready_pid = self._supervisor_ready(record)
                if ready_pid is not None:
                    if not self._supervisor_alive(record, ready_pid):
                        raise CodexProviderError(
                            "prepared Codex supervisor exited before launch"
                        )
                    return self._commit_ready_supervisor(record, ready_pid)
                supervisor_argv = (
                    sys.executable,
                    "-m",
                    "lockstep.runtime.providers._codex_supervisor",
                    str(body_path),
                    body_digest,
                )
                try:
                    process = subprocess.Popen(
                        supervisor_argv,
                        cwd=str(self.owner_state_dir),
                        env=dict(record.environment),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        shell=False,
                        close_fds=True,
                        start_new_session=True,
                    )
                except OSError as exc:
                    raise CodexProviderError(
                        "Codex supervisor was not started"
                    ) from exc
                self.spawn_count += 1
                ready_deadline = min(
                    time.monotonic() + 5,
                    time.monotonic()
                    + max(0.0, (record.deadline_at - self._clock()).total_seconds()),
                )
                ready_pid = self._supervisor_ready(record)
                while ready_pid is None and time.monotonic() < ready_deadline:
                    if process.poll() is not None:
                        raise CodexProviderError(
                            "Codex supervisor exited before publishing its handle"
                        )
                    time.sleep(0.01)
                    ready_pid = self._supervisor_ready(record)
                if ready_pid is None:
                    raise CodexProviderError(
                        "Codex supervisor did not publish its handle before launch timeout"
                    )
                return self._commit_ready_supervisor(record, ready_pid)
        return self.inspect(record.effect_id)

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _terminal_receipt(self, record: CodexLaunchRecord) -> dict[str, object] | None:
        path = self._directory(record.effect_id) / "terminal.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_bytes())
        except json.JSONDecodeError as exc:
            raise CodexProviderError("invalid Codex terminal receipt") from exc
        required = {
            "schema",
            "returncode",
            "overflow",
            "timed_out",
            "quiescent",
            "stdout_size",
            "stdout_sha256",
            "stderr_size",
            "stderr_sha256",
        }
        allowed = required | {"termination_reason"}
        if (
            not isinstance(raw, dict)
            or not required.issubset(raw)
            or not set(raw).issubset(allowed)
            or raw["schema"] != "lockstep.codex-terminal/v1"
        ):
            raise CodexProviderError("invalid Codex terminal receipt")
        reason = raw.get("termination_reason", "exited")
        if reason not in {
            "exited",
            "cancelled",
            "deadline",
            "output_overflow",
            "spawn_failed",
            "stdin_failed",
        }:
            raise CodexProviderError("invalid Codex terminal disposition")
        raw["termination_reason"] = reason
        return raw

    def _error_result(self, effect_id: str, code: str):
        return parse_effect_result(
            {
                "schema": "lockstep.effect-result/v1",
                "effect_id": effect_id,
                "outcome": "ERROR",
                "result_ref": None,
                "artifact_refs": [],
                "snapshot_ref": None,
                "diff_ref": None,
                "fixed_error_code": code,
                "evidence_refs": [],
            }
        )

    def _stored_result(self, record: CodexLaunchRecord):
        path = self._directory(record.effect_id) / "result.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_bytes())
            return parse_effect_result(raw)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CodexProviderError("invalid stored Codex result") from exc

    def _cleanup_spools(self, record: CodexLaunchRecord) -> None:
        directory = self._directory(record.effect_id)
        for name in ("stdin.bin", "stdout.bin", "stderr.bin"):
            path = directory / name
            if path.is_symlink():
                raise CodexProviderError("Codex spool path became a symlink")
            path.unlink(missing_ok=True)

    def _parse_result(
        self,
        record: CodexLaunchRecord,
        receipt: dict[str, object],
        snapshot_ref: str | None,
    ):
        if snapshot_ref is None:
            raise CodexProviderError("managed result requires a rollover snapshot")
        directory = self._directory(record.effect_id)
        stdout = (directory / "stdout.bin").read_bytes()
        stderr = (directory / "stderr.bin").read_bytes()
        if (
            len(stdout) != receipt["stdout_size"]
            or len(stderr) != receipt["stderr_size"]
            or hashlib.sha256(stdout).hexdigest() != receipt["stdout_sha256"]
            or hashlib.sha256(stderr).hexdigest() != receipt["stderr_sha256"]
        ):
            return self._error_result(record.effect_id, "result_invalid")
        if receipt["overflow"]:
            return self._error_result(record.effect_id, "result_invalid")
        if receipt["timed_out"]:
            return self._error_result(record.effect_id, "deadline_timeout")
        if receipt["returncode"] != 0:
            return self._error_result(record.effect_id, "runner_failed")
        final_message: str | None = None
        lines = stdout.splitlines()
        if len(lines) > self._limits.max_json_records:
            return self._error_result(record.effect_id, "result_invalid")
        try:
            for encoded in lines:
                event = bounded_json(json.loads(encoded), label="Codex JSONL event")
                if (
                    isinstance(event, dict)
                    and event.get("type") == "item.completed"
                    and isinstance(event.get("item"), dict)
                    and event["item"].get("type") == "agent_message"
                    and isinstance(event["item"].get("text"), str)
                ):
                    final_message = event["item"]["text"]
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            return self._error_result(record.effect_id, "result_invalid")
        if (
            final_message is None
            or len(final_message.encode()) > self._limits.max_result_bytes
        ):
            return self._error_result(record.effect_id, "result_invalid")
        blob = self._blobs.put(final_message.encode())
        return parse_effect_result(
            {
                "schema": "lockstep.effect-result/v1",
                "effect_id": record.effect_id,
                "outcome": "PASS",
                "result_ref": f"blob:{blob.sha256}",
                "artifact_refs": [],
                "snapshot_ref": snapshot_ref,
                "diff_ref": None,
                "fixed_error_code": None,
                "evidence_refs": [],
            }
        )

    def _finalize_workspace(self, record: CodexLaunchRecord) -> str | None:
        workspace = self._workspaces.inspect(record.workspace_ref)
        if record.workspace_purpose == "no_publish_operation":
            self._workspaces.quarantine_no_publish(workspace)
            return None
        return self._workspaces.quarantine_and_rollover(workspace)

    def _terminal(
        self, record: CodexLaunchRecord, receipt: dict[str, object]
    ) -> RunnerObservation:
        if not receipt["quiescent"]:
            return RunnerObservation(
                record.effect_id,
                record.request_digest,
                record.runner_binding_digest,
                "running",
            )
        directory = self._directory(record.effect_id)
        with file_lock(directory / "finalize", timeout=30, stale_after=300):
            stored = self._stored_result(record)
            if stored is None:
                try:
                    snapshot_ref = self._finalize_workspace(record)
                except WorkspaceError:
                    workspace = self._workspaces.inspect(record.workspace_ref)
                    if workspace.phase != "quarantined":
                        raise
                    stored = self._error_result(record.effect_id, "writes_invalid")
                else:
                    stored = self._parse_result(record, receipt, snapshot_ref)
                self._write_once(
                    directory / "result.json",
                    _canonical(stored.to_dict()),
                )
            workspace = self._workspaces.inspect(record.workspace_ref)
            if record.workspace_purpose == "managed_output" and (
                workspace.phase == "quarantined"
                and workspace.rollover_snapshot_ref is not None
            ):
                self._workspaces.release(workspace)
            elif workspace.phase not in {"quarantined", "released"}:
                raise WorkspaceError("stored result precedes workspace quarantine")
            self._cleanup_spools(record)
            return RunnerObservation(
                record.effect_id,
                record.request_digest,
                record.runner_binding_digest,
                "terminal",
                stored,
            )

    def inspect(self, effect_id: str) -> RunnerObservation:
        record = self._load_record(effect_id)
        receipt = self._terminal_receipt(record)
        if receipt is not None:
            return self._terminal(record, receipt)
        state = self._state(effect_id)
        phase = state.get("phase")
        if phase == "prepared":
            disposition = "absent"
        elif phase == "running":
            ready_pid = self._supervisor_ready(record)
            alive = (
                ready_pid is not None
                and ready_pid == int(state["supervisor_pid"])
                and self._supervisor_alive(record, ready_pid)
            )
            if not alive:
                # The supervisor publishes terminal.json before releasing its
                # liveness lock. Close the observation race across those two
                # reads before declaring an unrecoverable launch state.
                receipt = self._terminal_receipt(record)
                if receipt is not None:
                    return self._terminal(record, receipt)
            disposition = "running" if alive else "indeterminate"
        else:
            disposition = "indeterminate"
        return RunnerObservation(
            record.effect_id,
            record.request_digest,
            record.runner_binding_digest,
            disposition,
        )

    def lookup(self, effect_id: str) -> RunnerObservation:
        return self.inspect(effect_id)

    def cancel(self, effect_id: str) -> RunnerObservation:
        record = self._load_record(effect_id)
        if self._terminal_receipt(record) is not None:
            return self.inspect(effect_id)
        state = self._state(effect_id)
        ready_pid = self._supervisor_ready(record)
        if (
            state.get("phase") == "running"
            and ready_pid is not None
            and ready_pid == state.get("supervisor_pid")
            and self._supervisor_alive(record, ready_pid)
        ):
            self._write_once(self._directory(effect_id) / "cancel", b"cancel\n")
        return self.inspect(record.effect_id)

    def quiesce(self, effect_id: str) -> TerminalSafetyObservation:
        record = self._load_record(effect_id)
        receipt = self._terminal_receipt(record)
        launch = PreparedLaunch(
            record.effect_id,
            record.request_digest,
            record.runner_binding_digest,
            record.launch_ref,
            record.workspace_ref,
        )
        if receipt is None or not receipt["quiescent"]:
            return TerminalSafetyObservation.pending_for(launch)
        terminal = self._terminal(record, receipt)
        assert terminal.result is not None
        workspace = self._workspaces.inspect(record.workspace_ref)
        rollover = workspace.rollover_snapshot_ref
        quarantined = workspace.phase == "quarantined" and rollover is None
        if rollover is None and not quarantined:
            raise WorkspaceError("workspace has no terminal-safety proof")
        return TerminalSafetyObservation.proven_for(
            launch,
            result_stable=record.workspace_purpose == "managed_output",
            rollover_snapshot_ref=rollover,
            workspace_quarantined=quarantined,
        )

    def wait_terminal(self, effect_id: str, *, timeout: float) -> RunnerObservation:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            observation = self.inspect(effect_id)
            if observation.state in {"terminal", "indeterminate"}:
                return observation
            time.sleep(0.02)
        raise TimeoutError(f"Codex attempt {effect_id} did not become terminal")


class CodexRunnerAdapter:
    """Managed Codex strategy delegated to the shared durable attempt driver."""

    required_authorities = _CodexAttemptDriver.required_authorities
    reconciliation_boundary = _CodexAttemptDriver.reconciliation_boundary

    def __init__(self, **kwargs) -> None:
        self._driver = _CodexAttemptDriver(**kwargs)

    @property
    def binding_digest(self) -> str:
        return self._driver.binding_digest

    @property
    def spawn_count(self) -> int:
        return self._driver.spawn_count

    def prepare(self, request: EffectRequest) -> PreparedLaunch:
        return self._driver.prepare(request)

    def ensure_started(self, launch: PreparedLaunch) -> RunnerObservation:
        return self._driver.ensure_started(launch)

    def inspect(self, effect_id: str) -> RunnerObservation:
        return self._driver.inspect(effect_id)

    lookup = inspect

    def cancel(self, effect_id: str) -> RunnerObservation:
        return self._driver.cancel(effect_id)

    def quiesce(self, effect_id: str) -> TerminalSafetyObservation:
        return self._driver.quiesce(effect_id)

    def __getattr__(self, name: str):
        # Compatibility for provider diagnostics and the Task 6 white-box
        # conformance tests; execution authority stays in the private driver.
        return getattr(self._driver, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_driver" or "_driver" not in self.__dict__:
            object.__setattr__(self, name, value)
        elif hasattr(self._driver, name):
            setattr(self._driver, name, value)
        else:
            object.__setattr__(self, name, value)
