"""Durable Codex attempt state and launch preparation."""

from __future__ import annotations
import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.owner_state import ensure_owner_directory, initialize_owner_state, verify_owner_file
from lockstep.runtime.providers.base import DefinitiveProviderFailure, EffectRequest, PreparedLaunch
from lockstep.runtime.providers.workspaces import LocalGitWorkspaceProvider, WorkspaceError
from lockstep.runtime.sandbox import SandboxAttestor, verify_attestation

from lockstep.runtime.providers._codex_support import (
    CodexCaptureLimits,
    CodexInstallationBinding,
    CodexLaunchDecisionGate,
    CodexProviderError,
    _attestation_digest,
    _canonical,
    _managed_argv,
)
from lockstep.runtime.providers._codex_services import (
    _CodexAttemptServices,
    _ServiceAlias,
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


def _record_data(record: CodexLaunchRecord) -> dict[str, object]:
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


class _CodexAttemptState:
    _blobs = _ServiceAlias("_blobs")
    _clock = _ServiceAlias("_clock")
    _decision_gate = _ServiceAlias("_decision_gate")
    _limits = _ServiceAlias("_limits")
    _sandbox = _ServiceAlias("_sandbox")
    _workspaces = _ServiceAlias("_workspaces")

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
        self._services = _CodexAttemptServices(
            _blobs=blobs,
            _clock=clock or (lambda: datetime.now(UTC)),
            _decision_gate=decision_gate,
            _limits=limits or CodexCaptureLimits(),
            _sandbox=sandbox,
            _workspaces=workspaces,
        )
        self.spawn_count = 0

    def _directory(self, effect_id: str) -> Path:
        name = hashlib.sha256(effect_id.encode()).hexdigest()
        return ensure_owner_directory(self._attempts, name)

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


class _CodexPreparation:
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

    @staticmethod
    def _assert_no_project_control_surfaces(workspace: Path) -> None:
        for relative in (".codex", ".agents", ".mcp.json"):
            candidate = workspace / relative
            if candidate.exists() or candidate.is_symlink():
                raise CodexProviderError(
                    f"managed workspace contains forbidden Codex control surface: {relative}"
                )

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

    def _validate_prepare_request(self, request: EffectRequest) -> None:
        if request.grant_digest is None or request.workspace_ref is None:
            raise CodexProviderError(
                "Codex request requires an exact grant and workspace"
            )
        if request.effect_kind not in self.accepted_effect_kinds:
            accepted = " or ".join(sorted(self.accepted_effect_kinds))
            raise CodexProviderError(
                f"Codex adapter accepts only {accepted} effects"
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
            **_record_data(provisional),
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
        self._write_once(launch_path, _canonical(_record_data(record)))
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
