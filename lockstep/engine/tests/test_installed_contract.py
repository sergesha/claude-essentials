from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
CONTROLLED_EFFECT = Path(__file__).parent / "fixtures/controlled_effect_executable.py"

ACTIVE_ROOT_PATHS = (
    ".claude-plugin",
    ".codex-plugin",
    ".mcp.json",
    "README.md",
    "docs/DESIGN.md",
    "hooks",
    "scripts",
    "skills",
    "engine/pyproject.toml",
    "engine/src/lockstep",
)
STAGED_DELIVERY_PATHS = (
    ".claude-plugin",
    ".codex-plugin",
    ".mcp.json",
    "README.md",
    "docs/DESIGN.md",
    "hooks",
    "scripts",
    "skills",
    "engine/pyproject.toml",
    "engine/uv.lock",
    "engine/src/lockstep",
)
RETIRED_BYTES = (
    b"_subcall",
    b"lockstep.subcalls",
    b"_subcall_wrapper.py",
    b"Subcalls (v2)",
    b"runners.yaml",
    b"LOCKSTEP_RUNNER",
    b"RunnerSpec",
    b"load_runners",
    b"peak_parallel_subcalls",
)
HISTORICAL_ONLY = (
    "CHANGELOG.md",
    "docs/superpowers/specs/2026-08-19-codex-claude-parity-design.md",
)


def _tracked() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, text=True, capture_output=True
    )
    return set(result.stdout.splitlines())


def _files(root: Path, paths: tuple[str, ...]) -> tuple[Path, ...]:
    found: list[Path] = []
    for relative in paths:
        target = root / relative
        if target.is_file():
            found.append(target)
        elif target.is_dir():
            found.extend(path for path in target.rglob("*") if path.is_file())
    if root == ROOT:
        tracked = _tracked()
        found = [path for path in found if path.relative_to(root).as_posix() in tracked]
    return tuple(sorted(found))


def _assert_active_bytes_are_retired(root: Path, paths: tuple[str, ...]) -> None:
    violations: list[str] = []
    for path in _files(root, paths):
        content = path.read_bytes()
        lower = content.lower()
        matched = [term.decode() for term in RETIRED_BYTES if term in content]
        if b"fractal" in lower or b"subcall" in lower:
            matched.append("active fractal/subcall prose")
        if matched:
            violations.append(
                f"{path.relative_to(root)}: {', '.join(sorted(set(matched)))}"
            )
        for historical in HISTORICAL_ONLY:
            if historical.encode() in content:
                violations.append(
                    f"{path.relative_to(root)} links historical-only {historical}"
                )
    assert violations == []


def _assert_active_guidance(root: Path) -> None:
    for relative in (
        "README.md",
        "docs/DESIGN.md",
        "skills/lockstep/SKILL.md",
        "skills/lockstep-author/SKILL.md",
    ):
        text = (root / relative).read_text().lower()
        assert "local unsandboxed" in text, relative
        assert "single-user" in text, relative
        assert "marker-free" in text and "manual yamlgraph" in text, relative
        assert "reviewed-change" in text and "parallel-review" in text, relative
        assert "configuration" in text and "not authority" in text, relative
        assert "report" in text and "not authority" in text, relative


def _clean_env(**updates: str) -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
        "UV_PROJECT_ENVIRONMENT",
    ):
        env.pop(name, None)
    env.update(updates)
    return env


