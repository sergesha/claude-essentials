from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_real_codex_cli_smoke_uses_disposable_git_project(tmp_path: Path) -> None:
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("Codex CLI is not installed")
    version = subprocess.run(
        [codex, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if "0.147" not in version:
        pytest.skip(f"required Codex 0.147 capability is unavailable: {version}")

    project = tmp_path / "disposable-project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    assert (project / ".git").is_dir()
    assert project.is_relative_to(tmp_path)

