import json
import subprocess
import sys
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
        "skills/speciflow/references/installation.md",
        "skills/speciflow/scripts/storage.py",
    }
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run_helper(repo_root: Path, payload: bytes) -> subprocess.CompletedProcess[bytes]:
    helper = repo_root / "speciflow/skills/speciflow/scripts/storage.py"
    return subprocess.run(
        [sys.executable, str(helper)], input=payload, capture_output=True, check=False
    )


def test_both_marketplaces_register_the_speciflow_package(repo_root: Path) -> None:
    claude = json.loads((repo_root / ".claude-plugin/marketplace.json").read_text())
    codex = json.loads((repo_root / ".agents/plugins/marketplace.json").read_text())

    claude_entry = next(item for item in claude["plugins"] if item["name"] == "speciflow")
    codex_entry = next(item for item in codex["plugins"] if item["name"] == "speciflow")
    assert claude_entry["source"] == "./speciflow"
    assert codex_entry["source"] == {"source": "local", "path": "./speciflow"}


def test_plugin_manifests_are_independent_version_zero_two_zero(repo_root: Path) -> None:
    for relative in (
        "speciflow/.claude-plugin/plugin.json",
        "speciflow/.codex-plugin/plugin.json",
    ):
        manifest = json.loads((repo_root / relative).read_text())
        assert manifest["name"] == "speciflow"
        assert manifest["version"] == "0.2.0"
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

    release_manifest = json.loads((repo_root / ".release-please-manifest.json").read_text())
    assert release_manifest["speciflow"] == "0.2.0"


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


def test_private_resolve_transport_returns_one_json_result(
    repo_root: Path, tmp_path: Path
) -> None:
    run = run_helper(
        repo_root,
        json.dumps(
            {"op": "resolve", "cwd": str(tmp_path), "explicit_base": str(tmp_path)}
        ).encode(),
    )

    assert run.returncode == 0
    assert json.loads(run.stdout)["source"] == "explicit"
    assert run.stderr == b""


@pytest.mark.parametrize("payload", (b"\xff", b'{"op":'))
def test_private_transport_rejects_invalid_utf8_or_json_without_traceback(
    repo_root: Path, payload: bytes
) -> None:
    run = run_helper(repo_root, payload)

    assert run.returncode == 0
    assert json.loads(run.stdout) == {"error": "invalid_request"}
    assert run.stderr == b""


@pytest.mark.parametrize(
    ("path_key", "path_value"),
    (("explicit_base", "\x00"), ("cwd", "\x00"), ("explicit_base", []), ("cwd", [])),
)
def test_private_transport_rejects_invalid_paths_without_traceback(
    repo_root: Path, path_key: str, path_value: object
) -> None:
    run = run_helper(repo_root, json.dumps({"op": "resolve", path_key: path_value}).encode())

    assert run.returncode == 0
    assert json.loads(run.stdout) == {"error": "invalid_path"}
    assert run.stderr == b""


def test_private_transport_rejects_removed_rebind_operation(
    repo_root: Path, tmp_path: Path
) -> None:
    old_root = tmp_path / "old"
    old_root.mkdir()
    run = run_helper(
        repo_root,
        json.dumps(
            {
                "op": "resolve",
                "cwd": str(tmp_path),
                "explicit_base": str(tmp_path / "base"),
                "explicit_anchor": str(tmp_path),
                "rebind_data_root": str(old_root),
            }
        ).encode(),
    )

    assert run.returncode == 0
    assert json.loads(run.stdout) == {"error": "storage_conflict"}
    assert run.stderr == b""


def test_private_preview_is_canonical_and_storage_only(
    repo_root: Path, tmp_path: Path
) -> None:
    run = run_helper(
        repo_root,
        json.dumps(
            {"op": "preview", "cwd": str(tmp_path), "explicit_base": str(tmp_path)}
        ).encode(),
    )

    preview = json.loads(run.stdout)
    assert run.returncode == 0
    assert run.stdout == json.dumps(preview, sort_keys=True, separators=(",", ":")).encode()
    assert set(preview) == {
        "selection",
        "share_from_anchor",
        "locator_writes",
        "directories_to_create",
    }
    assert not {"planning", "backlog", "openspec", "beads", "commit"}.intersection(preview)


def test_private_init_accepts_only_the_exact_current_preview(
    repo_root: Path, tmp_path: Path
) -> None:
    request = {"op": "preview", "cwd": str(tmp_path), "explicit_base": str(tmp_path)}
    preview = json.loads(run_helper(repo_root, json.dumps(request).encode()).stdout)

    accepted = run_helper(
        repo_root,
        json.dumps({"op": "init", "approved_preview": preview}).encode(),
    )
    mismatch = run_helper(
        repo_root,
        json.dumps({"op": "init", "approved_preview": {"base": str(tmp_path)}}).encode(),
    )

    assert json.loads(accepted.stdout)["initialized"] is True
    assert json.loads(mismatch.stdout) == {"error": "preview_mismatch"}
    assert not (tmp_path / "planning").exists()
    assert not (tmp_path / "beads").exists()
