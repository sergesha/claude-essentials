import json
import subprocess
from pathlib import Path

import pytest


PROHIBITED_COMPONENT_KEYS = frozenset(
    {
        "agents",
        "apps",
        "commands",
        "experimental",
        "hooks",
        "lspServers",
        "mcpServers",
        "outputStyles",
    }
)

EXPECTED_DISTRIBUTABLE_FILES = frozenset(
    {
        "CHANGELOG.md",
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        "skills/speciflow/SKILL.md",
        "skills/speciflow/agents/openai.yaml",
        "skills/speciflow/references/operations.md",
        "skills/speciflow/references/ownership.md",
        "skills/speciflow/references/transitions.md",
        "skills/speciflow/references/storage.md",
        "skills/speciflow/references/diagnostics.md",
        "skills/speciflow/references/initialization.md",
        "skills/speciflow/references/installation.md",
        "skills/speciflow/scripts/storage.py",
    }
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_both_marketplaces_register_the_speciflow_package(repo_root: Path) -> None:
    claude = json.loads((repo_root / ".claude-plugin/marketplace.json").read_text())
    codex = json.loads((repo_root / ".agents/plugins/marketplace.json").read_text())

    claude_entry = next(item for item in claude["plugins"] if item["name"] == "speciflow")
    codex_entry = next(item for item in codex["plugins"] if item["name"] == "speciflow")
    assert claude_entry["source"] == "./speciflow"
    assert codex_entry["source"] == {"source": "local", "path": "./speciflow"}


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
        assert not PROHIBITED_COMPONENT_KEYS.intersection(manifest)

    codex = json.loads((repo_root / "speciflow/.codex-plugin/plugin.json").read_text())
    assert codex["skills"] == "./skills/"


def test_release_please_has_independent_speciflow_package(repo_root: Path) -> None:
    config = json.loads((repo_root / "release-please-config.json").read_text())
    package = config["packages"]["speciflow"]

    assert package == {
        "package-name": "speciflow",
        "changelog-path": "CHANGELOG.md",
        "initial-version": "0.1.0",
        "extra-files": [
            {"type": "json", "path": ".claude-plugin/plugin.json", "jsonpath": "$.version"},
            {"type": "json", "path": ".codex-plugin/plugin.json", "jsonpath": "$.version"},
        ],
    }

def test_invocation_is_explicit_on_both_hosts(repo_root: Path) -> None:
    policy = (repo_root / "speciflow/skills/speciflow/agents/openai.yaml").read_text()
    skill = (repo_root / "speciflow/skills/speciflow/SKILL.md").read_text()

    assert "allow_implicit_invocation: false" in policy
    assert "disable-model-invocation: true" in skill


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


def test_storage_helper_is_small_and_has_no_locator_protocol(repo_root: Path) -> None:
    helper = repo_root / "speciflow/skills/speciflow/scripts/storage.py"
    substantive = [
        line for line in helper.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(substantive) <= 100
    text = helper.read_text()
    for removed in ("Locator", "InitPreview", "approved_preview", "locator-v1", "fcntl"):
        assert removed not in text
