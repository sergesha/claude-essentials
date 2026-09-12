import json
import subprocess
from pathlib import Path

import pytest


EXPECTED_DISTRIBUTABLE_FILES = frozenset(
    {
        "CHANGELOG.md",
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
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


def test_speciflow_distributable_contains_only_the_skill(repo_root: Path) -> None:
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
        if Path(path).relative_to("speciflow").parts[0] != "tests"
        and Path(path).relative_to("speciflow").as_posix()
        not in {"pyproject.toml", "uv.lock"}
    }

    assert shipped == EXPECTED_DISTRIBUTABLE_FILES
