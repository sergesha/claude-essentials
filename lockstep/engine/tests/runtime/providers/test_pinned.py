from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.effects.authority import EffectGrant
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.project_snapshots import ProjectSnapshotStore
from lockstep.runtime.providers.base import EffectRequest
from lockstep.runtime.providers.codex import CodexProviderError
from lockstep.runtime.providers.workspaces import (
    LocalGitWorkspaceProvider,
    WorkspaceError,
)


def _workspace(tmp_path: Path, *, purpose: str):
    owner = tmp_path / "owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    seed = snapshots.capture(
        {"src/app.py": blobs.put(b"VALUE = 1\n")},
        declared_paths=("src/",),
        provenance={"source": "pinned-test"},
    )
    provider = LocalGitWorkspaceProvider(owner, snapshots, blobs)
    workspace_ref = provider.workspace_ref_for("effect", "a" * 64)
    lease = provider.materialize(
        effect_id="effect",
        request_digest="b" * 64,
        workspace_ref=workspace_ref,
        input_snapshot_ref=f"snapshot:{seed.digest}",
        declared_writes=(),
        purpose=purpose,
    )
    return provider, lease


def test_no_publish_workspace_purpose_is_immutable(tmp_path: Path) -> None:
    provider, lease = _workspace(tmp_path, purpose="no_publish_operation")

    assert lease.purpose == "no_publish_operation"
    with pytest.raises(WorkspaceError, match="purpose|another request"):
        provider.materialize(
            effect_id=lease.effect_id,
            request_digest=lease.request_digest,
            workspace_ref=lease.workspace_ref,
            input_snapshot_ref=lease.input_snapshot_ref,
            declared_writes=lease.declared_writes,
            purpose="managed_output",
        )


def test_no_publish_quarantine_never_returns_successor_snapshot(tmp_path: Path) -> None:
    provider, lease = _workspace(tmp_path, purpose="no_publish_operation")
    (lease.workspace_path / "src/app.py").write_text("VALUE = 2\n")

    proof = provider.quarantine_no_publish(lease)

    assert proof.workspace_ref == lease.workspace_ref
    assert proof.purpose == "no_publish_operation"
    assert proof.workspace_quarantined is True
    assert proof.rollover_snapshot_ref is None
    assert provider.inspect(lease.workspace_ref).phase == "quarantined"


