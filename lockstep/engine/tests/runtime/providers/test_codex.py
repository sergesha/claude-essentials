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
from threading import Event, Thread

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


def test_prepare_owns_stable_independent_public_attempt_refs(provider_system) -> None:
    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system

    first = adapter.launch_record(adapter.prepare(request).effect_id)
    second = adapter.launch_record(adapter.prepare(request).effect_id)

    assert first.public_launch_ref == second.public_launch_ref
    assert first.start_ref == second.start_ref
    assert len(bytes.fromhex(first.public_launch_ref)) == 32
    assert len(bytes.fromhex(first.start_ref)) == 32
    assert first.public_launch_ref != first.start_ref
    assert first.public_launch_ref not in first.launch_ref
    assert first.start_ref not in first.launch_ref


def test_public_start_projection_is_invariant_to_private_candidate_dictionaries() -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    public = {
        "effect_id": "effect-safe",
        "public_launch_ref": "a" * 64,
        "start_ref": "b" * 64,
    }
    expected = supervisor._public_start_value(public)
    terminal = {
        **expected,
        "overflow": False,
        "quiescent": True,
        "returncode": 0,
        "termination_reason": "exited",
        "timed_out": False,
    }
    terminal.pop("schema")
    terminal["terminal_ref"] = hashlib.sha256(
        b"lockstep-public-terminal-v1\0"
        + json.dumps(terminal, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    public_surface = json.dumps(
        {"launch": expected, "spawn": expected, "terminal": terminal},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    for pid in range(1, 65_536):
        assert supervisor._public_start_value(
            {**public, "pid": pid, "pgid": pid}
        ) == expected
        private_started = json.dumps(
            {
                "schema": "lockstep.codex-started/v1",
                "pid": pid,
                "pgid": pid,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        private_hash = hashlib.sha256(private_started).hexdigest().encode()
        assert private_hash not in public_surface
    private_candidates = {
        "credential_identity_digest": [
            hashlib.sha256(f"credential-{index}".encode()).hexdigest()
            for index in range(128)
        ],
        "runner_binding_digest": [
            hashlib.sha256(f"runner-{index}".encode()).hexdigest()
            for index in range(128)
        ],
        "launch_ref": [f"codex:{index:064x}" for index in range(128)],
        "request_digest": [
            hashlib.sha256(f"request-{index}".encode()).hexdigest()
            for index in range(128)
        ],
        "stdout_sha256": [
            hashlib.sha256(f"output-{index}".encode()).hexdigest()
            for index in range(128)
        ],
        "stderr_sha256": [
            hashlib.sha256(f"stderr-{index}".encode()).hexdigest()
            for index in range(128)
        ],
    }
    for field, candidates in private_candidates.items():
        for candidate in candidates:
            assert supervisor._public_start_value(
                {**public, field: candidate}
            ) == expected
            encoded = candidate.encode()
            assert encoded not in public_surface
            assert hashlib.sha256(encoded).hexdigest().encode() not in public_surface
    assert not {
        "pid",
        "pgid",
        "credential_identity_digest",
        "runner_binding_digest",
        "launch_ref",
        "request_digest",
        "stdout_sha256",
        "stderr_sha256",
    } & set(expected)
    assert public_surface.count(public["public_launch_ref"].encode()) == 3
    assert public_surface.count(public["start_ref"].encode()) == 3
    assert b"lockstep-public-terminal-v1" not in public_surface


def test_supervisor_publishes_safe_write_once_fence_and_start_receipt(
    provider_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    record = adapter.launch_record(adapter.prepare(request).effect_id)
    body_path, body_digest = adapter._launch_body(record)
    directory = adapter._directory(request.effect_id)
    (directory / "go").write_bytes(b"start\n")

    class ImmediateExit:
        pid = 424242
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            return 0

    monkeypatch.setattr(supervisor, "_verify_bound_files", lambda _spec, _argv: None)
    monkeypatch.setattr(
        supervisor.subprocess, "Popen", lambda *_args, **_kwargs: ImmediateExit()
    )
    monkeypatch.setattr(supervisor, "_kill_group", lambda _process_group: None)
    monkeypatch.setattr(supervisor, "_group_is_dead", lambda _process_group: True)

    assert supervisor.run(body_path, body_digest) == 0
    fence = json.loads((directory / "spawn-fence.json").read_bytes())
    started = json.loads((directory / "public-start.json").read_bytes())
    assert fence == {
        "schema": "lockstep.codex-public-start/v1",
        "effect_id": request.effect_id,
        "public_launch_ref": record.public_launch_ref,
        "start_ref": record.start_ref,
    }
    assert started == {
        "schema": "lockstep.codex-public-start/v1",
        "effect_id": request.effect_id,
        "public_launch_ref": record.public_launch_ref,
        "start_ref": record.start_ref,
    }
    assert (directory / "spawn-fence.json").stat().st_mode & 0o777 == 0o600
    assert (directory / "public-start.json").stat().st_mode & 0o777 == 0o600
    assert "pid" not in started
    assert "pgid" not in started
    expected_bytes = (
        json.dumps(
            started,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    assert (directory / "spawn-fence.json").read_bytes() == expected_bytes
    assert (directory / "public-start.json").read_bytes() == expected_bytes
    assert adapter.inspect(request.effect_id).state == "terminal"
    corrupt = {**started, "start_ref": "f" * 64}
    (directory / "public-start.json").write_bytes(
        json.dumps(corrupt, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    assert adapter.inspect(request.effect_id).state == "indeterminate"


def test_supervisor_body_carries_and_revalidates_private_launch_joins(
    provider_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    record = adapter.launch_record(adapter.prepare(request).effect_id)
    body_path, body_digest = adapter._launch_body(record)
    body = json.loads(body_path.read_bytes())
    assert {
        "launch_ref": body["launch_ref"],
        "request_digest": body["request_digest"],
        "runner_binding_digest": body["runner_binding_digest"],
        "workspace_ref": body["workspace_ref"],
    } == {
        "launch_ref": record.launch_ref,
        "request_digest": record.request_digest,
        "runner_binding_digest": record.runner_binding_digest,
        "workspace_ref": record.workspace_ref,
    }

    launch_path = adapter._directory(request.effect_id) / "launch.json"
    raw = json.loads(launch_path.read_bytes())
    raw["request_digest"] = "f" * 64
    launch_path.chmod(0o600)
    launch_path.write_bytes(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    launch_path.chmod(0o400)
    directory = adapter._directory(request.effect_id)
    (directory / "go").write_bytes(b"start\n")
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("broken private join reached Popen"),
    )

    assert supervisor.run(body_path, body_digest) == 0
    assert not (directory / "spawn-fence.json").exists()


@pytest.mark.parametrize(
    ("fault_stage", "durable_final"),
    (
        ("stage-open", False),
        ("short-write", False),
        ("file-fsync", False),
        ("stage-close", False),
        ("link", False),
        ("final-reopen", True),
        ("final-close", True),
        ("directory-fsync", True),
        ("stage-unlink", True),
        ("collision", False),
        ("symlink", False),
    ),
)
def test_public_receipt_faults_recover_only_reopened_durable_final_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
    durable_final: bool,
) -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    path = tmp_path / "probe.json"
    value = {
        "schema": "lockstep.codex-public-start/v1",
        "effect_id": "effect",
        "public_launch_ref": "a" * 64,
        "start_ref": "b" * 64,
    }
    if fault_stage == "collision":
        path.write_bytes(b"{}\n")
        path.chmod(0o600)
    elif fault_stage == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"{}\n")
        path.symlink_to(outside)

    regular_closes = {"value": 0}
    original_open = supervisor.os.open
    original_close = supervisor.os.close
    original_fsync = supervisor.os.fsync
    original_link = supervisor.os.link
    original_unlink = supervisor.os.unlink
    original_verify = supervisor._verify_public_final

    def injected_open(target, flags, *args, **kwargs):
        if fault_stage == "stage-open" and str(target).startswith(".probe.json."):
            raise OSError("injected stage open")
        return original_open(target, flags, *args, **kwargs)

    def injected_close(descriptor):
        info = supervisor.os.fstat(descriptor)
        if stat.S_ISREG(info.st_mode):
            regular_closes["value"] += 1
            target = 1 if fault_stage == "stage-close" else 2
            if fault_stage in {"stage-close", "final-close"} and regular_closes["value"] == target:
                original_close(descriptor)
                raise OSError("injected regular close")
        return original_close(descriptor)

    def injected_fsync(descriptor):
        info = supervisor.os.fstat(descriptor)
        if fault_stage == "file-fsync" and stat.S_ISREG(info.st_mode):
            raise OSError("injected file fsync")
        if fault_stage == "directory-fsync" and stat.S_ISDIR(info.st_mode):
            raise OSError("injected directory fsync")
        return original_fsync(descriptor)

    def injected_link(*args, **kwargs):
        if fault_stage == "link":
            raise OSError("injected link")
        return original_link(*args, **kwargs)

    def injected_unlink(target, *args, **kwargs):
        if fault_stage == "stage-unlink" and str(target).startswith(".probe.json."):
            raise OSError("injected stage unlink")
        return original_unlink(target, *args, **kwargs)

    def injected_verify(*args, **kwargs):
        if fault_stage == "final-reopen":
            raise OSError("injected final reopen")
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(supervisor.os, "open", injected_open)
    monkeypatch.setattr(supervisor.os, "close", injected_close)
    monkeypatch.setattr(supervisor.os, "fsync", injected_fsync)
    monkeypatch.setattr(supervisor.os, "link", injected_link)
    monkeypatch.setattr(supervisor.os, "unlink", injected_unlink)
    monkeypatch.setattr(supervisor, "_verify_public_final", injected_verify)
    if fault_stage == "short-write":
        monkeypatch.setattr(
            supervisor,
            "_write_all",
            lambda *_args: (_ for _ in ()).throw(OSError("injected short write")),
        )

    with pytest.raises((OSError, ValueError, FileExistsError)):
        supervisor._publish_public_record(path, value)

    monkeypatch.undo()
    assert supervisor._public_record_matches(path, value) is durable_final


def _inject_live_public_start_stage_fault(
    monkeypatch: pytest.MonkeyPatch, supervisor, stage: str, directory: Path
) -> None:
    original_open = supervisor.os.open
    original_close = supervisor.os.close
    original_fsync = supervisor.os.fsync
    original_link = supervisor.os.link
    original_unlink = supervisor.os.unlink
    original_write_all = supervisor._write_all
    original_verify = supervisor._verify_public_final
    descriptor_targets = {}
    public_linked = {"value": False}

    def injected_open(target, flags, *args, **kwargs):
        if stage == "stage-open" and str(target).startswith(".public-start.json."):
            raise OSError("injected live stage open")
        descriptor = original_open(target, flags, *args, **kwargs)
        descriptor_targets[descriptor] = str(target)
        return descriptor

    def injected_close(descriptor):
        target = descriptor_targets.pop(descriptor, "")
        should_fail = (
            stage == "stage-close" and target.startswith(".public-start.json.")
        ) or (stage == "final-close" and target == "public-start.json")
        result = original_close(descriptor)
        if should_fail:
            raise OSError(f"injected live {stage}")
        return result

    def injected_fsync(descriptor):
        info = supervisor.os.fstat(descriptor)
        target = descriptor_targets.get(descriptor, "")
        if stage == "file-fsync" and target.startswith(".public-start.json."):
            raise OSError("injected live file fsync")
        if (
            stage == "directory-fsync"
            and stat.S_ISDIR(info.st_mode)
            and public_linked["value"]
        ):
            public_linked["value"] = False
            raise OSError("injected live directory fsync")
        return original_fsync(descriptor)

    def injected_write_all(descriptor, encoded):
        target = descriptor_targets.get(descriptor, "")
        if stage == "short-write" and target.startswith(".public-start.json."):
            raise OSError("injected live short write")
        return original_write_all(descriptor, encoded)

    def injected_link(source, destination, *args, **kwargs):
        if destination == "public-start.json":
            if stage == "link":
                raise OSError("injected live link")
            destination_fd = kwargs["dst_dir_fd"]
            if stage == "collision":
                descriptor = original_open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=destination_fd,
                )
                original_close(descriptor)
            elif stage == "symlink":
                supervisor.os.symlink(
                    "outside-start.json", destination, dir_fd=destination_fd
                )
            public_linked["value"] = True
        return original_link(source, destination, *args, **kwargs)

    def injected_unlink(target, *args, **kwargs):
        if str(target).startswith(".public-start.json.") and stage == "stage-unlink":
            raise OSError("injected live stage unlink")
        return original_unlink(target, *args, **kwargs)

    def injected_verify(directory_descriptor, basename, encoded):
        if stage == "final-reopen" and basename == "public-start.json":
            raise OSError("injected live final reopen")
        return original_verify(directory_descriptor, basename, encoded)

    (directory / "outside-start.json").write_bytes(b"outside\n")
    monkeypatch.setattr(supervisor.os, "open", injected_open)
    monkeypatch.setattr(supervisor.os, "close", injected_close)
    monkeypatch.setattr(supervisor.os, "fsync", injected_fsync)
    monkeypatch.setattr(supervisor.os, "link", injected_link)
    monkeypatch.setattr(supervisor.os, "unlink", injected_unlink)
    monkeypatch.setattr(supervisor, "_write_all", injected_write_all)
    monkeypatch.setattr(supervisor, "_verify_public_final", injected_verify)


@pytest.mark.parametrize(
    ("fault_stage", "durable_final"),
    (
        ("stage-open", False),
        ("short-write", False),
        ("file-fsync", False),
        ("stage-close", False),
        ("link", False),
        ("final-reopen", True),
        ("final-close", True),
        ("directory-fsync", True),
        ("stage-unlink", True),
        ("collision", False),
        ("symlink", False),
    ),
)
def test_every_public_start_stage_after_live_popen_contains_exactly_once(
    provider_system,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
    durable_final: bool,
) -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    record = adapter.launch_record(adapter.prepare(request).effect_id)
    body_path, body_digest = adapter._launch_body(record)
    directory = adapter._directory(request.effect_id)
    (directory / "go").write_bytes(b"start\n")
    popen_count = {"value": 0}
    alive = {"value": True}
    signals = []

    class Spawned:
        pid = 424242
        stdin = io.BytesIO()
        stdout = io.BytesIO(b"bounded stdout")
        stderr = io.BytesIO(b"bounded stderr")

        @staticmethod
        def poll():
            return None if alive["value"] else -9

        @staticmethod
        def wait():
            assert not alive["value"]
            return -9

    def popen(*_args, **_kwargs):
        popen_count["value"] += 1
        return Spawned()

    monotonic = {"value": 0.0}

    def advancing_monotonic():
        monotonic["value"] += 3.0
        return monotonic["value"]

    monkeypatch.setattr(supervisor, "_verify_bound_files", lambda *_args: None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)
    monkeypatch.setattr(supervisor.time, "monotonic", advancing_monotonic)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        supervisor,
        "_terminate_group",
        lambda *_args: signals.append("TERM"),
    )

    def kill(*_args):
        signals.append("KILL")
        alive["value"] = False

    monkeypatch.setattr(supervisor, "_kill_group", kill)
    monkeypatch.setattr(supervisor, "_group_is_dead", lambda *_args: not alive["value"])
    expected_value = supervisor._public_start_value(
        supervisor._read_spec(body_path, body_digest)
    )
    expected_fence = supervisor._canonical_document(expected_value)
    _inject_live_public_start_stage_fault(
        monkeypatch, supervisor, fault_stage, directory
    )

    assert supervisor.run(body_path, body_digest) == 0
    monkeypatch.undo()
    assert popen_count["value"] == 1
    assert signals[:2] == ["TERM", "KILL"]
    assert Spawned.stdin.closed and Spawned.stdout.closed and Spawned.stderr.closed
    assert (directory / "spawn-fence.json").read_bytes() == expected_fence
    assert supervisor._public_record_matches(
        directory / "public-start.json", expected_value
    ) is durable_final
    observation = adapter.inspect(request.effect_id)
    assert observation.state == ("terminal" if durable_final else "indeterminate")
    assert popen_count["value"] == 1


@pytest.mark.parametrize("final_was_linked", [False, True])
def test_public_start_failure_after_popen_contains_once_and_recovers_final_truth(
    provider_system,
    monkeypatch: pytest.MonkeyPatch,
    final_was_linked: bool,
) -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    record = adapter.launch_record(adapter.prepare(request).effect_id)
    body_path, body_digest = adapter._launch_body(record)
    directory = adapter._directory(request.effect_id)
    (directory / "go").write_bytes(b"start\n")
    popen_count = {"value": 0}

    class Spawned:
        pid = 424242
        stdin = io.BytesIO()
        stdout = io.BytesIO(b"discarded")
        stderr = io.BytesIO(b"discarded")

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            return -15

    def popen(*_args, **_kwargs):
        popen_count["value"] += 1
        return Spawned()

    publish = supervisor._publish_public_record

    def fail_public_start(path: Path, value: object) -> None:
        if path.name == "public-start.json":
            if final_was_linked:
                publish(path, value)
            raise OSError("injected public-start publication failure")
        publish(path, value)

    monkeypatch.setattr(supervisor, "_verify_bound_files", lambda _spec, _argv: None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)
    monkeypatch.setattr(supervisor, "_publish_public_record", fail_public_start)
    monkeypatch.setattr(supervisor, "_terminate_group", lambda *_args: None)
    monkeypatch.setattr(supervisor, "_kill_group", lambda _process_group: None)
    monkeypatch.setattr(supervisor, "_group_is_dead", lambda _process_group: True)

    assert supervisor.run(body_path, body_digest) == 0
    assert popen_count["value"] == 1
    assert Spawned.stdin.closed
    assert (directory / "spawn-fence.json").is_file()
    assert (directory / "public-start.json").is_file() is final_was_linked
    terminal = json.loads((directory / "public-terminal.json").read_bytes())
    assert terminal["termination_reason"] == "receipt_publication_failed"
    assert terminal["quiescent"] is True
    observation = adapter.inspect(request.effect_id)
    if final_was_linked:
        assert observation.state == "terminal"
        assert observation.result.fixed_error_code == "runner_failed"
    else:
        assert observation.state == "indeterminate"

    spec = supervisor._read_spec(body_path, body_digest)
    argv, environment = supervisor._launch_inputs(spec)
    process, _stdin, reason = supervisor._spawn_inner_process(
        spec, argv, environment, directory / "cancel"
    )
    assert process is None
    assert reason == "spawn_failed"
    assert popen_count["value"] == 1


def test_containment_retains_owner_until_group_is_dead_after_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    group_dead = Event()
    returned = Event()

    class Spawned:
        pid = 424242
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait():
            return -9

    spec = {
        "effect_id": "effect",
        "public_launch_ref": "a" * 64,
        "start_ref": "b" * 64,
        "stdout": str(tmp_path / "stdout.bin"),
        "stderr": str(tmp_path / "stderr.bin"),
        "terminal": str(tmp_path / "terminal.json"),
        "public_terminal": str(tmp_path / "public-terminal.json"),
    }
    monotonic = iter((0.0, 3.0, 3.0, 6.0))
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(monotonic, 6.0))
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(supervisor, "_terminate_group", lambda *_args: None)
    monkeypatch.setattr(supervisor, "_kill_group", lambda *_args: None)
    monkeypatch.setattr(supervisor, "_group_is_dead", lambda *_args: group_dead.is_set())

    def contain() -> None:
        supervisor._contain_receipt_publication_failure(
            spec, Spawned(), (False, Event(), (), [])
        )
        returned.set()

    worker = Thread(target=contain)
    worker.start()
    worker.join(0.05)
    assert worker.is_alive()
    assert not returned.is_set()
    group_dead.set()
    worker.join(1.0)
    assert not worker.is_alive()
    assert returned.is_set()
    terminal = json.loads((tmp_path / "public-terminal.json").read_bytes())
    assert terminal["quiescent"] is True


def test_symlinked_final_start_after_fence_is_indeterminate(
    provider_system, tmp_path: Path
) -> None:
    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    record = adapter.launch_record(adapter.prepare(request).effect_id)
    directory = adapter._directory(request.effect_id)
    start = {
        "schema": "lockstep.codex-public-start/v1",
        "effect_id": request.effect_id,
        "public_launch_ref": record.public_launch_ref,
        "start_ref": record.start_ref,
    }
    document = json.dumps(start, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    adapter._write_once(directory / "spawn-fence.json", document)
    outside = tmp_path / "outside-start.json"
    outside.write_bytes(document)
    (directory / "public-start.json").symlink_to(outside)

    assert adapter.inspect(request.effect_id).state == "indeterminate"


@pytest.mark.parametrize(
    "fault_stage",
    (
        "private-started",
        "capture-start",
        "monitor",
        "wait",
        "capture-finish",
        "terminal-publication",
    ),
)
def test_every_post_popen_stage_is_non_unwinding_and_never_replaced(
    provider_system,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
) -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    record = adapter.launch_record(adapter.prepare(request).effect_id)
    body_path, body_digest = adapter._launch_body(record)
    directory = adapter._directory(request.effect_id)
    (directory / "go").write_bytes(b"start\n")
    popen_count = {"value": 0}
    alive = {"value": fault_stage == "monitor"}
    containment_events: list[str] = []
    wait_calls = {"value": 0}
    finish_calls = {"value": 0}

    class Spawned:
        pid = 424242
        stdin = io.BytesIO()
        stdout = io.BytesIO(b"bounded")
        stderr = io.BytesIO()

        @staticmethod
        def poll():
            return None if alive["value"] else 0

        @staticmethod
        def wait():
            wait_calls["value"] += 1
            if fault_stage == "wait" and wait_calls["value"] == 1:
                raise OSError("injected wait failure")
            assert not alive["value"]
            containment_events.append("waited")
            return 0

    def popen(*_args, **_kwargs):
        popen_count["value"] += 1
        return Spawned()

    monkeypatch.setattr(supervisor, "_verify_bound_files", lambda _spec, _argv: None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)
    def terminate(*_args):
        containment_events.append("term")
        alive["value"] = False

    monkeypatch.setattr(supervisor, "_terminate_group", terminate)
    monkeypatch.setattr(supervisor, "_kill_group", lambda *_args: None)
    monkeypatch.setattr(supervisor, "_group_is_dead", lambda *_args: True)

    if fault_stage == "private-started":
        atomic = supervisor._atomic_json

        def fail_started(path: Path, value: object) -> None:
            if path.name == "started.json":
                raise OSError("injected private-started failure")
            atomic(path, value)

        monkeypatch.setattr(supervisor, "_atomic_json", fail_started)
    elif fault_stage == "capture-start":
        monkeypatch.setattr(
            supervisor,
            "_start_capture",
            lambda *_args: (_ for _ in ()).throw(OSError("injected capture start")),
        )
    elif fault_stage == "monitor":
        monkeypatch.setattr(
            supervisor,
            "_monitor_process",
            lambda *_args: (_ for _ in ()).throw(OSError("injected monitor")),
        )
    elif fault_stage == "capture-finish":
        finish_capture = supervisor._finish_capture

        def fail_finish_once(*args, **kwargs):
            finish_calls["value"] += 1
            if finish_calls["value"] == 1:
                raise OSError("injected capture finish")
            return finish_capture(*args, **kwargs)

        monkeypatch.setattr(
            supervisor,
            "_finish_capture",
            fail_finish_once,
        )
    elif fault_stage == "terminal-publication":
        monkeypatch.setattr(
            supervisor,
            "_publish_terminal",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("injected terminal publication")
            ),
        )

    assert supervisor.run(body_path, body_digest) == 0
    assert popen_count["value"] == 1
    assert Spawned.stdin.closed
    assert (directory / "spawn-fence.json").is_file()
    if fault_stage == "monitor":
        assert containment_events[:2] == ["term", "waited"]
        assert Spawned.stdout.closed and Spawned.stderr.closed


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

        def is_alive(self) -> bool:
            return False

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


def test_running_inspect_orders_terminal_adoption_after_liveness(
    provider_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)
    record = adapter.launch_record(request.effect_id)
    receipt = {"quiescent": True}
    expected = RunnerObservation(
        request.effect_id,
        request.request_digest,
        request.runner_binding_digest,
        "terminal",
    )
    events: list[str] = []

    def state(effect_id: str):
        assert effect_id == request.effect_id
        events.append("state")
        return {"phase": "running", "supervisor_pid": os.getpid()}

    def alive(observed_record, pid: int):
        assert observed_record == record
        assert pid == os.getpid()
        events.append("liveness")
        return False

    def terminal(observed_record):
        assert observed_record == record
        events.append("terminal")
        return receipt

    def validate(observed_record, observed_receipt):
        assert observed_record == record
        assert observed_receipt is receipt
        events.append("validation")
        return "legacy"

    def adopt(observed_record, observed_receipt, disposition):
        assert observed_record == record
        assert observed_receipt is receipt
        assert disposition == "legacy"
        events.append("adoption")
        return expected

    monkeypatch.setattr(adapter, "_state", state)
    monkeypatch.setattr(adapter, "_supervisor_ready", lambda _record: os.getpid())
    monkeypatch.setattr(adapter, "_supervisor_alive", alive)
    monkeypatch.setattr(adapter, "_terminal_receipt", terminal)
    monkeypatch.setattr(adapter, "_validate_public_receipts", validate)
    monkeypatch.setattr(adapter, "_adopt_public_terminal", adopt)

    assert adapter.inspect(launch.effect_id) == expected
    assert events == ["state", "liveness", "terminal", "validation", "adoption"]


@pytest.mark.parametrize("public", (False, True))
def test_running_inspect_defers_visible_terminal_until_liveness_release(
    provider_system, monkeypatch: pytest.MonkeyPatch, public: bool
) -> None:
    from lockstep.runtime.providers import _codex_supervisor as supervisor

    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)
    record = adapter.launch_record(request.effect_id)
    body_path, body_digest = adapter._launch_body(record)
    spec = supervisor._read_spec(body_path, body_digest)
    terminal_reads: list[object] = []
    validations: list[object] = []
    read_terminal = adapter._terminal_receipt
    validate_public = adapter._validate_public_receipts

    def terminal(observed_record):
        terminal_reads.append(observed_record)
        return read_terminal(observed_record)

    def validate(observed_record, observed_receipt):
        validations.append((observed_record, observed_receipt))
        return validate_public(observed_record, observed_receipt)

    monkeypatch.setattr(adapter, "_terminal_receipt", terminal)
    monkeypatch.setattr(adapter, "_validate_public_receipts", validate)

    with _ready_supervisor(adapter, request.effect_id) as directory:
        adapter._atomic_json(
            directory / "state.json",
            {
                "schema": "lockstep.codex-state/v1",
                "phase": "running",
                "supervisor_pid": os.getpid(),
            },
        )
        if public:
            start = supervisor._public_start_value(spec)
            supervisor._publish_public_record(directory / "spawn-fence.json", start)
            supervisor._publish_public_record(directory / "public-start.json", start)
        supervisor._publish_terminal(
            spec,
            returncode=0,
            overflow=False,
            timed_out=False,
            quiescent=True,
            termination_reason="exited",
            public=public,
        )

        assert adapter.inspect(launch.effect_id).state == "running"
        assert terminal_reads == []
        assert validations == []
        assert all(
            (directory / name).exists()
            for name in ("stdin.bin", "stdout.bin", "stderr.bin")
        )
        assert not (directory / "result.json").exists()

    assert adapter.inspect(launch.effect_id).state == "terminal"
    assert len(terminal_reads) == 1
    assert len(validations) == 1
    assert all(
        not (directory / name).exists()
        for name in ("stdin.bin", "stdout.bin", "stderr.bin")
    )


def test_running_inspect_without_terminal_is_indeterminate_after_liveness_release(
    provider_system,
) -> None:
    adapter, request, _current, _gate, _workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)

    with _ready_supervisor(adapter, request.effect_id) as directory:
        adapter._atomic_json(
            directory / "state.json",
            {
                "schema": "lockstep.codex-state/v1",
                "phase": "running",
                "supervisor_pid": os.getpid(),
            },
        )
        assert adapter.inspect(launch.effect_id).state == "running"

    assert adapter.inspect(launch.effect_id).state == "indeterminate"


@pytest.mark.parametrize("failure", ("malformed public terminal", "mismatching public start"))
def test_inspect_terminal_race_rejects_invalid_public_receipts(
    provider_system, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from lockstep.runtime.providers.codex import CodexProviderError

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

    def validate(_record, _receipt):
        assert _receipt is receipt
        raise CodexProviderError(failure)

    monkeypatch.setattr(adapter, "_terminal_receipt", lambda _record: receipt)
    monkeypatch.setattr(adapter, "_validate_public_receipts", validate)
    monkeypatch.setattr(
        adapter,
        "_terminal",
        lambda *_args: pytest.fail("invalid race receipts reached terminal adoption"),
    )

    with pytest.raises(CodexProviderError, match=failure):
        adapter.inspect(launch.effect_id)


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
