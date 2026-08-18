import json
import os
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
        "shortDescription": "Evidence-gated workflows for coding agents",
        "longDescription": (
            "Run declarative engineering workflows with durable state, "
            "policy hooks, and deterministic artifact-backed evidence gates."
        ),
        "developerName": "sergesha",
        "category": "Developer Tools",
        "capabilities": ["Skills", "MCP server", "Policy hooks", "Runner subcalls"],
        "defaultPrompt": [
            "Use lockstep to run the requested workflow and validate its evidence."
        ],
    }


def test_codex_mcp_contract_is_pinned_to_plugin_root():
    server = _json(".mcp.json")["mcpServers"]["lockstep"]
    assert server == {
        "command": "./scripts/lockstep-plugin",
        "args": ["serve"],
        "env": {"LOCKSTEP_RUNNER": "codex"},
        "cwd": "./",
        "required": True,
        "default_tools_approval_mode": "approve",
        "startup_timeout_sec": 300,
        "tool_timeout_sec": 900,
    }


def test_claude_mcp_uses_launcher_and_literal_runner_default():
    server = _json(".claude-plugin/plugin.json")["mcpServers"]["lockstep"]
    assert server["command"] == "${CLAUDE_PLUGIN_ROOT}/scripts/lockstep-plugin"
    assert server["args"] == ["serve"]
    assert server["env"] == {"LOCKSTEP_RUNNER": "claude"}


def test_launcher_is_executable_and_does_not_change_directory():
    launcher = ROOT / "scripts/lockstep-plugin"
    assert launcher.stat().st_mode & stat.S_IXUSR
    source = launcher.read_text()
    assert source.startswith("#!/bin/sh\n")
    assert "cd " not in source
    assert "exec uv run --project" in source


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
        "run", "--project", str(ROOT / "engine"), "lockstep-mcp", "doctor",
    ]
