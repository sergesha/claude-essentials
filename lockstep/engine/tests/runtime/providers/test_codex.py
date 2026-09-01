from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.effects.authority import EffectGrant
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.project_snapshots import ProjectSnapshotRef, ProjectSnapshotStore
from lockstep.runtime.providers.base import EffectRequest, RunnerObservation
from lockstep.runtime.sandbox import FakeSandboxProvider, SandboxPolicy


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_binding_capture_rejects_atomic_executable_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lockstep.runtime.providers import codex as codex_module

    executable = _executable(tmp_path / "codex", "raise SystemExit(0)\n")
    replacement = _executable(
        tmp_path / "replacement-codex", "raise SystemExit(1)\n"
    )
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    auth = home / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)
    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir(mode=0o700)
    real_open = os.open
    replaced = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == executable and not replaced:
            replaced = True
            os.replace(replacement, executable)
        return descriptor

    monkeypatch.setattr(codex_module.os, "open", racing_open)

    with pytest.raises(codex_module.CodexProviderError, match="identity changed"):
        codex_module.CodexInstallationBinding.capture(
            executable=executable,
            model="model",
            cli_version="version",
            permission_profile={"sandbox": "workspace-write", "approval": "never"},
            codex_home=home,
            environment={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TMPDIR": str(private_tmp),
            },
        )
    assert replaced