_FLOW_PROBE = textwrap.dedent(
    r"""
    import hashlib
    import importlib.util
    import json
    import logging
    import os
    import time
    from pathlib import Path

    from lockstep.runtime import sessions
    from lockstep.runtime.effects.models import AcceptDescriptor
    from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
    from lockstep.runtime.effects.owner_provisioning import provision_runtime_snapshot
    from lockstep.runtime.engine import Engine
    from lockstep.runtime.errors import LockstepError
    from lockstep.runtime.providers.codex import CodexRunnerAdapter
    from lockstep.runtime.service import preflight_recipe
    from lockstep.templates import install_template

    root = Path(os.environ["LOCKSTEP_PROBE_ROOT"])
    controlled = Path(os.environ["LOCKSTEP_CONTROLLED_EFFECT"]).resolve(strict=True)
    logging.disable(logging.CRITICAL)

    def wait(command, project, run_id, predicate, timeout=30.0):
        projection = Engine.observe(command.state_dir, command.recipes_dir)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if command._pump_failure is not None:
                    raise command._pump_failure
                value = projection.status(run_id, str(project))
                if predicate(value):
                    return value
                time.sleep(0.02)
        finally:
            projection.close()
        raise AssertionError(f"timed out waiting for run {run_id}")

    def config(runtime_root, parallel=False):
        runtime_root.mkdir(parents=True)
        codex_home = runtime_root / "codex-home"
        pinned_home = runtime_root / "pinned-home"
        private_tmp = runtime_root / "tmp"
        for path in (codex_home, pinned_home, private_tmp):
            path.mkdir(mode=0o700)
        auth = codex_home / "auth.json"
        auth.write_text("{}")
        auth.chmod(0o600)
        if parallel:
            (private_tmp / "lockstep-controlled-two-process-barrier").mkdir()
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(private_tmp),
        }
        common = {
            "executable": str(controlled),
            "model": "task12c-installed-contract",
            "cli_version": "task12c-installed-contract",
            "permission_profile": {"sandbox": "workspace-write", "approval": "never"},
            "environment": environment,
        }
        return {
            "codex": {**common, "codex_home": str(codex_home)},
            "pinned": {
                **common,
                "codex_home": str(pinned_home),
                "pinned_permission_profile": "task12c-installed-pinned",
            },
        }

    def provision(project, recipe, owner, runtime_root, parallel=False):
        recipes = project / ".lockstep/recipes"
        authorized = preflight_recipe(recipes, recipe)
        index = RuntimeRequirementIndex.for_authorized_closures(
            (authorized,), project_identity=str(project.resolve())
        )
        bindings = config(runtime_root, parallel=parallel)
        provision_runtime_snapshot(
            state_dir=owner,
            codex=bindings["codex"],
            pinned=bindings["pinned"],
            replacement_keys=tuple(item.grant_selection_key for item in index.requirements),
            index=index,
            project=project,
        )
        return index

    def pending_accept(command, project, run_id):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if command._pump_failure is not None:
                raise command._pump_failure
            with command._admission_recovery_lock:
                command.runtime.bind(command.catalog.get(run_id))
                snapshot = command.runtime.snapshot(run_id, subgraphs=True)
            descriptors = [
                descriptor
                for interrupt in snapshot.pending
                if isinstance(
                    descriptor := command._protected_interrupt_descriptor(interrupt),
                    AcceptDescriptor,
                )
            ]
            if descriptors:
                assert len(descriptors) == 1
                return descriptors[0].logical_id
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for acceptance in run {run_id}")

    def accept(command, project, run_id, step):
        preview = command.preview_publication_consent(run_id, step, project=str(project))
        issued = command.issue_publication_consent(
            run_id, step, preview["digest"], project=str(project)
        )
        assert command.authority.inspect_token(issued.token).receipt_digest is None
        command.scenario_accept_artifact(issued.token, project=str(project))
        assert command.authority.inspect_token(issued.token).receipt_digest is not None
        return issued.token

    def assert_observations(owner, recipes, project, run_id, terminal=False):
        deadline = time.monotonic() + 10
        last_error = None
        while time.monotonic() < deadline:
            projection = Engine.observe(owner, recipes)
            try:
                status = projection.status(run_id, str(project))
                assert projection.history(run_id, str(project))
                assert projection.events(run_id, str(project))
                waited = projection.wait(run_id, 1, str(project))
                assert isinstance(waited["changed"], bool) and waited["revision"]
                if terminal:
                    assert status["status"] == "completed"
                return
            except LockstepError as exc:
                last_error = exc
                time.sleep(0.02)
            finally:
                projection.close()
        raise AssertionError("public observations did not become readable") from last_error

    def reviewed():
        base = root / "reviewed"
        project = base / "project"
        project.mkdir(parents=True)
        install_template("reviewed-change", "release", project, state_dir=base / "authoring")
        recipes = project / ".lockstep/recipes"
        index = provision(project, "release", base / "owner", base / "runtime")
        assert sorted(item.runner_selector for item in index.requirements) == ["codex", "pinned"]
        command = Engine.command(base / "owner", recipes)
        try:
            run_id = command.start("release", {}, str(project))["run_id"]
            wait(
                command,
                project,
                run_id,
                lambda item: item.get("owner") == "worker"
                and item.get("step") == "plan",
            )
            session = "installed-reviewed-worker"
            assert sessions.touch(base / "owner", run_id, session, 30) == "bound"
            plan = project / ".lockstep/plan.md"
            plan.write_text("# Goal\nShip.\n\n# Acceptance Criteria\nPass.\n\n# Steps\nReview.\n")
            command.scenario_done(run_id, "plan", {"path": ".lockstep/plan.md"}, session_id=session, project=str(project))
            pinned_marker = base / "pinned.marker"
            tests = project / "tests"
            tests.mkdir()
            (tests / "test_installed.py").write_text(
                "from pathlib import Path\n\n"
                "def test_installed_pinned():\n"
                f"    Path({str(pinned_marker)!r}).write_bytes(b'pinned verified\\n')\n"
            )
            wait(
                command,
                project,
                run_id,
                lambda item: item.get("owner") == "worker"
                and item.get("step") == "tests",
            )
            command.scenario_done(run_id, "tests", {"path": "tests/test_installed.py"}, session_id=session, project=str(project))
            source = project / "src"
            source.mkdir()
            (source / "installed.py").write_text("VALUE = 4\n")
            wait(
                command,
                project,
                run_id,
                lambda item: item.get("owner") == "worker"
                and item.get("step") == "implement",
            )
            command.scenario_done(run_id, "implement", {"path": "src/installed.py"}, session_id=session, project=str(project))
            step = pending_accept(command, project, run_id)
            binding = command.catalog.get(run_id)
            records = command.effects.list_for_thread(binding.thread_id)
            managed = [record for record in records if record.effect_kind == "managed"]
            pinned = [record for record in records if record.effect_kind == "verify"]
            assert len(managed) == len(pinned) == 1
            assert managed[0].phase == pinned[0].phase == "delivered"
            assert pinned_marker.read_bytes() == b"pinned verified\n"
            artifact = command.artifacts.read(managed[0].result.artifact_refs[0])
            expected = command.blobs.read(artifact.blob)
            assert expected.startswith(b"# Findings\nControlled evidence-backed review.\n")
            assert not (project / ".lockstep/review.md").exists()
            assert type(command._runtime_execution_composition.runners.codex) is CodexRunnerAdapter
            assert command._runtime_execution_composition.runners.codex.spawn_count == 1
            assert command._runtime_execution_composition.runners.pinned.spawn_count == 1
            assert_observations(base / "owner", recipes, project, run_id)
        finally:
            command.close()
        reopened = Engine.command(base / "owner", recipes)
        try:
            reopened.scenario_recover(str(project), limit=128)
            accept(reopened, project, run_id, step)
            wait(reopened, project, run_id, lambda item: item.get("status") == "completed")
            assert (project / ".lockstep/review.md").read_bytes() == expected
            assert reopened._runtime_execution_composition.runners.codex.spawn_count == 0
            binding = reopened.catalog.get(run_id)
            acceptance = [
                record for record in reopened.effects.list_for_thread(binding.thread_id)
                if record.effect_kind == "accept"
            ]
            assert len(acceptance) == 1 and acceptance[0].result is not None
            assert acceptance[0].result.receipt_digest is not None
            assert_observations(base / "owner", recipes, project, run_id, terminal=True)
        finally:
            reopened.close()

    def parallel():
        base = root / "parallel"
        project = base / "project"
        project.mkdir(parents=True)
        (project / "tracked.txt").write_text("parallel bytes\n")
        install_template("parallel-review", "release", project, state_dir=base / "authoring")
        recipes = project / ".lockstep/recipes"
        index = provision(project, "release", base / "owner", base / "runtime", parallel=True)
        assert len(index.requirements) == 2
        assert {item.runner_selector for item in index.requirements} == {"codex"}
        command = Engine.command(base / "owner", recipes)
        try:
            run_id = command.start("release", {}, str(project))["run_id"]
            first_step = pending_accept(command, project, run_id)
            binding = command.catalog.get(run_id)
            managed = [record for record in command.effects.list_for_thread(binding.thread_id) if record.effect_kind == "managed"]
            assert len(managed) == 2 and all(record.phase == "delivered" for record in managed)
            assert command._runtime_execution_composition.runners.codex.spawn_count == 2
            artifacts = [command.artifacts.read(record.result.artifact_refs[0]) for record in managed]
            assert {item.source_path for item in artifacts} == {"security-review.md", "architecture-review.md"}
            intervals = []
            expected = {}
            for artifact in artifacts:
                content = command.blobs.read(artifact.blob)
                expected[artifact.source_path] = content
                lines = content.decode().splitlines()
                intervals.append((int(next(line for line in lines if line.startswith("started_ns: ")).split()[1]), int(next(line for line in lines if line.startswith("ended_ns: ")).split()[1])))
            assert max(start for start, _ in intervals) < min(end for _, end in intervals)
            first_token = accept(command, project, run_id, first_step)
            second_step = pending_accept(command, project, run_id)
            assert second_step != first_step
        finally:
            command.close()
        reopened = Engine.command(base / "owner", recipes)
        try:
            reopened.scenario_recover(str(project), limit=128)
            second_token = accept(reopened, project, run_id, second_step)
            assert first_token != second_token
            wait(reopened, project, run_id, lambda item: item.get("status") == "completed")
            for source_path, content in expected.items():
                assert (project / ".lockstep" / source_path).read_bytes() == content
            assert reopened._runtime_execution_composition.runners.codex.spawn_count == 0
            with reopened._admission_recovery_lock:
                reopened.runtime.bind(reopened.catalog.get(run_id))
                history = tuple(reopened.runtime.history(run_id))
            join_schedules = [
                snapshot for snapshot in history
                if len(snapshot.next) == 1
                and snapshot.next[0].startswith("parallel-0-join-")
            ]
            assert len(join_schedules) == 1
            joined_values = {
                json.dumps(snapshot.values["reviews_result"], sort_keys=True)
                for snapshot in history if "reviews_result" in snapshot.values
            }
            assert joined_values == {'{"outcome": "PASS", "value": "pass"}'}
            assert_observations(base / "owner", recipes, project, run_id, terminal=True)
        finally:
            reopened.close()

    def manual():
        base = root / "manual"
        project = base / "project"
        recipes = project / ".lockstep/recipes"
        recipes.mkdir(parents=True)
        source = project / "src"
        source.mkdir()
        (source / "manual.py").write_text("VALUE = 'manual'\n")
        pinned_marker = base / "pinned.marker"
        tests = project / "tests"
        tests.mkdir()
        (tests / "test_manual.py").write_text(
            "from pathlib import Path\n\n"
            "def test_manual_pinned():\n"
            f"    Path({str(pinned_marker)!r}).write_bytes(b'manual pinned verified\\n')\n"
        )
        recipe = recipes / "manual.recipe.yaml"
        recipe.write_text(
            "name: manual\n"
            "state: {command: dict, work_request: dict, work_result: dict, request: dict, result: dict, lockstep_outcome: str}\n"
            "nodes:\n"
            "  work:\n"
            "    type: interrupt\n    state_key: work_request\n    resume_key: work_result\n"
            "    idempotent: false\n    message:\n      lockstep_effect:\n"
            "        schema: lockstep.effect/v1\n        kind: manual\n"
            "        logical_id: manual-work\n        runner: null\n        inputs: {}\n"
            "        writes: [src/]\n        artifacts: []\n        deadline_seconds: null\n"
            "        scope_state_keys: []\n        result_schema: lockstep.effect-result/v1\n"
            "  command:\n    type: passthrough\n    output:\n      command:\n"
            "        schema: lockstep.pinned-command/v1\n"
            "        logical_argv: [python, -m, pytest, -q]\n"
            "        logical_cwd: .\n        result_source: exit\n"
            "  verify:\n    type: interrupt\n    state_key: request\n    resume_key: result\n"
            "    idempotent: false\n    message:\n      lockstep_effect:\n"
            "        schema: lockstep.effect/v1\n        kind: verify\n"
            "        logical_id: manual-tests\n"
            "        runner: {selector: pinned, required_capabilities: [bounded_result, sandbox, workspace]}\n"
            "        inputs: {command: {state_key: command}, snapshot: {runtime_key: current_project_snapshot}}\n"
            "        writes: []\n        artifacts: []\n        deadline_seconds: 120\n"
            "        scope_state_keys: []\n        result_schema: lockstep.effect-result/v1\n"
            "  done: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
            "edges:\n  - {from: START, to: work}\n  - {from: work, to: command}\n"
            "  - {from: command, to: verify}\n  - {from: verify, to: done}\n"
            "  - {from: done, to: END}\n"
        )
        assert b"x-lockstep-generated" not in recipe.read_bytes()
        index = provision(project, "manual", base / "owner", base / "runtime")
        assert len(index.requirements) == 1
        assert index.requirements[0].runner_selector == "pinned"
        command = Engine.command(base / "owner", recipes)
        try:
            result = command.start("manual", {}, str(project))
            run_id = result["run_id"]
            waiting = wait(command, project, run_id, lambda item: item.get("owner") == "worker")
            step = waiting["step"]
            assert_observations(base / "owner", recipes, project, run_id)
        finally:
            command.close()
        reopened = Engine.command(base / "owner", recipes)
        try:
            reopened.scenario_recover(str(project), limit=128)
            session = "installed-manual-worker"
            assert sessions.touch(base / "owner", run_id, session, 30) == "bound"
            reopened.scenario_done(
                run_id, step, {"path": "src/manual.py"},
                session_id=session, project=str(project),
            )
            wait(reopened, project, run_id, lambda item: item.get("status") == "completed")
            assert pinned_marker.read_bytes() == b"manual pinned verified\n"
            assert reopened._runtime_execution_composition.runners.pinned.spawn_count == 1
            assert_observations(base / "owner", recipes, project, run_id, terminal=True)
        finally:
            reopened.close()

    reviewed()
    parallel()
    manual()
    import lockstep
    import lockstep.templates
    from lockstep.mcp import server
    assert {"reviewed-change", "parallel-review"} == set(__import__("lockstep.templates", fromlist=["list_templates"]).list_templates())
    assert {"scenario_start", "scenario_wait", "scenario_events", "scenario_accept_artifact"} <= {tool.name for tool in server.app._tool_manager.list_tools()}
    print(json.dumps({
        "lockstep": str(Path(lockstep.__file__).resolve()),
        "templates": str(Path(lockstep.templates.__file__).resolve()),
        "legacy_runner_importable": importlib.util.find_spec("lockstep.runtime.runners") is not None,
    }, sort_keys=True))
    """
)


