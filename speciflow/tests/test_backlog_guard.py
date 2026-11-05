"""Exercise the hook as a subprocess, including paired real filesystem effects."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


PACKAGE = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("SPECIFLOW_TEST_HOOK", PACKAGE / "scripts/backlog_guard.py"))


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "planning with spaces"
    tasks = root / "backlog/tasks"
    tasks.mkdir(parents=True)
    (root / "backlog/config.yml").write_text("project_name: fixture\n")
    task = tasks / "task-1.md"
    task.write_text("Outcome: preserve customer requirements\n")
    return root, task


def invoke(root, name, args):
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"hook_event_name": "PreToolUse", "cwd": str(root),
                          "tool_name": name, "tool_input": args}),
        text=True, capture_output=True, timeout=5,
    )


def denied(result):
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stdout == ""
    assert "SpeciFlow" in result.stderr and "Backlog CLI" in result.stderr


def silent(result):
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("command", [
    "backlog task edit 10 --ref openspec/changes/current/proposal.md",
    "backlog tasks edit TASK-10 --ref=openspec/changes/current/proposal.md",
    "backlog task edit 10 --ref ''",
    "command backlog task edit 10 --ref a --ref b",
    "env LANG=C backlog task edit 10 --ref a",
    "/tools/backlog task edit 10 --ref a",
    "backlog task 10 --plain && backlog task edit 10 --ref a",
    "backlog task edit 10 --clear-milestone --ref a",
    "backlog task edit 10 -tTitle --ref a",
    "backlog task edit 10 --title=Title --ref a",
])
@pytest.mark.parametrize("host_name", ["Bash", "exec_command"])
def test_reference_replacement_shorthand_is_denied(workspace, command, host_name):
    root, task = workspace
    before = task.read_bytes()
    key = "cmd" if host_name == "exec_command" else "command"
    result = invoke(root, host_name, {key: command})
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stdout == ""
    assert "--add-ref" in result.stderr and "replace" in result.stderr.lower()
    assert task.read_bytes() == before


@pytest.mark.parametrize("command", [
    "backlog task create 'New outcome' --ref a",
    "backlog task edit 10 --add-ref a",
    "backlog task edit 10 --remove-ref old --add-ref new",
    "backlog task edit 10 --clear-refs",
    "backlog task edit 10 --title '--ref'",
    "backlog task edit 10 --description '--ref'",
    "backlog task edit 10 -t '--ref'",
    "backlog task edit 10 -tTitle --add-ref a",
    "backlog task edit 10 -tTitle --help --ref a",
    "backlog task edit 10 --notes 'backlog task edit 10 --ref a'",
    "backlog task edit 10 --ref a --help",
    "backlog task edit --help --ref a",
    "backlog task edit 10 -- --ref",
    "printf '%s' 'backlog task edit 10 --ref a'",
])
def test_additive_and_explicit_delta_reference_operations_are_silent(workspace, command):
    root, _ = workspace
    silent(invoke(root, "Bash", {"command": command}))


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("SPECIFLOW_TEST_BACKLOG"), reason="real Backlog CLI trial is opt-in")
def test_native_reference_addition_and_intentional_replacement(tmp_path):
    backlog = os.environ["SPECIFLOW_TEST_BACKLOG"]
    env = {**os.environ, "BACKLOG_CWD": str(tmp_path)}

    def native(*args):
        result = subprocess.run([backlog, *args], cwd=tmp_path, env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (result.stdout, result.stderr)
        return result.stdout

    native("init", "Reference fixture", "--defaults", "--no-git", "--agent-instructions", "none")
    first, second, added = (f"https://tracker.invalid/ISSUE-{i}" for i in (101, 102, 103))
    native("task", "create", "Keep source references", "--ref", first, "--ref", second, "--plain")
    assert first in native("task", "1", "--plain") and second in native("task", "1", "--plain")

    # Establish the installed owner's actual asymmetric semantics in disposable data.
    native("task", "edit", "1", "--ref", added)
    replaced = native("task", "1", "--plain")
    assert added in replaced and first not in replaced and second not in replaced
    native("task", "edit", "1", "--remove-ref", added, "--add-ref", first, "--add-ref", second)

    task = next((tmp_path / "backlog/tasks").glob("*.md"))
    before = task.read_bytes()
    denied_call = invoke(tmp_path, "Bash", {"command": f"backlog task edit 1 --ref {added}"})
    assert denied_call.returncode == 2
    assert task.read_bytes() == before

    silent(invoke(tmp_path, "Bash", {"command": f"backlog task edit 1 --add-ref {added}"}))
    native("task", "edit", "1", "--add-ref", added)
    readback = native("task", "1", "--plain")
    assert all(ref in readback for ref in (first, second, added))

    silent(invoke(tmp_path, "Bash", {"command": f"backlog task edit 1 --remove-ref {first} --remove-ref {added}"}))
    native("task", "edit", "1", "--remove-ref", first, "--remove-ref", added)
    readback = native("task", "1", "--plain")
    assert second in readback and first not in readback and added not in readback


@pytest.mark.parametrize("tool,args", [
    ("Write", {"file_path": "backlog/tasks/task-1.md", "content": "lost"}),
    ("Edit", {"file_path": "backlog/tasks/task-1.md", "old_string": "Outcome", "new_string": "lost"}),
    ("apply_patch", {"command": "*** Begin Patch\n*** Update File: backlog/tasks/task-1.md\n@@\n-Outcome\n+lost\n*** End Patch"}),
    ("apply_patch", {"command": "*** Begin Patch\n*** Delete File: backlog/tasks/task-1.md\n*** End Patch"}),
    ("apply_patch", {"command": "*** Begin Patch\n*** Add File: output.md\n*** Move to: backlog/tasks/task-1.md\n+x\n*** End Patch"}),
])
def test_direct_editor_and_patch_are_denied(workspace, tool, args):
    root, task = workspace
    before = task.read_bytes()
    denied(invoke(root, tool, args))
    assert task.read_bytes() == before


@pytest.mark.parametrize("command", [
    "printf lost > backlog/tasks/task-1.md",
    "printf lost >> backlog/tasks/task-1.md",
    "tee backlog/tasks/task-1.md </dev/null",
    "sed -i '' s/Outcome/lost/ backlog/tasks/task-1.md",
    "rm backlog/tasks/task-1.md",
    "mv backlog/tasks/task-1.md output.md",
    "cp output.md backlog/tasks/task-1.md",
    "rm -rf backlog",
    "backlog task list --plain; rm backlog/tasks/task-1.md",
    "backlog task list --plain > backlog/tasks/task-1.md",
    "git diff --output=backlog/tasks/task-1.md",
    "git log --output backlog/tasks/task-1.md",
    "cat README.md;\nrm backlog/tasks/task-1.md",
    "cat README.md &&\nrm backlog/tasks/task-1.md",
    "(cat README.md); rm backlog/tasks/task-1.md",
    "(cd /tmp; cat README.md); rm backlog/tasks/task-1.md",
    "cd backlog && printf lost > tasks/task-1.md",
    "python3 -c \"from pathlib import Path; Path('backlog/tasks/task-1.md').write_text('lost')\"",
])
@pytest.mark.parametrize("host_name", ["Bash", "exec_command"])
def test_visible_shell_mutations_are_denied(workspace, command, host_name):
    root, task = workspace
    key = "cmd" if host_name == "exec_command" else "command"
    denied(invoke(root, host_name, {key: command}))


@pytest.mark.parametrize("command", [
    "backlog task edit TASK-1 --description 'backlog/tasks/task-1.md'",
    "/tools/backlog task edit TASK-1 --ac 'Preserve SpeciFlow'",
    "env BACKLOG_CWD=/tmp/planning backlog task list --plain",
    "cat backlog/tasks/task-1.md",
    "rg Outcome backlog",
    "git diff -- backlog/tasks/task-1.md",
    "printf lost > source.md",
    "cat backlog/tasks/task-1.md > /tmp/requirements-copy.md",
    "sed -n '1,5p' backlog/tasks/task-1.md",
    "find backlog -type f -name '*.md'",
    "shasum -a 256 backlog/tasks/task-1.md",
    "realpath backlog",
])
def test_cli_reads_and_unrelated_writes_are_silent(workspace, command):
    root, _ = workspace
    silent(invoke(root, "Bash", {"command": command}))


def test_external_root_symlink_workdir_and_parent_rename(workspace, tmp_path):
    root, task = workspace
    alias = tmp_path / "alias"
    alias.symlink_to(root / "backlog", target_is_directory=True)
    denied(invoke(tmp_path, "Write", {"file_path": str(task)}))
    denied(invoke(tmp_path, "Write", {"file_path": "alias/tasks/task-1.md"}))
    denied(invoke(tmp_path, "Bash", {"command": "printf lost > backlog/tasks/task-1.md", "workdir": str(root)}))
    denied(invoke(tmp_path, "Bash", {"command": f"mv '{root}' archived"}))


def test_plain_backlog_named_document_is_unrelated(tmp_path):
    silent(invoke(tmp_path, "Write", {"file_path": "backlog.md", "content": "ok"}))


@pytest.mark.parametrize("folder,root_config", [(".backlog", False), ("plans/product", False), ("plans/product", True)])
def test_native_custom_backlog_locations(tmp_path, folder, root_config):
    data = tmp_path / folder
    (data / "tasks").mkdir(parents=True)
    config = tmp_path / "backlog.config.yml" if root_config else data / "config.yml"
    config.write_text(f'project_name: fixture\nbacklog_directory: "{folder}"\n')
    denied(invoke(tmp_path, "Write", {"file_path": str(data / "tasks/task-1.md")}))
    denied(invoke(tmp_path, "Write", {"file_path": str(config)}))
    denied(invoke(tmp_path, "Bash", {"command": f"rm -rf {folder}"}))
    silent(invoke(tmp_path, "Write", {"file_path": "src/main.py", "content": "ok"}))


@pytest.mark.parametrize("value,folder", [
    ('plans/product # literal', 'plans/product # literal'),
    ('"plans/product" # literal', 'plans/product" # literal'),
    ("'plans/product' # literal", "plans/product' # literal"),
])
def test_root_configuration_uses_native_literal_scalar_semantics(tmp_path, value, folder):
    (tmp_path / folder / "tasks").mkdir(parents=True)
    (tmp_path / "backlog.config.yml").write_text(f"project_name: fixture\nbacklog_directory: {value}\n")
    denied(invoke(tmp_path, "Write", {"file_path": str(tmp_path / folder / "tasks/task-1.md")}))


def test_root_config_with_symlinked_data_checks_project_and_lexical_ancestry(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    (external / "tasks").mkdir(parents=True)
    (project / "plans").symlink_to(external, target_is_directory=True)
    (project / "backlog.config.yml").write_text('project_name: fixture\nbacklog_directory: "plans"\n')
    denied(invoke(project, "Write", {"file_path": str(external / "tasks/new.md")}))
    denied(invoke(tmp_path, "Write", {"file_path": str(project / "plans/tasks/new.md")}))
    denied(invoke(tmp_path, "Bash", {"command": "rm -rf project"}))
    silent(invoke(project, "Write", {"file_path": "source.py"}))


def test_cwd_inside_managed_data_does_not_protect_unrelated_paths(workspace, tmp_path):
    root, _ = workspace
    cwd = root / "backlog/tasks"
    unrelated = tmp_path / "unrelated.md"
    silent(invoke(cwd, "Write", {"file_path": str(unrelated), "content": "ok"}))
    silent(invoke(cwd, "Bash", {"command": f"printf ok > '{unrelated}'"}))


@pytest.mark.parametrize("command", [
    "printf lost > backlog/tasks/task-1.md",
    "rm backlog/tasks/task-1.md",
    "mv backlog/tasks/task-1.md moved.md",
    "cat backlog/config.yml;\nrm backlog/tasks/task-1.md",
])
def test_paired_real_mutation_and_recovery(workspace, command):
    root, task = workspace
    original = task.read_bytes()
    # Control: the exact tool command really destroys the authoritative bytes.
    subprocess.run(command, cwd=root, shell=True, check=True)
    assert not task.exists() or task.read_bytes() != original
    task.write_bytes(original)
    # Candidate: emulate the host's documented PreToolUse exit-2 contract.
    decision = invoke(root, "Bash", {"command": command})
    if decision.returncode == 0:
        subprocess.run(command, cwd=root, shell=True, check=True)
    denied(decision)
    assert task.read_bytes() == original
    recovery = subprocess.run(["cat", str(task)], capture_output=True, check=True)
    assert recovery.stdout == original


@pytest.mark.integration
@pytest.mark.parametrize("folder,location", [("backlog", "folder"), (".backlog", "folder"), ("plans/product", "root")])
def test_real_native_cli_can_change_requirements_silently(tmp_path, folder, location):
    backlog = os.environ.get("SPECIFLOW_TEST_BACKLOG")
    if not backlog:
        pytest.skip("Set SPECIFLOW_TEST_BACKLOG to an installed native Backlog CLI")
    subprocess.run([backlog, "init", "fixture", "--defaults", "--no-git", "--agent-instructions", "none",
                    "--backlog-dir", folder, "--config-location", location], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run([backlog, "task", "create", "Keep requirement", "--ac", "Original intent"], cwd=tmp_path, check=True, capture_output=True)
    task = next((tmp_path / folder / "tasks").glob("*.md"))
    before = task.read_bytes()
    denied(invoke(tmp_path, "Write", {"file_path": str(task), "content": "lost"}))
    command = f"'{backlog}' task edit TASK-1 --description 'Changed without invoking SpeciFlow'"
    silent(invoke(tmp_path, "Bash", {"command": command}))
    subprocess.run(command, shell=True, cwd=tmp_path, check=True, capture_output=True)
    assert task.read_bytes() != before  # Explicit counterexample to full enforcement.


@pytest.mark.integration
@pytest.mark.parametrize("scalar", [
    'plans/product # literal', '"plans/product" # literal', "'plans/product' # literal",
    r'"plans/prod\u0075ct"', r'"plans/a\\b"',
])
def test_configured_root_matches_actual_native_scalar_reader(tmp_path, scalar):
    backlog = os.environ.get("SPECIFLOW_TEST_BACKLOG")
    if not backlog:
        pytest.skip("Set SPECIFLOW_TEST_BACKLOG to an installed native Backlog CLI")
    subprocess.run([backlog, "init", "fixture", "--defaults", "--no-git", "--agent-instructions", "none",
                    "--backlog-dir", "plans/product", "--config-location", "root"], cwd=tmp_path, check=True, capture_output=True)
    config = tmp_path / "backlog.config.yml"
    import re
    config.write_text(re.sub(r"^backlog_directory:.*$", lambda _: f"backlog_directory: {scalar}",
                             config.read_text(), flags=re.MULTILINE))
    subprocess.run([backlog, "task", "create", "Owner chooses directory"], cwd=tmp_path, check=True, capture_output=True)
    task = next(p for p in tmp_path.rglob("*.md") if p.parent.name == "tasks")
    denied(invoke(tmp_path, "Write", {"file_path": str(task), "content": "lost"}))
