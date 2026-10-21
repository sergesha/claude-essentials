"""Active contracts are protected; pending OpenSpec changes remain editable."""

import json
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from test_backlog_guard import invoke, silent


@pytest.fixture
def active_workspace(tmp_path):
    root = tmp_path / "planning with spaces"
    active = root / "openspec/specs/export/spec.md"
    active.parent.mkdir(parents=True)
    (root / "openspec/changes/increase-wait/specs/export").mkdir(parents=True)
    (root / "openspec/config.yaml").write_text("schema: spec-driven\n")
    active.write_text("Exports SHALL finish within 30 seconds.\n")
    return root, active


def denied(result):
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stdout == ""
    assert all(word in result.stderr for word in ("SpeciFlow", "OpenSpec", "change", "CLI"))
    assert "Backlog" not in result.stderr


@pytest.mark.parametrize("tool,args", [
    ("Write", {"file_path": "openspec/specs/export/spec.md", "content": "lost"}),
    ("Edit", {"file_path": "openspec/specs/export/spec.md", "old_string": "30", "new_string": "45"}),
    ("MultiEdit", {"file_path": "openspec/specs/export/spec.md", "edits": []}),
    ("NotebookEdit", {"notebook_path": "openspec/specs/export/example.ipynb"}),
    ("apply_patch", {"command": "*** Begin Patch\n*** Update File: openspec/specs/export/spec.md\n@@\n-30\n+45\n*** End Patch"}),
    ("apply_patch", {"command": "*** Begin Patch\n*** Add File: openspec/specs/new/spec.md\n+new\n*** End Patch"}),
    ("apply_patch", {"command": "*** Begin Patch\n*** Delete File: openspec/specs/export/spec.md\n*** End Patch"}),
    ("apply_patch", {"command": "*** Begin Patch\n*** Update File: draft.md\n*** Move to: openspec/specs/export/spec.md\n@@\n-old\n+new\n*** End Patch"}),
])
def test_direct_active_spec_tools_receive_specific_reminder(active_workspace, tool, args):
    root, active = active_workspace
    original = active.read_bytes()
    denied(invoke(root, tool, args))
    assert active.read_bytes() == original


@pytest.mark.parametrize("command", [
    "printf lost > openspec/specs/export/spec.md",
    "tee openspec/specs/export/spec.md </dev/null",
    "sed -i '' s/30/45/ openspec/specs/export/spec.md",
    "sed 's/30/45/' -i openspec/specs/export/spec.md",
    "sed -n '1p' --in-place openspec/specs/export/spec.md",
    "sed 's/30/45/' -e 'w openspec/specs/export/spec.md' draft.md",
    "rm openspec/specs/export/spec.md",
    "mv openspec/specs/export/spec.md moved.md",
    "cp draft.md openspec/specs/export/spec.md",
    "rm -rf openspec/specs",
    "rm -rf openspec",
    "openspec archive increase-wait --yes; rm openspec/specs/export/spec.md",
    "openspec instructions apply --change increase-wait > openspec/specs/export/spec.md",
    "git diff --output=openspec/specs/export/spec.md",
    "cat openspec/config.yaml;\nrm openspec/specs/export/spec.md",
    "cd openspec && printf lost > specs/export/spec.md",
    "python3 -c \"from pathlib import Path; Path('openspec/specs/export/spec.md').write_text('lost')\"",
    "cp -t openspec/specs/export draft.md",
    "cp -topenspec/specs/export draft.md",
    "cp -rt openspec/specs/export draft.md",
    "cp -- -test.md openspec/specs/export/spec.md",
    "find openspec/specs -delete",
    "find openspec -type f -exec rm {} +",
])
@pytest.mark.parametrize("tool,key", [("Bash", "command"), ("exec_command", "cmd")])
def test_shell_active_mutations_are_denied(active_workspace, command, tool, key):
    root, _ = active_workspace
    denied(invoke(root, tool, {key: command}))


@pytest.mark.parametrize("command", [
    "openspec archive increase-wait --yes",
    "/tools/openspec --no-color archive increase-wait --yes",
    "env TEST=1 openspec archive increase-wait --yes",
    "openspec new change increase-wait",
    "openspec instructions specs --change increase-wait",
    "openspec show export --type spec",
    "openspec validate --specs",
    "cat openspec/specs/export/spec.md",
    "git diff -- openspec/specs/export/spec.md",
    "printf new > openspec/changes/increase-wait/design.md",
    "printf new > openspec/changes/increase-wait/specs/export/spec.md",
    "backlog task create 'Reference' --description 'openspec/specs/export/spec.md'",
    "cp openspec/specs/export/spec.md openspec/changes/increase-wait/specs/export/spec.md",
    "sed 's/30/45/' openspec/specs/export/spec.md > openspec/changes/increase-wait/specs/export/spec.md",
    "find openspec -type f -name '*.md'",
    "shasum -a 256 openspec/specs/export/spec.md",
    "realpath openspec",
])
def test_native_cli_and_change_work_are_silent(active_workspace, command):
    root, _ = active_workspace
    silent(invoke(root, "Bash", {"command": command}))


@pytest.mark.parametrize("file", [
    "openspec/changes/increase-wait/proposal.md", "openspec/changes/increase-wait/design.md",
    "openspec/changes/increase-wait/tasks.md", "openspec/changes/increase-wait/specs/export/spec.md",
    "docs/specs/example.md", "src/main.py",
    "openspec/specs/../changes/increase-wait/proposal.md",
])
def test_structured_drafts_and_unrelated_files_are_silent(active_workspace, file):
    root, _ = active_workspace
    silent(invoke(root, "Write", {"file_path": file, "content": "new"}))
    silent(invoke(root, "apply_patch", {"command": f"*** Begin Patch\n*** Add File: {file}\n+new\n*** End Patch"}))