def _run_probe(python: Path, foreign: Path, controlled: Path) -> dict[str, object]:
    foreign.mkdir(parents=True)
    probe_root = foreign / "probe"
    result = subprocess.run(
        [str(python), "-I", "-c", _FLOW_PROBE],
        cwd=foreign,
        env=_clean_env(
            LOCKSTEP_PROBE_ROOT=str(probe_root),
            LOCKSTEP_CONTROLLED_EFFECT=str(controlled),
            LOCKSTEP_STATE_DIR=str(probe_root / "ambient-owner"),
        ),
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def _assert_cli_resource_contract(
    executable: Path, project: Path, env: dict[str, str]
) -> None:
    project.mkdir(parents=True, exist_ok=True)
    commands = (
        (("template", "list"), "parallel-review\nreviewed-change\n"),
        (("template", "show", "reviewed-change", "release"), None),
        (("template", "init", "reviewed-change", "release"), "initialized release\n"),
        (("recipe", "check", "release"), None),
        (("recipe", "estimate", "release", "--json"), None),
    )
    for args, exact_stdout in commands:
        result = subprocess.run(
            [str(executable), *args],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if exact_stdout is not None:
            assert result.stdout == exact_stdout
    shown = json.loads(
        subprocess.run(
            [str(executable), "template", "show", "parallel-review", "parallel"],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=True,
        ).stdout
    )
    assert shown["template"] == "parallel-review"
    assert len(shown["compile_order"]) == 3


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("wheel-dist")
    subprocess.run(["uv", "build", "--out-dir", str(output)], cwd=ENGINE, check=True)
    wheels = tuple(output.glob("lockstep-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_source_checkout_active_bytes_are_current() -> None:
    _assert_active_bytes_are_retired(ROOT, ACTIVE_ROOT_PATHS)
    assert not (ENGINE / "src/lockstep/runtime/runners.py").exists()


def test_source_checkout_active_guidance_describes_the_installed_contract() -> None:
    _assert_active_guidance(ROOT)


def test_source_checkout_runs_all_complete_public_flows_from_foreign_cwd(
    tmp_path: Path,
) -> None:
    observed = _run_probe(
        Path(sys.executable), tmp_path / "foreign-source", CONTROLLED_EFFECT
    )
    assert Path(str(observed["lockstep"])).is_relative_to(ENGINE)
    assert observed["legacy_runner_importable"] is False


def test_clean_wheel_isolated_install_contains_only_current_runtime_and_runs_full_flows(
    tmp_path: Path, built_wheel: Path
) -> None:
    venv = tmp_path / "wheel-venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "bin/python"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            str(python),
            str(built_wheel),
        ],
        cwd=tmp_path,
        env=_clean_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [str(venv / "bin/lockstep-dependency-install")],
        cwd=tmp_path,
        env=_clean_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    controlled = tmp_path / "controlled-effect"
    shutil.copy2(CONTROLLED_EFFECT, controlled)
    controlled.chmod(controlled.stat().st_mode | stat.S_IXUSR)
    _assert_cli_resource_contract(
        venv / "bin/lockstep",
        tmp_path / "foreign-wheel-cli",
        _clean_env(LOCKSTEP_STATE_DIR=str(tmp_path / "wheel-cli-state")),
    )
    observed = _run_probe(python, tmp_path / "foreign-wheel", controlled)
    for key in ("lockstep", "templates"):
        assert Path(str(observed[key])).is_relative_to(venv)
        assert not Path(str(observed[key])).is_relative_to(ROOT)
    with zipfile.ZipFile(built_wheel) as archive:
        names = archive.namelist()
        assert not any(name.endswith("lockstep/runtime/runners.py") for name in names)
        for name in names:
            if name.endswith((".py", ".md", ".yaml", ".json")):
                content = archive.read(name)
                assert all(term not in content for term in RETIRED_BYTES), name
    assert observed["legacy_runner_importable"] is False


def _stage_plugin(destination: Path) -> None:
    tracked = _tracked()
    copied: set[str] = set()
    for relative in STAGED_DELIVERY_PATHS:
        source = ROOT / relative
        assert source.exists(), relative
        if source.is_dir():
            prefix = relative.rstrip("/") + "/"
            selected = sorted(path for path in tracked if path.startswith(prefix))
            assert selected, relative
            for path in selected:
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / path, target)
                copied.add(path)
        else:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.add(relative)
    assert copied <= tracked


def test_staged_plugin_uses_only_tracked_delivery_paths_and_runs_full_flows(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "staged-plugin"
    _stage_plugin(stage)
    foreign = tmp_path / "foreign-plugin"
    foreign.mkdir()
    doctor_state = tmp_path / "doctor-state"
    env = _clean_env(UV_OFFLINE="1", LOCKSTEP_STATE_DIR=str(doctor_state))
    initialized = subprocess.run(
        [str(stage / "scripts/lockstep-plugin"), "recipe", "init", "doctor-probe"],
        cwd=foreign,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    doctor = subprocess.run(
        [str(stage / "scripts/lockstep-plugin"), "doctor"],
        cwd=foreign,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    _assert_cli_resource_contract(
        stage / "engine/.venv/bin/lockstep", foreign / "resource-contract", env
    )
    server = subprocess.Popen(
        [str(stage / "scripts/lockstep-plugin"), "serve"],
        cwd=foreign,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.5)
        assert server.poll() is None
    finally:
        server.terminate()
        server.communicate(timeout=10)
    python = stage / "engine/.venv/bin/python"
    controlled = tmp_path / "staged-controlled-effect"
    shutil.copy2(CONTROLLED_EFFECT, controlled)
    controlled.chmod(controlled.stat().st_mode | stat.S_IXUSR)
    observed = _run_probe(python, tmp_path / "foreign-staged-flow", controlled)
    assert (
        json.loads((stage / ".codex-plugin/plugin.json").read_text())["name"]
        == "lockstep"
    )
    assert (
        json.loads((stage / ".claude-plugin/plugin.json").read_text())["name"]
        == "lockstep"
    )
    for key in ("lockstep", "templates"):
        assert Path(str(observed[key])).is_relative_to(stage)
        assert not Path(str(observed[key])).is_relative_to(ROOT)
    _assert_active_bytes_are_retired(stage, ACTIVE_ROOT_PATHS)
    _assert_active_guidance(stage)
    assert observed["legacy_runner_importable"] is False
