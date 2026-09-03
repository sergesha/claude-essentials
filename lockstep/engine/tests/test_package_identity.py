"""Public package and console-script identity contracts."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import tomllib


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
            [sys.executable, "-c", code, *args], text=True, capture_output=True, check=False
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


def test_mcp_serving_is_explicit(cli):
    assert cli().returncode == 2
    assert "serve" in cli().stderr
    assert cli("serve", probe_startup=True).mcp_initialized
