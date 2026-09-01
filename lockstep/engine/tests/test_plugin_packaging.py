import json
import os
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def test_host_manifests_share_identity_version_and_components():
    claude = _json(".claude-plugin/plugin.json")
    codex = _json(".codex-plugin/plugin.json")
    package = tomllib.loads((ROOT / "engine/pyproject.toml").read_text())

    assert claude["name"] == codex["name"] == "lockstep"
    assert claude["version"] == codex["version"] == package["project"]["version"]
    for key in ("description", "author", "homepage", "repository", "license"):
        assert claude[key] == codex[key]
    assert codex["skills"] == "./skills/"
    assert codex["mcpServers"] == "./.mcp.json"
    assert claude["hooks"] == "./hooks/hooks.json"
    assert codex["interface"] == {
        "displayName": "Lockstep",
        "shortDescription": "Native durable workflows for coding agents",
        "longDescription": (
            "Author and run deterministic yamlgraph/LangGraph workflows with "
            "evidence-gated external effects, native recovery, and artifact "
            "publication."
        ),
        "developerName": "sergesha",
        "category": "Developer Tools",
        "capabilities": [
            "Skills",
            "MCP server",
            "Policy hooks",
            "Native durable workflows",
            "External-effect bridging",
        ],
        "defaultPrompt": [
            (
                "Use lockstep to author or run the requested native workflow and "
                "validate its evidence."
            )
        ],
    }


def test_codex_mcp_contract_is_pinned_to_plugin_root():
    server = _json(".mcp.json")["mcpServers"]["lockstep"]
    assert server == {
        "command": "./scripts/lockstep-plugin",
        "args": ["serve"],
        "cwd": "./",
        "required": True,
        "default_tools_approval_mode": "approve",
        "startup_timeout_sec": 300,
        "tool_timeout_sec": 900,
        "env": {"LOCKSTEP_PLUGIN_HOST": "codex"},
    }


def test_claude_mcp_uses_launcher_without_legacy_runner_default():
    server = _json(".claude-plugin/plugin.json")["mcpServers"]["lockstep"]
    assert server["command"] == "${CLAUDE_PLUGIN_ROOT}/scripts/lockstep-plugin"
    assert server["args"] == ["serve"]
    assert "env" not in server


def test_launcher_is_executable_and_does_not_change_directory():
    launcher = ROOT / "scripts/lockstep-plugin"
    assert launcher.stat().st_mode & stat.S_IXUSR
    source = launcher.read_text()
    assert source.startswith("#!/bin/sh\n")
    assert "cd " not in source
    assert '"$lockstep_plugin_root/scripts/lockstep-install"' in source
    assert "exec uv run --project" in source
    assert "--no-sync lockstep" in source


def test_launcher_resolves_engine_but_preserves_caller_cwd(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PWD\"\n"
        "printf '%s\\n' \"$@\"\n"
    )
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR)
    project = tmp_path / "project"
    project.mkdir()
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

    result = subprocess.run(
        [str(ROOT / "scripts/lockstep-plugin"), "doctor"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    lines = result.stdout.splitlines()
    assert lines[0] == str(project)
    assert lines[1:] == [
        "sync", "--project", str(ROOT / "engine"), "--frozen",
        str(project),
        "run", "--project", str(ROOT / "engine"), "--no-sync",
        "lockstep-dependency-install",
        str(project),
        "run", "--project", str(ROOT / "engine"), "--no-sync", "lockstep", "doctor",
    ]


def test_launcher_derives_codex_home_from_installed_plugin_path(tmp_path):
    codex_home = tmp_path / "codex-home"
    plugin_root = (
        codex_home / "plugins/cache/claude-essentials/lockstep/0.1.0"
    )
    scripts_dir = plugin_root / "scripts"
    scripts_dir.mkdir(parents=True)
    (plugin_root / "engine").mkdir()
    launcher = scripts_dir / "lockstep-plugin"
    shutil.copy2(ROOT / "scripts/lockstep-plugin", launcher)
    shutil.copy2(ROOT / "scripts/lockstep-install", scripts_dir / "lockstep-install")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text("#!/bin/sh\nprintf '%s\\n' \"${CODEX_HOME-unset}\"\n")
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR)
    env = {
        **os.environ,
        "LOCKSTEP_PLUGIN_HOST": "codex",
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    env.pop("CODEX_HOME", None)

    result = subprocess.run(
        [str(launcher), "doctor"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert set(result.stdout.splitlines()) == {str(codex_home)}


def test_runtime_never_reads_the_non_authoritative_plugin_host_marker():
    runtime = ROOT / "engine/src/lockstep"
    readers = [
        str(path.relative_to(ROOT))
        for path in sorted(runtime.rglob("*.py"))
        if "LOCKSTEP_PLUGIN_HOST" in path.read_text()
    ]
    assert readers == []


def test_install_build_and_plugin_enforce_sync_patch_no_sync_order(tmp_path):
    """An implicit sync after patching would restore the vulnerable wheel."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$LOCKSTEP_TEST_LOG\"\n"
    )
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "uv.log"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "LOCKSTEP_TEST_LOG": str(log),
    }

    subprocess.run([str(ROOT / "scripts/lockstep-install")], env=env, check=True)
    assert log.read_text().splitlines() == [
        f"sync --project {ROOT / 'engine'} --frozen",
        f"run --project {ROOT / 'engine'} --no-sync lockstep-dependency-install",
    ]

    log.write_text("")
    subprocess.run([str(ROOT / "scripts/lockstep-build")], env=env, check=True)
    assert log.read_text().splitlines() == [
        f"sync --project {ROOT / 'engine'} --frozen",
        f"run --project {ROOT / 'engine'} --no-sync lockstep-dependency-install",
        f"build --project {ROOT / 'engine'}",
    ]

    log.write_text("")
    subprocess.run(
        [str(ROOT / "scripts/lockstep-plugin"), "doctor"],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    assert log.read_text().splitlines() == [
        f"sync --project {ROOT / 'engine'} --frozen",
        f"run --project {ROOT / 'engine'} --no-sync lockstep-dependency-install",
        f"run --project {ROOT / 'engine'} --no-sync lockstep doctor",
    ]


def test_runtime_skill_uses_host_neutral_worker_language():
    skill = (ROOT / "skills/lockstep/SKILL.md").read_text()
    assert "your own `Agent` tool" not in skill
    assert "host's subagent capability" in skill


def test_author_skill_does_not_document_retired_runner_configuration():
    skill = (ROOT / "skills/lockstep-author/SKILL.md").read_text()
    assert "driver: claude" not in skill
    assert "driver: codex" not in skill
    assert "LOCKSTEP_RUNNER" not in skill
    assert "Runner names" not in skill