def test_external_paths_symlinks_parent_removal_and_workdir(active_workspace, tmp_path):
    root, active = active_workspace
    alias = tmp_path / "alias"
    alias.symlink_to(root / "openspec/specs", target_is_directory=True)
    denied(invoke(tmp_path, "Write", {"file_path": str(active)}))
    denied(invoke(tmp_path, "Write", {"file_path": "alias/export/spec.md"}))
    denied(invoke(tmp_path, "Bash", {"command": "rm openspec/specs/export/spec.md", "workdir": str(root)}))
    denied(invoke(tmp_path, "Bash", {"command": f"mv {shlex.quote(str(root))} archived"}))
    silent(invoke(active.parent, "Write", {"file_path": str(tmp_path / "unrelated.md")}))


def test_external_specs_symlink_preserves_change_edits(tmp_path):
    root = tmp_path / "project"
    external = tmp_path / "external"
    (root / "openspec/changes").mkdir(parents=True)
    (external / "export").mkdir(parents=True)
    (root / "openspec/specs").symlink_to(external, target_is_directory=True)
    denied(invoke(root, "Write", {"file_path": str(external / "export/spec.md")}))
    denied(invoke(tmp_path, "Write", {"file_path": str(root / "openspec/specs/export/spec.md")}))
    denied(invoke(tmp_path, "Bash", {"command": "rm -rf project"}))
    denied(invoke(root / "openspec/specs", "Write", {"file_path": "export/spec.md"}))
    denied(invoke(root, "Bash", {"command": "rm export/spec.md", "workdir": "openspec/specs"}))
    denied(invoke(root, "Bash", {"command": "cd openspec/specs && rm export/spec.md"}))
    silent(invoke(root, "Write", {"file_path": "openspec/changes/new/proposal.md"}))


def test_active_file_symlink_is_still_an_active_contract(active_workspace, tmp_path):
    root, active = active_workspace
    external = tmp_path / "external-contract.md"
    external.write_bytes(active.read_bytes())
    active.unlink()
    active.symlink_to(external)
    denied(invoke(root, "Write", {"file_path": "openspec/specs/export/spec.md"}))
    silent(invoke(root, "Write", {"file_path": "openspec/specs/../changes/increase-wait/proposal.md"}))


@pytest.mark.parametrize("command", [
    "printf lost > openspec/specs/export/spec.md", "rm openspec/specs/export/spec.md",
    "mv openspec/specs/export/spec.md moved.md",
])
def test_actual_guard_prevents_demonstrated_side_effect(active_workspace, command):
    root, active = active_workspace
    original = active.read_bytes()
    subprocess.run(command, cwd=root, shell=True, check=True, capture_output=True)
    assert not active.exists() or active.read_bytes() != original
    active.write_bytes(original)
    decision = invoke(root, "Bash", {"command": command})
    if decision.returncode == 0:
        subprocess.run(command, cwd=root, shell=True, check=True)
    denied(decision)
    assert active.read_bytes() == original


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["ADDED", "MODIFIED"])
def test_real_native_archive_applies_change_silently(tmp_path, operation):
    cli = os.environ.get("SPECIFLOW_TEST_OPENSPEC")
    if not cli:
        pytest.skip("Set SPECIFLOW_TEST_OPENSPEC to the installed native CLI")
    subprocess.run([cli, "init", "--tools", "none", "--no-animation", "--no-copilot-cloud"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run([cli, "new", "change", "increase-wait"], cwd=tmp_path, check=True, capture_output=True)
    change = tmp_path / "openspec/changes/increase-wait"
    (change / "proposal.md").write_text("# Proposal\n\n## Why\n\nSupport a longer authorized export wait.\n\n## What Changes\n\n- Add export wait contract.\n\n## Capabilities\n\n### New Capabilities\n\n- `export`: Export wait contract.\n\n## Impact\n\nSpecification only.\n")
    delta = change / "specs/export/spec.md"
    delta.parent.mkdir(parents=True)
    delta.write_text("# Export\n\n## Purpose\n\nDefine the authorized export waiting behavior for the disposable integration fixture.\n\n## ADDED Requirements\n\n### Requirement: Export wait\nExports SHALL wait up to 45 seconds.\n\n#### Scenario: Export ready\n- **WHEN** an export takes 40 seconds\n- **THEN** it is returned successfully\n")
    if operation == "MODIFIED":
        active = tmp_path / "openspec/specs/export/spec.md"
        active.parent.mkdir(parents=True)
        active.write_text(delta.read_text().replace("## ADDED Requirements", "## Requirements").replace("45 seconds", "30 seconds"))
        requirement = "### Requirement:" + delta.read_text().split("### Requirement:", 1)[1]
        delta.write_text("# Export\n\n## MODIFIED Requirements\n\n" + requirement)
    (change / "tasks.md").write_text("# Tasks\n\n- [x] 1.1 Define and verify the fixture contract\n")
    command = f"{shlex.quote(cli)} archive increase-wait --yes"
    silent(invoke(tmp_path, "Bash", {"command": command}))
    result = subprocess.run(command, shell=True, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    active = tmp_path / "openspec/specs/export/spec.md"
    assert "Exports SHALL wait up to 45 seconds." in active.read_text()
    denied(invoke(tmp_path, "Write", {"file_path": str(active), "content": "lost"}))
