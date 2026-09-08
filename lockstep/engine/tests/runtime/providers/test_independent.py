"""Independent local runners exercise the real durable attempt boundary."""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lockstep.runtime.blobs import BlobRef, BlobStore
from lockstep.runtime.effects.authority import EffectGrant
from lockstep.runtime.effects.models import PinnedCommandSpec
from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
from lockstep.runtime.effects.owner_policy import (
    RuntimeRequirement,
    grant_selection_key,
)
from lockstep.runtime.effects.owner_snapshot_store import (
    open_runtime_snapshot,
    replace_runtime_snapshot,
)
from lockstep.runtime.effects.owner_policy_ingress import (
    parse_runtime_provision_documents,
)
from lockstep.runtime.effects.owner_provisioning import provision_runtime_snapshot
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.project_snapshots import ProjectSnapshotRef, ProjectSnapshotStore
from lockstep.runtime.providers.base import EffectRequest
from lockstep.runtime.runtime_execution import (
    build_runtime_execution_composition,
    capture_runtime_execution_admission,
)


def _system(tmp_path: Path, selector: str, *, claude_body: str | None = None):
    project = tmp_path / "project"
    project.mkdir()
    private_tmp = tmp_path / "tmp"
    private_tmp.mkdir(mode=0o700)
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "TMPDIR": str(private_tmp),
    }
    config = {"schema": "lockstep.runtime-provision-config/v1"}
    if selector == "pinned":
        config["pinned"] = {"backend": "direct-local", "environment": env}
    else:
        executable = tmp_path / "claude"
        executable.write_text(f"#!{sys.executable}\n" + claude_body)
        executable.chmod(0o700)
        home = tmp_path / "native-home"
        home.mkdir(mode=0o700)
        config["claude"] = {
            "executable": str(executable),
            "model": "sonnet",
            "home": str(home),
            "environment": env,
        }
    parsed = parse_runtime_provision_documents(json.dumps(config).encode(), b"[]")
    index = RuntimeRequirementIndex(str(project), ())
    owner = tmp_path / "owner"
    provision_runtime_snapshot(
        state_dir=owner,
        codex=parsed.codex,
        pinned=parsed.pinned,
        claude=parsed.claude,
        replacement_keys=parsed.replacement_keys,
        index=index,
        project=project,
    )
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    seed = snapshots.capture(
        {"src/app.py": blobs.put(b"VALUE = 1\n")},
        declared_paths=("src/",),
        provenance={"source": "independent-test"},
    )
    context = capture_runtime_execution_admission(owner, index).context

    def adapter():
        return build_runtime_execution_composition(
            state_dir=owner,
            context=context,
            catalog=None,
            bundles=None,
            blobs=blobs,
            snapshots=snapshots,
        ).runners.resolve(selector)

    return adapter, blobs, seed, owner


def _request(
    adapter, seed, selector: str, *, argv: tuple[str, ...] = (), timeout: float = 20
):
    inputs = (("snapshot", f"snapshot:{seed.digest}"),)
    capabilities = ("workspace", "bounded_result", "sandbox")
    if selector == "pinned":
        inputs += (
            (
                "command",
                PinnedCommandSpec.build(
                    logical_argv=argv, logical_cwd="src", result_source="exit"
                ).to_dict(),
            ),
        )
    else:
        inputs += (("brief", "Change VALUE to 2 and report done"),)
        capabilities += ("credentials", "network")
    intent = EffectRequest.build(
        effect_id="eff_independent",
        public_run_id="run",
        project_identity="project",
        definition_digest="a" * 64,
        coordinate=NativeCoordinate("thread", "checkpoint", "", "task", "interrupt"),
        descriptor_digest="b" * 64,
        effect_kind="pinned" if selector == "pinned" else "managed",
        runner_selector=selector,
        runner_binding_digest=adapter.binding_digest,
        required_capabilities=capabilities,
        inputs=inputs,
        writes=() if selector == "pinned" else ("src/",),
        deadline_at=datetime.now(UTC) + timedelta(seconds=timeout),
    )
    grant = EffectGrant.build(
        intent,
        actor_binding_digest="c" * 64,
        required_authorities=("os_user_execution",),
        workspace_ref="workspace:" + "d" * 64,
        parent_capability_generation=1,
        grant_generation=1,
        policy_epoch=1,
        config_epoch=1,
        approval_generation=None,
        expires_at=intent.deadline_at,
    )
    return intent.bind_grant(grant)


