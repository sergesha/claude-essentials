from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.effects.authority import EffectGrant
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.project_snapshots import ProjectSnapshotRef, ProjectSnapshotStore
from lockstep.runtime.providers.base import EffectRequest
from lockstep.runtime.providers.codex import (
    CodexInstallationBinding,
    CodexLaunchDecisionGate,
    CodexRunnerAdapter,
    CodexSandboxAttestor,
)
from lockstep.runtime.providers.workspaces import LocalGitWorkspaceProvider


def test_real_codex_managed_vertical_uses_disposable_git_project(
    tmp_path: Path,
) -> None:
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("Codex CLI is not installed")
    version = subprocess.run(
        [codex, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    codex_home_raw = os.environ.get("LOCKSTEP_CODEX_SMOKE_HOME")
    model = os.environ.get("LOCKSTEP_CODEX_SMOKE_MODEL")
    if codex_home_raw is None or model is None:
        pytest.skip("dedicated Codex smoke credentials/model are unavailable")
    codex_home = Path(codex_home_raw)
    if not (codex_home / "auth.json").is_file():
        pytest.skip("dedicated Codex smoke credential is unavailable")

    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    private_tmp = owner / "tmp"
    private_tmp.mkdir(mode=0o700)
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    seed = snapshots.capture(
        {"src/README.md": blobs.put(b"managed smoke input\n")},
        declared_paths=("src/",),
        provenance={"source": "real-codex-smoke"},
    )
    binding = CodexInstallationBinding.capture(
        executable=codex,
        model=model,
        cli_version=version,
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
    adapter = CodexRunnerAdapter(
        owner_state_dir=owner,
        installation=lambda: binding,
        decision_gate=CodexLaunchDecisionGate(binding.digest, generation=1),
        workspaces=workspaces,
        blobs=blobs,
        sandbox=CodexSandboxAttestor(cli_version=version),
    )
    coordinate = NativeCoordinate("thread", "checkpoint", "", "task", "interrupt")
    intent = EffectRequest.build(
        effect_id="real-codex-smoke",
        public_run_id="run-real-codex-smoke",
        project_identity="disposable-project",
        definition_digest="a" * 64,
        coordinate=coordinate,
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
        inputs=(
            (
                "brief",
                (
                    "Create src/smoke.txt containing exactly the text "
                    "'lockstep managed smoke' followed by one newline. "
                    "Do not modify any other project file."
                ),
            ),
            ("snapshot", f"snapshot:{seed.digest}"),
        ),
        writes=("src/",),
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
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
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    request = intent.bind_grant(grant)

    launch = adapter.prepare(request)
    assert launch.workspace_ref is not None
    workspace = workspaces.inspect(launch.workspace_ref)
    assert workspace.workspace_path.is_relative_to(tmp_path)
    assert (workspace.workspace_path / ".git").is_dir()
    adapter.ensure_started(launch)
    terminal = adapter.wait_terminal(request.effect_id, timeout=300)

    assert terminal.state == "terminal"
    assert terminal.result is not None
    assert terminal.result.outcome == "PASS"
    assert terminal.result.snapshot_ref is not None
    safety = adapter.quiesce(request.effect_id)
    assert safety.state == "proven"
    assert safety.rollover_snapshot_ref == terminal.result.snapshot_ref
    successor = snapshots.read(
        ProjectSnapshotRef(terminal.result.snapshot_ref.removeprefix("snapshot:"))
    )
    files = {entry.path: blobs.read(entry.blob) for entry in successor.files}
    assert files["src/smoke.txt"] == b"lockstep managed smoke\n"
    assert workspaces.inspect(launch.workspace_ref).phase == "released"
