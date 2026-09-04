import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills/speciflow/scripts/storage.py"


def run_storage(*args: object, env: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def result(*args: object, env: dict[str, str] | None = None) -> dict[str, object]:
    run = run_storage(*args, env=env)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def git(*args: object) -> None:
    subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True)


def test_default_resolve_is_read_only_and_deterministic(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    before = set(tmp_path.rglob("*"))
    first = result("resolve", project, env=env)
    second = result("resolve", project, env=env)
    assert first == second
    assert Path(first["storage_base"]) == home / ".speciflow"
    assert Path(first["data_root"]) == home / ".speciflow/projects" / first["project_key"]
    assert set(tmp_path.rglob("*")) == before


def test_explicit_base_wins_and_nearest_ancestor_is_next(tmp_path: Path) -> None:
    project = tmp_path / "project" / "nested"
    project.mkdir(parents=True)
    ancestor = tmp_path / "project" / ".speciflow"
    ancestor.mkdir()
    explicit = tmp_path / "elsewhere"
    chosen = result("resolve", project, "--base", explicit)
    inherited = result("resolve", project)
    assert Path(chosen["storage_base"]) == explicit
    assert Path(inherited["storage_base"]) == ancestor


def test_linked_worktrees_share_identity_under_one_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    git("init", repo)
    git("-C", repo, "config", "user.name", "SpeciFlow Test")
    git("-C", repo, "config", "user.email", "speciflow@example.invalid")
    (repo / "seed").write_text("seed\n")
    git("-C", repo, "add", "seed")
    git("-C", repo, "commit", "-m", "seed")
    worktree = tmp_path / "worktree"
    git("-C", repo, "worktree", "add", "-b", "linked", worktree)
    base = tmp_path / "storage"
    first = result("resolve", repo, "--base", base)
    second = result("resolve", worktree, "--base", base)
    assert (first["project_identity"], first["project_key"], first["data_root"]) == (
        second["project_identity"], second["project_key"], second["data_root"]
    )


def test_metadata_status_is_missing_valid_or_invalid(tmp_path: Path) -> None:
    project, base = tmp_path / "project", tmp_path / "base"
    project.mkdir()
    missing = result("resolve", project, "--base", base)
    assert missing["metadata_status"] == "missing"
    initialized = result("init", project, "--base", base)
    assert initialized["metadata_status"] == "valid"
    metadata = Path(initialized["metadata_path"])
    changed = json.loads(metadata.read_text())
    changed["version"] = 2
    metadata.write_text(json.dumps(changed))
    invalid = result("resolve", project, "--base", base)
    assert invalid["metadata_status"] == "invalid"


def test_boolean_metadata_version_is_invalid(tmp_path: Path) -> None:
    project, base = tmp_path / "project", tmp_path / "base"
    project.mkdir()
    initialized = result("init", project, "--base", base)
    metadata = Path(initialized["metadata_path"])
    changed = json.loads(metadata.read_text())
    changed["version"] = True
    metadata.write_text(json.dumps(changed))
    assert result("resolve", project, "--base", base)["metadata_status"] == "invalid"


def test_init_adopts_populated_root_without_touching_native_bytes(tmp_path: Path) -> None:
    project, base = tmp_path / "project", tmp_path / "base"
    project.mkdir()
    resolved = result("resolve", project, "--base", base)
    data_root = Path(resolved["data_root"])
    (data_root / "planning").mkdir(parents=True)
    (data_root / "beads").mkdir()
    (data_root / "planning/existing.txt").write_bytes(b"planning-owned")
    (data_root / "beads/existing.bin").write_bytes(b"beads-owned")
    before = {
        "planning": (data_root / "planning/existing.txt").read_bytes(),
        "beads": (data_root / "beads/existing.bin").read_bytes(),
    }
    initialized = result("init", project, "--base", base)
    assert initialized["metadata_status"] == "valid"
    assert (data_root / "planning/existing.txt").read_bytes() == before["planning"]
    assert (data_root / "beads/existing.bin").read_bytes() == before["beads"]
    assert {p.name for p in data_root.iterdir()} == {"planning", "beads", ".speciflow-project.json"}


def test_init_never_overwrites_metadata(tmp_path: Path) -> None:
    project, base = tmp_path / "project", tmp_path / "base"
    project.mkdir()
    resolved = result("resolve", project, "--base", base)
    metadata = Path(resolved["metadata_path"])
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes(b"mismatched")
    run = run_storage("init", project, "--base", base)
    assert run.returncode != 0
    assert metadata.read_bytes() == b"mismatched"


def test_init_never_creates_planning_or_beads(tmp_path: Path) -> None:
    project, base = tmp_path / "project", tmp_path / "base"
    project.mkdir()
    initialized = result("init", project, "--base", base)
    data_root = Path(initialized["data_root"])
    assert initialized["metadata_status"] == "valid"
    assert not (data_root / "planning").exists()
    assert not (data_root / "beads").exists()
