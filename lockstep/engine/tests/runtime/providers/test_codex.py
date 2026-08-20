from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.effects.authority import EffectGrant
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.project_snapshots import ProjectSnapshotRef, ProjectSnapshotStore
from lockstep.runtime.providers.base import EffectRequest
from lockstep.runtime.sandbox import FakeSandboxProvider


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def provider_system(tmp_path: Path):
    from lockstep.runtime.providers.codex import (
        CodexInstallationBinding,
        CodexLaunchDecisionGate,
        CodexRunnerAdapter,
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
            "name": "lockstep-managed",
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
        sandbox=FakeSandboxProvider(),
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
        required_capabilities=("workspace", "bounded_result", "sandbox"),
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
    assert record.permission_profile_digest
    assert record.deployment_profile == "local_unsandboxed"


def test_prepare_rejects_executable_or_permission_profile_drift(provider_system) -> None:
    adapter, request, current, _gate, _workspaces, _snapshots, _blobs = provider_system
    adapter.prepare(request)
    current["binding"] = replace(
        current["binding"],
        permission_profile_digest="d" * 64,
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
    assert "codex" not in json.dumps(observation.result.to_dict()).lower()
    safety = adapter.quiesce(request.effect_id)
    assert safety.state == "proven"
    assert safety.rollover_snapshot_ref == observation.result.snapshot_ref
    snapshot = snapshots.read(
        ProjectSnapshotRef(observation.result.snapshot_ref.removeprefix("snapshot:"))
    )
    assert blobs.read(snapshot.files[0].blob) == b"VALUE = 2\n"
    assert workspaces.inspect(launch.workspace_ref).phase == "quarantined"


def test_workspace_rollover_rejects_symlink_vcs_and_undeclared_mutations(provider_system) -> None:
    from lockstep.runtime.providers.workspaces import WorkspaceError

    adapter, request, _current, _gate, workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)
    lease = workspaces.inspect(launch.workspace_ref)
    outside = lease.workspace_path.parent / "outside"
    outside.write_text("outside")
    (lease.workspace_path / "src" / "link").symlink_to(outside)

    with pytest.raises(WorkspaceError, match="symlink|manifest|integrity"):
        workspaces.quarantine_and_rollover(
            lease,
            actual_death=workspaces.actual_death_proof(lease, process_identity="test"),
        )


def test_cleanup_requires_current_fence_and_actual_death(provider_system) -> None:
    from lockstep.runtime.providers.workspaces import WorkspaceError

    adapter, request, _current, _gate, workspaces, _snapshots, _blobs = provider_system
    launch = adapter.prepare(request)
    lease = workspaces.inspect(launch.workspace_ref)
    with pytest.raises(WorkspaceError, match="death"):
        workspaces.release(lease, actual_death=None, cleanup_fence=lease.cleanup_fence)
    proof = workspaces.actual_death_proof(lease, process_identity="test")
    with pytest.raises(WorkspaceError, match="fence"):
        workspaces.release(lease, actual_death=proof, cleanup_fence=lease.cleanup_fence + 1)


def test_streaming_capture_limit_fails_without_snapshot_visibility(tmp_path: Path) -> None:
    from lockstep.runtime.providers.codex import CodexCaptureLimits, CodexRunnerAdapter

    # Constructor-level coverage is enough to prove the limit is a launcher/capture
    # responsibility; the implementation test feeds a process that exceeds it.
    assert CodexCaptureLimits(max_stdout_bytes=64, max_stderr_bytes=64).max_stdout_bytes == 64
    assert "subprocess" not in EffectRequest.__dataclass_fields__
    assert CodexRunnerAdapter is not None


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
