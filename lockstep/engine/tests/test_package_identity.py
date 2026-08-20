"""Public package and console-script identity contracts."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest


_OBSOLETE_IMPORT = "lockstep" + "_mcp"
_OBSOLETE_DISTRIBUTION = "lockstep" + "-mcp"


@dataclass
class CliResult:
    returncode: int
    stdout: str
    stderr: str
    mcp_initialized: bool


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def pyproject(repo_root: Path) -> dict:
    return tomllib.loads((repo_root / "engine/pyproject.toml").read_text())


def scan_runtime_sources(repo_root: Path, obsolete_names: tuple[str, ...]) -> list[str]:
    """Return executable/configuration files that retain a legacy public name."""
    paths = [
        *sorted((repo_root / "engine/src").rglob("*.py")),
        repo_root / ".mcp.json",
        repo_root / ".claude-plugin/plugin.json",
        repo_root / ".codex-plugin/plugin.json",
        repo_root / "hooks/hooks.json",
        repo_root / "scripts/lockstep-plugin",
    ]
    return [
        str(path.relative_to(repo_root))
        for path in paths
        if any(name in path.read_text() for name in obsolete_names)
    ]


@pytest.fixture
def cli() -> object:
    def run(*args: str, probe_startup: bool = False) -> CliResult:
        probe = ""
        if probe_startup:
            probe = "\nserver.app.run = lambda: print('MCP_INITIALIZED')\n"
        code = (
            "from lockstep import cli\nfrom lockstep.mcp import server\n"
            + probe
            + "raise SystemExit(cli.main())\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, *args], text=True, capture_output=True
        )
        return CliResult(
            result.returncode,
            result.stdout,
            result.stderr,
            "MCP_INITIALIZED" in result.stdout,
        )

    return run


def test_expected_console_scripts(pyproject):
    assert pyproject["project"]["name"] == "lockstep"
    assert pyproject["project"]["scripts"] == {
        "lockstep": "lockstep.bootstrap:main",
        "lockstep-dependency-install": "lockstep.dependency_patch:main",
    }


def test_executable_sources_have_no_obsolete_names(repo_root):
    hits = scan_runtime_sources(repo_root, (_OBSOLETE_IMPORT, _OBSOLETE_DISTRIBUTION))
    assert hits == []


def test_mcp_serving_is_explicit(cli):
    assert cli().returncode == 2
    assert "serve" in cli().stderr
    assert cli("serve", probe_startup=True).mcp_initialized