def test_direct_pinned_executes_literal_argv_without_ai_cli_and_recovers(
    tmp_path: Path,
):
    factory, _blobs, seed, _owner = _system(tmp_path, "pinned")
    adapter = factory()
    marker = tmp_path / "shell-must-not-run"
    code = "import pathlib,sys; assert pathlib.Path.cwd().name == 'src'; print(sys.argv[1]); pathlib.Path('app.py').write_text('changed')"
    request = _request(
        adapter, seed, "pinned", argv=(sys.executable, "-c", code, f"$(touch {marker})")
    )
    launch = adapter.prepare(request)
    adapter.ensure_started(launch)
    result = adapter.wait_terminal(request.effect_id, timeout=10)
    assert result.result.outcome == "PASS"
    assert result.result.snapshot_ref is None
    assert not marker.exists()
    recovered = factory()
    assert recovered.inspect(request.effect_id).result == result.result
    assert recovered.ensure_started(launch).result == result.result
    assert recovered.spawn_count == 0


def test_claude_native_home_managed_result_and_workspace_without_codex(tmp_path: Path):
    body = """import json, os, pathlib, sys
assert "CODEX_HOME" not in os.environ
assert pathlib.Path(os.environ["HOME"]).name == "native-home"
assert "--bare" not in sys.argv
assert "--print" in sys.argv
assert sys.stdin.read() == "Change VALUE to 2 and report done"
pathlib.Path("src/app.py").write_text("VALUE = 2\\n")
print(json.dumps({"type":"result", "subtype":"success", "is_error":False, "result":"done"}))
"""
    factory, blobs, seed, owner = _system(tmp_path, "claude", claude_body=body)
    adapter = factory()
    request = _request(adapter, seed, "claude")
    adapter.ensure_started(adapter.prepare(request))
    result = adapter.wait_terminal(request.effect_id, timeout=10)
    assert result.result.outcome == "PASS"
    assert result.result.snapshot_ref is not None
    assert (
        blobs.read(BlobRef(result.result.result_ref.removeprefix("blob:"), 4))
        == b"done"
    )
    published = ProjectSnapshotStore(owner, blobs).read(
        ProjectSnapshotRef(result.result.snapshot_ref.removeprefix("snapshot:"))
    )
    assert blobs.read(published.files[0].blob) == b"VALUE = 2\n"
    assert factory().inspect(request.effect_id).result == result.result
    assert "auth.json" not in (owner / "runtime-owner/snapshot.json").read_text()


@pytest.mark.parametrize("backend", ["claude", "automatic", "codex"])
def test_unknown_pinned_backend_is_rejected(backend: str):
    config = {
        "schema": "lockstep.runtime-provision-config/v1",
        "pinned": {"backend": backend, "environment": {}},
    }
    with pytest.raises(ValueError):
        parse_runtime_provision_documents(json.dumps(config).encode(), b"[]")


@pytest.mark.parametrize(
    ("code", "outcome", "error"),
    [
        ("raise SystemExit(7)", "FAIL", None),
        ("import time; time.sleep(30)", "ERROR", "deadline_timeout"),
    ],
)
def test_direct_exit_and_deadline_preserve_terminal_disposition(
    tmp_path: Path, code, outcome, error
):
    factory, _blobs, seed, _owner = _system(tmp_path, "pinned")
    adapter = factory()
    request = _request(
        adapter, seed, "pinned", argv=(sys.executable, "-c", code), timeout=2
    )
    adapter.ensure_started(adapter.prepare(request))
    result = adapter.wait_terminal(request.effect_id, timeout=10)
    assert result.result.outcome == outcome
    assert result.result.fixed_error_code == error
    assert result.result.snapshot_ref is None


