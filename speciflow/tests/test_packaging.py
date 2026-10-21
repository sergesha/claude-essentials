import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("host", [".claude-plugin", ".codex-plugin"])
def test_installed_hook_blocks_with_spaces_in_plugin_path(tmp_path: Path, host: str) -> None:
    package = Path(__file__).resolve().parents[1]
    installed = tmp_path / "installed plugin"
    shutil.copytree(package / "scripts", installed / "scripts")
    shutil.copytree(package / "hooks", installed / "hooks")
    manifest = json.loads((package / host / "plugin.json").read_text())
    config = json.loads((installed / manifest["hooks"]).read_text())
    group = config["hooks"]["PreToolUse"][0]
    hook = group["hooks"][0]
    root = tmp_path / "workspace"
    (root / "backlog/tasks").mkdir(parents=True)
    (root / "backlog/config.yml").write_text("project_name: fixture\n")
    result = subprocess.run(
        hook["command"], shell=True, cwd=root,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(installed)},
        input=json.dumps({"hook_event_name": "PreToolUse", "cwd": str(root),
                          "tool_name": "Write", "tool_input": {"file_path": "backlog/tasks/new.md"}}),
        capture_output=True, text=True, timeout=hook["timeout"],
    )
    assert result.returncode == 2
    assert "SpeciFlow" in result.stderr and "Backlog CLI" in result.stderr
    assert not (root / "backlog/tasks/new.md").exists()


EXPECTED_DISTRIBUTABLE_FILES = frozenset(
    {
        "CHANGELOG.md",
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        "hooks/backlog-guard.json",
        "scripts/backlog_guard.py",
        "skills/speciflow/references/backlog-guard.md",
        "skills/speciflow/SKILL.md",
        "skills/speciflow/agents/openai.yaml",
        "skills/speciflow/references/grilling-integration.md",
        "skills/speciflow/references/wayfinding.md",
        "skills/speciflow/references/operations.md",
        "skills/speciflow/references/ownership.md",
        "skills/speciflow/references/transitions.md",
        "skills/speciflow/references/storage.md",
        "skills/speciflow/references/diagnostics.md",
        "skills/speciflow/references/initialization.md",
        "skills/speciflow/references/installation.md",
        "skills/speciflow/references/doctor.md",
        "skills/speciflow/references/examples.md",
        "skills/speciflow/references/iterative-planning.md",
        "skills/speciflow/scripts/storage.py",
    }
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_plugin_manifests_match_speciflow_release_version(repo_root: Path) -> None:
    release_manifest = json.loads((repo_root / ".release-please-manifest.json").read_text())
    expected_version = release_manifest["speciflow"]

    for relative in (
        "speciflow/.claude-plugin/plugin.json",
        "speciflow/.codex-plugin/plugin.json",
    ):
        manifest = json.loads((repo_root / relative).read_text())
        assert manifest["name"] == "speciflow"
        assert manifest["version"] == expected_version


def test_speciflow_distributable_contains_only_the_skill_and_guard(repo_root: Path) -> None:
    listed = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "speciflow",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    shipped = {
        Path(path).relative_to("speciflow").as_posix()
        for path in listed
        if (repo_root / path).is_file()
        and Path(path).relative_to("speciflow").parts[0] != "tests"
        and Path(path).relative_to("speciflow").as_posix()
        not in {"pyproject.toml", "uv.lock"}
    }

    assert shipped == EXPECTED_DISTRIBUTABLE_FILES