def _pinned_system(
    tmp_path: Path,
    *,
    result_source: str = "exit",
    effect_kind: str = "pinned",
):
    from lockstep.runtime.providers.codex import (
        CodexInstallationBinding,
        CodexLaunchDecisionGate,
        CodexSandboxAttestor,
    )
    from lockstep.runtime.providers.pinned import (
        PinnedCommandSpec,
        PinnedRunnerAdapter,
        pinned_runner_binding_digest,
    )

    owner = tmp_path / "owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    seed = snapshots.capture(
        {"src/app.py": blobs.put(b"VALUE = 1\n")},
        declared_paths=("src/",),
        provenance={"source": "pinned-test"},
    )
    executable = tmp_path / "fake-codex"
    executable.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    codex_home = owner / "codex-home"
    codex_home.mkdir(mode=0o700)
    private_tmp = owner / "tmp"
    private_tmp.mkdir(mode=0o700)
    binding = CodexInstallationBinding.capture(
        executable=executable,
        model="unused-for-pinned",
        cli_version="0.147.0-test",
        permission_profile={"sandbox": "workspace-write", "approval": "never"},
        codex_home=codex_home,
        environment={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(private_tmp),
        },
    )
    workspaces = LocalGitWorkspaceProvider(owner, snapshots, blobs)
    permission_profile = "lockstep-pinned"
    adapter = PinnedRunnerAdapter(
        owner_state_dir=owner,
        installation=lambda: binding,
        decision_gate=CodexLaunchDecisionGate(
            pinned_runner_binding_digest(binding.digest, permission_profile),
            generation=1,
        ),
        workspaces=workspaces,
        blobs=blobs,
        sandbox=CodexSandboxAttestor(cli_version=binding.cli_version),
        permission_profile=permission_profile,
    )
    spec = PinnedCommandSpec.build(
        logical_argv=("python", "-m", "pytest", "-q"),
        logical_cwd=".",
        result_source=result_source,
    )
    capabilities = ["workspace", "bounded_result", "sandbox"]
    if result_source != "exit":
        capabilities.append("result_stability")
    intent = EffectRequest.build(
        effect_id="eff_pinned",
        public_run_id="run-pinned",
        project_identity="project-pinned",
        definition_digest="a" * 64,
        coordinate=NativeCoordinate("thread", "checkpoint", "", "task", "interrupt"),
        descriptor_digest="b" * 64,
        effect_kind=effect_kind,
        runner_selector="pinned",
        runner_binding_digest=adapter.binding_digest,
        required_capabilities=tuple(capabilities),
        inputs=(
            ("command", spec.to_dict()),
            ("snapshot", f"snapshot:{seed.digest}"),
        ),
        writes=(),
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    grant = EffectGrant.build(
        intent,
        actor_binding_digest="c" * 64,
        required_authorities=("os_user_execution",),
        workspace_ref=workspaces.workspace_ref_for(
            intent.effect_id, intent.intent_digest
        ),
        parent_capability_generation=1,
        grant_generation=1,
        policy_epoch=1,
        config_epoch=1,
        approval_generation=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    return adapter, intent.bind_grant(grant), workspaces


def test_pinned_prepare_accepts_verify_without_rewriting_effect_kind(
    tmp_path: Path,
) -> None:
    adapter, request, _workspaces = _pinned_system(
        tmp_path,
        effect_kind="verify",
    )

    launch = adapter.prepare(request)

    assert launch.effect_id == request.effect_id
    assert request.effect_kind == "verify"


def test_pinned_prepare_rejects_managed_effect_kind(tmp_path: Path) -> None:
    adapter, request, _workspaces = _pinned_system(
        tmp_path,
        effect_kind="managed",
    )

    with pytest.raises(CodexProviderError, match="accepts only"):
        adapter.prepare(request)
    assert adapter.spawn_count == 0


def test_pinned_prepare_commits_safe_logical_and_exact_codex_sandbox_argv(
    tmp_path: Path,
) -> None:
    adapter, request, workspaces = _pinned_system(tmp_path)

    launch = adapter.prepare(request)
    record = adapter.launch_record(request.effect_id)

    assert launch == adapter.prepare(request)
    assert workspaces.inspect(launch.workspace_ref).purpose == "no_publish_operation"
    assert record.inner_argv == (
        str(record.executable_path),
        "sandbox",
        "--permission-profile",
        "lockstep-pinned",
        "--cd",
        str(record.workspace_path),
        "--include-managed-config",
        "--",
        "python",
        "-m",
        "pytest",
        "-q",
    )
    assert record.shell is False
    assert record.deployment_profile == "local_unsandboxed"


def test_pinned_exit_result_quarantines_and_never_publishes_workspace(
    tmp_path: Path,
) -> None:
    adapter, request, workspaces = _pinned_system(tmp_path)
    launch = adapter.prepare(request)
    adapter.ensure_started(launch)

    terminal = adapter.wait_terminal(request.effect_id, timeout=10)
    safety = adapter.quiesce(request.effect_id)

    assert terminal.state == "terminal"
    assert terminal.result.outcome == "PASS"
    assert terminal.result.result_ref is None
    assert terminal.result.snapshot_ref is None
    assert safety.state == "proven"
    assert safety.workspace_quarantined is True
    assert safety.rollover_snapshot_ref is None
    assert safety.result_stable is False
    assert workspaces.inspect(launch.workspace_ref).phase == "quarantined"


@pytest.mark.parametrize("result_source", ["file", "junit"])
def test_pinned_file_results_reject_without_real_stability_provider(
    tmp_path: Path, result_source: str
) -> None:
    adapter, request, _workspaces = _pinned_system(
        tmp_path, result_source=result_source
    )

    with pytest.raises(Exception, match="stability|exit-only"):
        adapter.prepare(request)
    assert adapter.spawn_count == 0


def test_pinned_spawn_failure_is_error_not_command_failure(tmp_path: Path) -> None:
    adapter, request, _workspaces = _pinned_system(tmp_path)
    record = adapter.launch_record(adapter.prepare(request).effect_id)

    result = adapter._parse_result(
        record,
        {
            "returncode": 127,
            "overflow": False,
            "timed_out": False,
            "termination_reason": "spawn_failed",
        },
        None,
    )

    assert result.outcome == "ERROR"
    assert result.fixed_error_code == "runner_failed"
