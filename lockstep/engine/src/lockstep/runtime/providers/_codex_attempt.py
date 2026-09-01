"""Durable Codex execution, observation, and terminal lifecycle."""

from __future__ import annotations
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal
from lockstep.runtime.effects.descriptors import parse_effect_result
from lockstep.runtime.locking import file_lock
from lockstep.runtime.payload_limits import bounded_json
from lockstep.runtime.providers.base import EffectRequest, PreparedLaunch, RunnerObservation, TerminalSafetyObservation
from lockstep.runtime.providers.workspaces import WorkspaceError
from lockstep.runtime.sandbox import SandboxPolicy, verify_attestation

from lockstep.runtime.providers._codex_support import (
    CodexInstallationBinding,
    CodexProviderError,
    _attestation_digest,
    _canonical,
)
from lockstep.runtime.providers._codex_preparation import (
    CodexLaunchRecord,
    _CodexAttemptState,
    _CodexPreparation,
)


class _CodexAttemptDriver(_CodexAttemptState, _CodexPreparation):
    required_authorities = ("os_user_execution",)

    reconciliation_boundary = "local_durable_handle"

    accepted_effect_kinds = frozenset({"managed"})

    required_capabilities = frozenset(
        {"workspace", "bounded_result", "sandbox", "network", "credentials"}
    )

    workspace_purpose: Literal["managed_output", "no_publish_operation"] = (
        "managed_output"
    )

    execution_class: Literal["managed-agent", "pinned-command"] = "managed-agent"

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

    def _launcher_binding_digest(
        self, binding: CodexInstallationBinding
    ) -> str:
        return binding.digest

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