def test_direct_cancel_stops_running_command_without_relaunch(tmp_path: Path):
    factory, _blobs, seed, _owner = _system(tmp_path, "pinned")
    adapter = factory()
    marker = tmp_path / "started"
    code = f"import pathlib,time; pathlib.Path({str(marker)!r}).touch(); time.sleep(30)"
    request = _request(adapter, seed, "pinned", argv=(sys.executable, "-c", code))
    adapter.ensure_started(adapter.prepare(request))
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists()
    recovered = factory()
    recovered.cancel(request.effect_id)
    result = recovered.wait_terminal(request.effect_id, timeout=10)
    assert result.result.outcome != "PASS"
    assert recovered.quiesce(request.effect_id).workspace_quarantined
    assert recovered.spawn_count == 0


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "Failed to authenticate",
            "terminal_reason": "api_error",
        },
        {
            "type": "result",
            "subtype": "success",
            "result": "missing error discriminator",
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "wrong provider"},
        },
    ],
)
def test_claude_error_or_invalid_native_result_never_passes(tmp_path: Path, event):
    body = f"import sys; sys.stdin.read(); print({json.dumps(event)!r})\n"
    factory, _blobs, seed, _owner = _system(tmp_path, "claude", claude_body=body)
    adapter = factory()
    request = _request(adapter, seed, "claude")
    adapter.ensure_started(adapter.prepare(request))
    result = adapter.wait_terminal(request.effect_id, timeout=10)
    assert result.result.outcome == "ERROR"
    assert result.result.result_ref is None


def test_direct_executable_replacement_after_prepare_is_not_executed(tmp_path: Path):
    factory, _blobs, seed, _owner = _system(tmp_path, "pinned")
    executable = tmp_path / "command"
    executable.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
    executable.chmod(0o700)
    adapter = factory()
    request = _request(adapter, seed, "pinned", argv=(str(executable),))
    launch = adapter.prepare(request)
    marker = tmp_path / "replacement-executed"
    executable.write_text(
        f"#!{sys.executable}\nimport pathlib; pathlib.Path({str(marker)!r}).touch()\n"
    )
    adapter.ensure_started(launch)
    result = adapter.wait_terminal(request.effect_id, timeout=10)
    assert result.result.outcome == "ERROR"
    assert not marker.exists()


def _index(project: Path, selector: str):
    facts = dict(
        project_identity=str(project),
        definition_digest="a" * 64,
        protected_descriptor_digest="b" * 64,
        runner_selector=selector,
        required_capabilities=("bounded_result", "workspace"),
        required_authorities=("os_user_execution",),
    )
    requirement = RuntimeRequirement(
        grant_selection_key=grant_selection_key(**facts),
        **facts,
        uses=(("review.recipe.yaml", "review"),),
    )
    return RuntimeRequirementIndex(str(project), (requirement,))


def test_claude_only_snapshot_authorizes_exact_claude_requirement(tmp_path: Path):
    _factory, _blobs, _seed, owner = _system(
        tmp_path, "claude", claude_body="raise SystemExit(0)\n"
    )
    _digest, snapshot = open_runtime_snapshot(owner)
    index = _index(tmp_path / "project", "claude")
    replace_runtime_snapshot(
        directory=owner / "runtime-owner",
        codex=None,
        pinned=None,
        claude=snapshot.claude,
        index=index,
        replacement_keys=(index.requirements[0].grant_selection_key,),
    )
    admitted = capture_runtime_execution_admission(owner, index)
    assert admitted.context.bindings.codex_installation is None
    assert len(admitted.decision.requirements) == 1
    assert admitted.decision.requirements[0][0].runner_selector == "claude"


@pytest.mark.parametrize("selector", ["codex", "claude", "pinned"])
def test_missing_selected_provider_rejects_provision_before_state_creation(
    tmp_path: Path, selector
):
    project = tmp_path / "project"
    project.mkdir()
    owner = tmp_path / "owner"
    index = _index(project, selector)
    with pytest.raises(ValueError, match="requires .* binding"):
        provision_runtime_snapshot(
            state_dir=owner,
            codex=None,
            pinned=None,
            claude=None,
            index=index,
            project=project,
            replacement_keys=(),
        )
    assert not owner.exists()