@contextmanager
def _ready_supervisor(adapter, effect_id: str):
    directory = adapter._directory(effect_id)
    alive = os.open(directory / "supervisor-alive.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(alive, fcntl.LOCK_EX | fcntl.LOCK_NB)
    adapter._atomic_json(
        directory / "supervisor-ready.json",
        {"schema": "lockstep.codex-supervisor-ready/v1", "pid": os.getpid()},
    )
    try:
        yield directory
    finally:
        fcntl.flock(alive, fcntl.LOCK_UN)
        os.close(alive)


@pytest.fixture
def provider_system(tmp_path: Path):
    from lockstep.runtime.providers.codex import (
        CodexCaptureLimits,
        CodexInstallationBinding,
        CodexLaunchDecisionGate,
        CodexRunnerAdapter,
        CodexSandboxAttestor,
    )
    from lockstep.runtime.providers.workspaces import LocalGitWorkspaceProvider

    owner = tmp_path / "owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    seed = snapshots.capture(
        {"src/app.py": blobs.put(b"VALUE = 1\n")},
        declared_paths=("src/",),
        provenance={"source": "test"},
    )
    codex = _executable(
        tmp_path / "fake-codex",
        """
import json, pathlib, sys
prompt = sys.stdin.read()
if "overflow" in prompt:
    print("x" * 2048)
    raise SystemExit(0)
root = pathlib.Path.cwd()
(root / "src" / "app.py").write_text("VALUE = 2\\n")
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}))
""",
    )
    codex_home = owner / "codex-home"
    codex_home.mkdir(mode=0o700)
    private_tmp = owner / "tmp"
    private_tmp.mkdir(mode=0o700)
    binding = CodexInstallationBinding.capture(
        executable=codex,
        model="gpt-test",
        cli_version="0.147.0-test",
        permission_profile={
            "sandbox": "workspace-write",
            "approval": "never",
        },
        codex_home=codex_home,
        environment={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(private_tmp),
        },
    )
    current = {"binding": binding}
    workspaces = LocalGitWorkspaceProvider(owner, snapshots, blobs)
    gate = CodexLaunchDecisionGate(binding.digest, generation=7)
    adapter = CodexRunnerAdapter(
        owner_state_dir=owner,
        installation=lambda: current["binding"],
        decision_gate=gate,
        workspaces=workspaces,
        blobs=blobs,
        sandbox=CodexSandboxAttestor(cli_version=binding.cli_version),
        limits=CodexCaptureLimits(max_stdout_bytes=512, max_stderr_bytes=512),
    )

    intent = EffectRequest.build(
        effect_id="eff_codex",
        public_run_id="run-codex",
        project_identity="project-codex",
        definition_digest="a" * 64,
        coordinate=NativeCoordinate("thread", "checkpoint", "", "task", "interrupt"),
        descriptor_digest="b" * 64,
        effect_kind="managed",
        runner_selector="codex",
        runner_binding_digest=adapter.binding_digest,
        required_capabilities=(
            "workspace",
            "bounded_result",
            "sandbox",
            "network",
            "credentials",
        ),
        inputs=(("brief", "change VALUE to 2"), ("snapshot", f"snapshot:{seed.digest}")),
        writes=("src/",),
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    workspace_ref = workspaces.workspace_ref_for(intent.effect_id, intent.intent_digest)
    grant = EffectGrant.build(
        intent,
        actor_binding_digest="c" * 64,
        required_authorities=("os_user_execution",),
        workspace_ref=workspace_ref,
        parent_capability_generation=1,
        grant_generation=1,
        policy_epoch=1,
        config_epoch=7,
        approval_generation=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    request = intent.bind_grant(grant)
    return adapter, request, current, gate, workspaces, snapshots, blobs


def _next_request(
    adapter,
    request,
    workspaces,
    *,
    effect_id: str,
    brief: str,
    snapshot_ref: str | None = None,
):
    intent = EffectRequest.build(
        effect_id=effect_id,
        public_run_id=request.public_run_id,
        project_identity=request.project_identity,
        definition_digest=request.definition_digest,
        coordinate=replace(request.coordinate, interrupt_id=effect_id),
        descriptor_digest=request.descriptor_digest,
        effect_kind="managed",
        runner_selector="codex",
        runner_binding_digest=adapter.binding_digest,
        required_capabilities=request.required_capabilities,
        inputs=(
            ("brief", brief),
            ("snapshot", snapshot_ref or dict(request.inputs)["snapshot"]),
        ),
        writes=request.writes,
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    grant = EffectGrant.build(
        intent,
        actor_binding_digest="c" * 64,
        required_authorities=("os_user_execution",),
        workspace_ref=workspaces.workspace_ref_for(intent.effect_id, intent.intent_digest),
        parent_capability_generation=1,
        grant_generation=1,
        policy_epoch=1,
        config_epoch=7,
        approval_generation=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    return intent.bind_grant(grant)


def test_prepare_binds_exact_argv_profile_environment_and_no_shell(provider_system) -> None:
    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)
    record = adapter.launch_record(request.effect_id)

    assert launch == adapter.prepare(request)
    assert record.inner_argv == (
        str(record.executable_path),
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--model",
        "gpt-test",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
        str(record.workspace_path),
        "-",
    )
    assert record.shell is False
    assert record.close_fds is True
    assert record.inherited_fds == ()
    assert dict(record.environment) == {
        "CODEX_HOME": str(record.codex_home),
        "HOME": str(record.codex_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": dict(record.environment)["PATH"],
        "TMPDIR": dict(record.environment)["TMPDIR"],
    }
    assert "change VALUE" not in "\0".join(record.inner_argv)
    assert record.deployment_profile == "local_unsandboxed"
    attestation = adapter._sandbox.preflight(adapter._policy(record))
    assert attestation.evidence_scope == "requested_mechanics"
    assert not attestation.denies_outside_workspace
    assert not attestation.denies_vcs_write
    assert not attestation.denies_symlink_escape


def test_prepare_rejects_executable_or_permission_profile_drift(provider_system) -> None:
    adapter, request, current, _gate, _workspaces, _snapshots, _blobs = provider_system
    adapter.prepare(request)
    current["binding"] = replace(
        current["binding"],
        permission_profile=(("approval", "on-request"), ("sandbox", "workspace-write")),
    )

    with pytest.raises(Exception, match="binding|profile|installation"):
        adapter.ensure_started(adapter.prepare(request))


def test_deadline_and_launcher_decision_are_rechecked_before_spawn(provider_system) -> None:
    from lockstep.runtime.providers.codex import CodexProviderError

    adapter, request, _current, gate, _workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)
    gate.revoke()
    with pytest.raises(CodexProviderError, match="decision|revoked"):
        adapter.ensure_started(launch)
    assert adapter.spawn_count == 0

    expired = replace(request, deadline_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(CodexProviderError, match="deadline"):
        adapter.prepare(expired)


def test_lookup_adopts_same_attempt_and_never_spawns_twice(provider_system) -> None:
    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)
    adapter.ensure_started(launch)
    adapter.ensure_started(launch)

    assert adapter.spawn_count == 1
    assert adapter.lookup(request.effect_id).request_digest == request.request_digest


def test_concurrent_start_and_terminal_adoption_spawn_and_finalize_once(provider_system) -> None:
    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        starts = tuple(pool.map(lambda _index: adapter.ensure_started(launch), range(2)))
    assert adapter.spawn_count == 1
    assert all(item.state in {"running", "terminal"} for item in starts)

    adapter.wait_terminal(request.effect_id, timeout=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        terminals = tuple(pool.map(lambda _index: adapter.inspect(request.effect_id), range(2)))
    assert all(item.state == "terminal" for item in terminals)
    assert terminals[0].result == terminals[1].result


def test_prepared_attempt_adopts_ready_supervisor_before_inner_spawn(provider_system) -> None:
    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)
    with _ready_supervisor(adapter, request.effect_id) as directory:
        observation = adapter.ensure_started(launch)

    assert observation.state == "running"
    assert (directory / "go").is_file()
    assert adapter.spawn_count == 0


def test_ready_supervisor_is_still_subject_to_current_decision_gate(provider_system) -> None:
    from lockstep.runtime.providers.codex import CodexProviderError

    adapter, request, _current, gate, _workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)
    with _ready_supervisor(adapter, request.effect_id) as directory:
        gate.revoke()
        with pytest.raises(CodexProviderError, match="decision|revoked"):
            adapter.ensure_started(launch)
    assert not (directory / "go").exists()
    assert adapter.spawn_count == 0


def test_cancel_requests_live_supervisor_without_signalling_stored_pgid(
    provider_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    adapter.prepare(request)
    with _ready_supervisor(adapter, request.effect_id) as directory:
        adapter._atomic_json(
            directory / "state.json",
            {
                "schema": "lockstep.codex-state/v1",
                "phase": "running",
                "supervisor_pid": os.getpid(),
            },
        )
        adapter._atomic_json(
            directory / "started.json",
            {"schema": "lockstep.codex-started/v1", "pid": 999_999, "pgid": 999_999},
        )
        monkeypatch.setattr(
            "lockstep.runtime.providers.codex.os.killpg",
            lambda *_args: pytest.fail("adapter must not signal a stored process group"),
        )

        observation = adapter.cancel(request.effect_id)

        assert observation.state == "running"
        assert (directory / "cancel").read_bytes() == b"cancel\n"


def test_supervisor_rechecks_deadline_immediately_before_inner_spawn(
    provider_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    record = adapter.launch_record(adapter.prepare(request).effect_id)
    body_path, body_digest = adapter._launch_body(record)
    directory = adapter._directory(request.effect_id)
    (directory / "go").write_bytes(b"start\n")
    now = {"value": record.deadline_at.timestamp() - 1}

    def finish_verification(_spec, _argv) -> None:
        now["value"] = record.deadline_at.timestamp() + 1

    monkeypatch.setattr(supervisor, "_verify_bound_files", finish_verification)
    monkeypatch.setattr(supervisor.time, "time", lambda: now["value"])
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("expired launch reached inner Popen"),
    )

    assert supervisor.run(body_path, body_digest) == 0
    receipt = json.loads((directory / "terminal.json").read_bytes())
    assert receipt["timed_out"] is True
    assert receipt["quiescent"] is True


def test_supervisor_checks_deadline_after_bounded_stdin_read(
    provider_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    record = adapter.launch_record(adapter.prepare(request).effect_id)
    body_path, body_digest = adapter._launch_body(record)
    directory = adapter._directory(request.effect_id)
    (directory / "go").write_bytes(b"start\n")
    stdin_path = directory / "stdin.bin"
    now = {"value": record.deadline_at.timestamp() - 1}
    original_read = Path.read_bytes

    def advancing_read(path: Path) -> bytes:
        data = original_read(path)
        if path == stdin_path:
            now["value"] = record.deadline_at.timestamp() + 1
        return data

    monkeypatch.setattr(Path, "read_bytes", advancing_read)
    monkeypatch.setattr(supervisor, "_verify_bound_files", lambda _spec, _argv: None)
    monkeypatch.setattr(supervisor.time, "time", lambda: now["value"])
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("expired launch reached inner Popen"),
    )

    assert supervisor.run(body_path, body_digest) == 0
    assert json.loads((directory / "terminal.json").read_bytes())["timed_out"] is True


def test_supervisor_retains_liveness_until_trusted_process_group_is_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    observations = iter([False] * 251 + [True])
    calls = {"count": 0}
    delays: list[float] = []

    def observed_dead(_process_group: int) -> bool:
        calls["count"] += 1
        return next(observations)

    monkeypatch.setattr(supervisor, "_group_is_dead", observed_dead)
    monkeypatch.setattr(supervisor, "_kill_group", lambda _process_group: None)
    monkeypatch.setattr(supervisor.time, "sleep", delays.append)

    supervisor._wait_group_dead(42)
    assert calls["count"] == 252
    assert delays[:3] == [0.02, 0.04, 0.08]
    assert delays[-1] == 10.0
    assert all(delay <= 10.0 for delay in delays)


def test_supervisor_waits_for_group_death_before_final_capture_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    events: list[str] = []

    class Reader:
        def join(self) -> None:
            assert events == ["group-dead"]
            events.append("reader-joined")

    monkeypatch.setattr(
        supervisor, "_wait_group_dead", lambda _process_group: events.append("group-dead")
    )

    supervisor._finish_capture(42, (Reader(),))
    assert events == ["group-dead", "reader-joined"]


def test_supervisor_publishes_terminal_when_child_closes_stdin_immediately(
    provider_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    record = adapter.launch_record(adapter.prepare(request).effect_id)
    body_path, body_digest = adapter._launch_body(record)
    directory = adapter._directory(request.effect_id)
    (directory / "go").write_bytes(b"start\n")

    class ClosedInput(io.BytesIO):
        def write(self, _data):
            raise BrokenPipeError

        def close(self) -> None:
            pass

    class ImmediateExit:
        pid = 424242
        stdin = ClosedInput()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            return 0

    monkeypatch.setattr(supervisor, "_verify_bound_files", lambda _spec, _argv: None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_args, **_kwargs: ImmediateExit())
    monkeypatch.setattr(supervisor, "_kill_group", lambda _process_group: None)
    monkeypatch.setattr(supervisor, "_group_is_dead", lambda _process_group: True)

    assert supervisor.run(body_path, body_digest) == 0
    receipt = json.loads((directory / "terminal.json").read_bytes())
    assert receipt["returncode"] == 127
    assert receipt["quiescent"] is True


def test_synchronous_supervisor_failure_remains_definitely_absent(
    provider_system, monkeypatch
) -> None:
    from lockstep.runtime.providers.codex import CodexProviderError

    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)

    def fail_spawn(*_args, **_kwargs):
        raise OSError("exec failed")

    monkeypatch.setattr("lockstep.runtime.providers.codex.subprocess.Popen", fail_spawn)
    with pytest.raises(CodexProviderError, match="not started"):
        adapter.ensure_started(launch)

    assert adapter.inspect(request.effect_id).state == "absent"


def test_credential_rotation_after_prepare_blocks_inner_spawn(provider_system) -> None:
    from lockstep.runtime.providers.codex import CodexProviderError

    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)
    credential = adapter._binding.codex_home / "auth.json"
    credential.write_text('{"token":"rotated"}')
    credential.chmod(0o600)

    with pytest.raises(CodexProviderError, match="credential|installation"):
        adapter.ensure_started(launch)
    assert adapter.spawn_count == 0


def test_codex_adapter_rejects_non_managed_effect_kind(provider_system) -> None:
    from lockstep.runtime.providers.codex import CodexProviderError

    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    with pytest.raises(CodexProviderError, match="managed"):
        adapter.prepare(replace(request, effect_kind="verify"))
    assert adapter.spawn_count == 0


def test_prepare_recovers_crash_after_immutable_launch_commit(provider_system) -> None:
    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    expected = adapter.prepare(request)
    directory = adapter._directory(request.effect_id)
    (directory / "stdin.bin").unlink()
    (directory / "state.json").unlink()

    assert adapter.prepare(request) == expected
    assert (directory / "stdin.bin").read_bytes() == b"change VALUE to 2"
    assert json.loads((directory / "state.json").read_bytes()) == {
        "schema": "lockstep.codex-state/v1",
        "phase": "prepared",
    }


def test_terminal_result_uses_only_blob_and_snapshot_refs(provider_system) -> None:
    adapter, request, _current, _gate, workspaces, snapshots, blobs = provider_system
    launch = adapter.prepare(request)
    adapter.ensure_started(launch)
    observation = adapter.wait_terminal(request.effect_id, timeout=10)

    assert observation.state == "terminal"
    assert observation.result.outcome == "PASS"
    assert observation.result.artifact_refs == ()
    assert observation.result.result_ref.startswith("blob:")
    assert observation.result.snapshot_ref.startswith("snapshot:")
    assert not set(observation.result.to_dict()).intersection(
        {"codex_session", "argv", "environment", "workspace_path", "result_spool"}
    )
    safety = adapter.quiesce(request.effect_id)
    assert safety.state == "proven"
    assert safety.rollover_snapshot_ref == observation.result.snapshot_ref
    snapshot = snapshots.read(
        ProjectSnapshotRef(observation.result.snapshot_ref.removeprefix("snapshot:"))
    )
    assert blobs.read(snapshot.files[0].blob) == b"VALUE = 2\n"
    assert workspaces.inspect(launch.workspace_ref).phase == "released"
    attempt = adapter._directory(request.effect_id)
    assert not (attempt / "stdin.bin").exists()
    assert not (attempt / "stdout.bin").exists()
    assert not (attempt / "stderr.bin").exists()
    assert adapter.inspect(request.effect_id) == observation


def test_workspace_rollover_rejects_symlink_vcs_and_undeclared_mutations(provider_system) -> None:
    from lockstep.runtime.providers.workspaces import WorkspaceError

    adapter, request, _current, _gate, workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)
    lease = workspaces.inspect(launch.workspace_ref)
    outside = lease.workspace_path.parent / "outside"
    outside.write_text("outside")
    (lease.workspace_path / "src" / "link").symlink_to(outside)

    with pytest.raises(WorkspaceError, match="symlink|manifest|integrity"):
        workspaces.quarantine_and_rollover(lease)


def test_streaming_capture_limit_fails_without_snapshot_visibility(provider_system) -> None:
    adapter, request, _current, _gate, workspaces, _snapshots, _blobs = provider_system
    overflow = _next_request(
        adapter, request, workspaces, effect_id="eff_overflow", brief="overflow"
    )

    launch = adapter.prepare(overflow)
    adapter.ensure_started(launch)
    observation = adapter.wait_terminal(overflow.effect_id, timeout=10)

    assert observation.state == "terminal"
    assert observation.result.outcome == "ERROR"
    assert observation.result.fixed_error_code == "result_invalid"
    assert observation.result.result_ref is None
    assert observation.result.snapshot_ref is None
    safety = adapter.quiesce(overflow.effect_id)
    assert safety.state == "proven"
    assert safety.rollover_snapshot_ref is not None
    capture = (
        adapter.owner_state_dir
        / "codex-attempts"
        / hashlib.sha256(overflow.effect_id.encode()).hexdigest()
        / "stdout.bin"
    )
    receipt = json.loads((capture.parent / "terminal.json").read_bytes())
    assert receipt["stdout_size"] == 512
    assert not capture.exists()
    assert "subprocess" not in EffectRequest.__dataclass_fields__


def test_inspect_rechecks_terminal_after_supervisor_lock_release(
    provider_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)
    directory = adapter._directory(request.effect_id)
    adapter._atomic_json(
        directory / "supervisor-ready.json",
        {"schema": "lockstep.codex-supervisor-ready/v1", "pid": os.getpid()},
    )
    adapter._atomic_json(
        directory / "state.json",
        {
            "schema": "lockstep.codex-state/v1",
            "phase": "running",
            "supervisor_pid": os.getpid(),
        },
    )
    (directory / "supervisor-alive.lock").touch(mode=0o600)
    receipt = {"quiescent": True}
    observations = iter((None, receipt))
    expected = RunnerObservation(
        request.effect_id,
        request.request_digest,
        request.runner_binding_digest,
        "terminal",
    )
    monkeypatch.setattr(adapter, "_terminal_receipt", lambda _record: next(observations))
    monkeypatch.setattr(adapter, "_terminal", lambda _record, _receipt: expected)

    assert adapter.inspect(launch.effect_id) == expected


def test_sandbox_attestation_drift_blocks_launch(provider_system) -> None:
    from lockstep.runtime.providers.codex import CodexRunnerAdapter

    adapter, request, current, gate, workspaces, _snapshots, blobs = provider_system
    launch = adapter.prepare(request)

    class Drifted(FakeSandboxProvider):
        def preflight(self, policy):
            return replace(super().preflight(policy), policy_digest="0" * 64)

    drifted = CodexRunnerAdapter(
        owner_state_dir=adapter.owner_state_dir,
        installation=lambda: current["binding"],
        decision_gate=gate,
        workspaces=workspaces,
        blobs=blobs,
        sandbox=Drifted(),
    )
    with pytest.raises(Exception, match="attestation|policy"):
        drifted.ensure_started(launch)


def test_project_codex_control_surface_is_rejected_before_launch(provider_system) -> None:
    from lockstep.runtime.providers.base import DefinitiveProviderFailure

    adapter, request, _current, _gate, workspaces, snapshots, blobs = provider_system
    hostile = snapshots.capture(
        {
            "src/app.py": blobs.put(b"VALUE = 1\n"),
            ".codex/config.toml": blobs.put(b"[mcp_servers.hostile]\ncommand='payload'\n"),
        },
        declared_paths=("src/", ".codex/"),
        provenance={"source": "hostile-project"},
    )
    poisoned = _next_request(
        adapter,
        request,
        workspaces,
        effect_id="eff_project_control",
        brief="do work",
        snapshot_ref=f"snapshot:{hostile.digest}",
    )

    with pytest.raises(DefinitiveProviderFailure) as rejected:
        adapter.prepare(poisoned)
    assert rejected.value.result.fixed_error_code == "prelaunch_failed"
    assert adapter.spawn_count == 0
    workspace_ref = workspaces.workspace_ref_for(
        poisoned.effect_id, poisoned.intent_digest
    )
    assert workspaces.inspect(workspace_ref).phase == "released"


def test_rejected_workspace_output_becomes_terminal_error_and_stays_quarantined(
    provider_system,
) -> None:
    adapter, request, _current, _gate, workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)
    lease = workspaces.inspect(launch.workspace_ref)
    outside = lease.workspace_path.parent / "outside"
    outside.write_text("outside")
    (lease.workspace_path / "src" / "link").symlink_to(outside)
    directory = adapter._directory(request.effect_id)
    stdout = b'{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
    (directory / "stdout.bin").write_bytes(stdout)
    (directory / "stderr.bin").write_bytes(b"")
    adapter._atomic_json(
        directory / "terminal.json",
        {
            "schema": "lockstep.codex-terminal/v1",
            "returncode": 0,
            "overflow": False,
            "timed_out": False,
            "quiescent": True,
            "stdout_size": len(stdout),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_size": 0,
            "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        },
    )

    terminal = adapter.inspect(request.effect_id)
    assert terminal.state == "terminal"
    assert terminal.result.fixed_error_code == "writes_invalid"
    assert terminal.result.snapshot_ref is None
    assert workspaces.inspect(launch.workspace_ref).phase == "quarantined"
    safety = adapter.quiesce(request.effect_id)
    assert safety.state == "proven"
    assert safety.workspace_quarantined is True
    assert safety.rollover_snapshot_ref is None


def test_attempt_quota_bounds_retained_provider_metadata(provider_system) -> None:
    from lockstep.runtime.providers.codex import CodexCaptureLimits, CodexProviderError

    adapter, request, _current, _gate, workspaces, _snapshots, _blobs = provider_system
    adapter._limits = CodexCaptureLimits(max_retained_attempts=1)
    adapter.prepare(request)
    second = _next_request(
        adapter, request, workspaces, effect_id="eff_quota", brief="do work"
    )

    with pytest.raises(CodexProviderError, match="quota"):
        adapter.prepare(second)


def test_codex_mechanics_do_not_extend_generic_sandbox_contracts() -> None:
    assert "permission_profile_digest" not in SandboxPolicy.__dataclass_fields__
    assert "deployment_profile" not in SandboxPolicy.__dataclass_fields__
