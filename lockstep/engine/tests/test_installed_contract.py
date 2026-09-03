from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
DELIVERY_EXCLUDES = (".superpowers/", "engine/tests/")

TERMINAL_RECIPE = """\
name: terminal
state: {lockstep_outcome: str}
nodes:
  done:
    type: passthrough
    output: {lockstep_outcome: PASS}
edges:
  - {from: START, to: done}
  - {from: done, to: END}
"""


def _clean_env(**updates: str) -> dict[str, str]:
    env = dict(os.environ)
    for name in tuple(env):
        if name.startswith("LOCKSTEP_") or name in {
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONUSERBASE",
            "UV_PROJECT_ENVIRONMENT",
            "VIRTUAL_ENV",
        }:
            env.pop(name)
    env.update(updates)
    return env


def _copy_tracked_plugin(stage: Path) -> None:
    repository = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", ROOT.name],
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    for raw in tracked:
        if not raw:
            continue
        relative = Path(os.fsdecode(raw)).relative_to(ROOT.name)
        logical = relative.as_posix()
        if logical.startswith(DELIVERY_EXCLUDES):
            continue
        source = repository / ROOT.name / relative
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _assert_installed_runtime(
    executable: Path,
    python: Path,
    project: Path,
    runtime_env: dict[str, str],
    expected_root: Path,
) -> None:
    recipes = project / ".lockstep/recipes"
    recipes.mkdir(parents=True, exist_ok=True)
    (recipes / "terminal.recipe.yaml").write_text(TERMINAL_RECIPE)
    result = subprocess.run(
        [str(executable), "recipe", "check", "terminal"],
        cwd=project,
        env=runtime_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    probe = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import importlib.resources,json,os;"
                "from pathlib import Path;"
                "import lockstep;"
                "from lockstep.runtime.engine import Engine;"
                "project=Path(os.environ['LOCKSTEP_TEST_PROJECT']);"
                "state=Path(os.environ['LOCKSTEP_STATE_DIR']);"
                "recipes=project/'.lockstep/recipes';"
                "command=Engine.command(state,recipes);"
                "started=command.start('terminal',{},str(project));"
                "command.close();"
                "reopened=Engine.command(state,recipes);"
                "reopened.scenario_recover(str(project));"
                "reopened.close();"
                "observer=Engine.observe(state,recipes);"
                "status=observer.status(started['run_id'],str(project));"
                "observer.close();"
                "print(json.dumps({'package':str(Path(lockstep.__file__).resolve()),"
                "'templates':str(Path(str(importlib.resources.files('lockstep.templates'))).resolve()),"
                "'status':status['status']}))"
            ),
        ],
        cwd=project,
        env={**runtime_env, "LOCKSTEP_TEST_PROJECT": str(project)},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    observed = json.loads(probe.stdout.splitlines()[-1])
    assert observed["status"] == "completed"
    assert Path(observed["package"]).is_relative_to(expected_root)
    assert Path(observed["templates"]).is_relative_to(expected_root)


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("wheel-dist")
    subprocess.run(
        ["uv", "build", "--out-dir", str(output)],
        cwd=ENGINE,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = tuple(output.glob("lockstep-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_installed_wheel_creates_workflow_from_foreign_directory(
    tmp_path: Path,
    built_wheel: Path,
) -> None:
    environment = tmp_path / "environment"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            str(environment / "bin/python"),
            str(built_wheel),
        ],
        cwd=tmp_path,
        env=_clean_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    patch_result = subprocess.run(
        [str(environment / "bin/lockstep-dependency-install")],
        cwd=tmp_path,
        env=_clean_env(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert patch_result.returncode == 0, patch_result.stdout + patch_result.stderr
    project = tmp_path / "project"
    project.mkdir()
    runtime_env = _clean_env(LOCKSTEP_STATE_DIR=str(tmp_path / "state"))

    result = subprocess.run(
        [str(environment / "bin/lockstep"), "recipe", "init", "smoke"],
        cwd=project,
        env=runtime_env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (project / ".lockstep/workflows/smoke.workflow.yaml").is_file()
    _assert_installed_runtime(
        environment / "bin/lockstep",
        environment / "bin/python",
        project,
        runtime_env,
        environment,
    )


def test_staged_plugin_creates_workflow_from_foreign_directory(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "plugin"
    _copy_tracked_plugin(stage)
    project = tmp_path / "project"
    project.mkdir()
    runtime_env = _clean_env(
        LOCKSTEP_STATE_DIR=str(tmp_path / "state"), UV_OFFLINE="1"
    )
    install = subprocess.run(
        [str(stage / "scripts/lockstep-install")],
        cwd=project,
        env=runtime_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    result = subprocess.run(
        [str(stage / "scripts/lockstep-plugin"), "recipe", "init", "smoke"],
        cwd=project,
        env=runtime_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=45,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (project / ".lockstep/workflows/smoke.workflow.yaml").is_file()
    _assert_installed_runtime(
        stage / "scripts/lockstep-plugin",
        stage / "engine/.venv/bin/python",
        project,
        runtime_env,
        stage,
    )
