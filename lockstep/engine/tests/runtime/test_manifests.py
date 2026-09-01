import os
import stat
import subprocess
from pathlib import Path

import pytest

from lockstep.runtime import manifests
from lockstep.runtime.manifests import (
    PathContractError,
    ProjectWritePath,
    capture_git_attestation,
    capture_project,
)
from lockstep.runtime.owner_state import StorageLimitExceeded
from lockstep.runtime.project_paths import ProjectTreeLimits


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.mark.parametrize(
    "raw",
    [
        "",
        ".",
        "..",
        "../x",
        "/tmp/x",
        ".git",
        ".git/config",
        ".GIT/config",
        "a/../b",
        "a\\b",
        "a\x00b",
    ],
)
def test_project_write_path_rejects_unsafe_values(project: Path, raw: str) -> None:
    with pytest.raises(PathContractError):
        ProjectWritePath.parse(raw, project)


@pytest.mark.parametrize("raw", ["CON", "aux.txt", "report.", "report "])
def test_project_write_path_rejects_windows_aliases_even_on_posix(
    project: Path, raw: str
) -> None:
    with pytest.raises(PathContractError, match="platform"):
        ProjectWritePath.parse(raw, project)


def test_project_write_path_rejects_existing_symlink_ancestor(
    project: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, project / "linked")

    with pytest.raises(PathContractError, match="symlink"):
        ProjectWritePath.parse("linked/report.md", project)


def test_project_write_path_marks_trailing_slash_as_prefix(project: Path) -> None:
    path = ProjectWritePath.parse("reports/", project)

    assert path.relative.as_posix() == "reports"
    assert path.is_prefix is True


def test_project_write_path_rejects_case_and_unicode_collisions(project: Path) -> None:
    (project / "Report.md").write_text("one")
    (project / "café.md").write_text("two")

    with pytest.raises(PathContractError, match="collision"):
        ProjectWritePath.parse("report.md", project)
    with pytest.raises(PathContractError, match="collision"):
        ProjectWritePath.parse("café.md", project)


def test_capture_project_binds_kind_mode_and_hash_without_following_symlink(
    project: Path, tmp_path: Path
) -> None:
    executable = project / "run"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    (project / "directory").mkdir()
    outside = tmp_path / "secret"
    outside.write_text("not project content")
    os.symlink(outside, project / "link")

    snapshot = capture_project(project)
    entries = {entry.path: entry for entry in snapshot.entries}

    assert entries["run"].kind == "file"
    assert entries["run"].executable is True
    assert entries["run"].sha256
    assert entries["directory"].kind == "directory"
    assert entries["directory"].sha256 is None
    assert entries["link"].kind == "symlink"
    assert entries["link"].sha256


def test_capture_project_binds_a_symlink_target_not_only_its_kind(
    project: Path, tmp_path: Path
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("one")
    second.write_text("two")
    link = project / "link"
    os.symlink(first, link)
    before = capture_project(project)
    link.unlink()
    os.symlink(second, link)

    assert capture_project(project) != before


def test_capture_project_rejects_same_inode_mutation_during_hash(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = project / "target.txt"
    target.write_text("before")

    def mutate() -> None:
        target.write_text("after-with-different-size")

    monkeypatch.setattr(manifests, "_before_regular_hash", mutate)
    with pytest.raises(PathContractError, match="changed while capturing"):
        capture_project(project)


def test_capture_project_rejects_directory_symlink_swap_before_open(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = project / "output"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    def swap() -> None:
        directory.rmdir()
        os.symlink(outside, directory)

    monkeypatch.setattr(manifests, "_before_directory_open", swap)
    with pytest.raises(PathContractError, match="directory changed"):
        capture_project(project)


@pytest.mark.parametrize("contents", [b"not a gitdir", b"gitdir: ../../etc\n"])
def test_capture_git_attestation_rejects_malformed_or_escaping_gitdir_marker(
    project: Path, contents: bytes
) -> None:
    (project / ".git").write_bytes(contents)

    with pytest.raises(PathContractError):
        capture_git_attestation(project)


def test_capture_git_attestation_rejects_git_symlink(
    project: Path, tmp_path: Path
) -> None:
    target = tmp_path / "not-git"
    target.mkdir()
    os.symlink(target, project / ".git")

    with pytest.raises(PathContractError, match="symlink"):
        capture_git_attestation(project)


def test_linked_worktree_attestation_binds_common_config_and_refs_not_sibling_registry(
    project: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repository"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "tracked.txt").write_text("base")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "linked", str(linked)],
        check=True,
    )

    before = capture_git_attestation(linked)
    assert before is not None
    assert before.common_config_sha256
    assert before.common_refs_sha256

    # Sibling registry churn is coordinator-owned and deliberately excluded.
    (repo / ".git" / "worktrees" / "unrelated").mkdir()
    assert capture_git_attestation(linked) == before

    other = tmp_path / "other"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "other", str(other)],
        check=True,
    )
    other_private = Path((other / ".git").read_text().split(": ", 1)[1].strip())

    (repo / ".git" / "refs" / "heads" / "master").write_text("0" * 40 + "\n")
    assert capture_git_attestation(linked) != before

    private_git = Path((linked / ".git").read_text().split(": ", 1)[1].strip())
    (private_git / "config.worktree").write_text("[core]\nignorecase = false\n")
    assert capture_git_attestation(linked) != before

    # The private gitdir marker binds this directory to this exact project;
    # pointing it at a sibling worktree must not be accepted as an alias.
    (linked / ".git").write_text(f"gitdir: {other_private}\n")
    with pytest.raises(PathContractError, match="linkage"):
        capture_git_attestation(linked)


def test_common_ref_walk_is_deterministic_across_directory_creation_order(
    project: Path, tmp_path: Path
) -> None:
    first = project / "refs"
    second_root = tmp_path / "second"
    second = second_root / "refs"
    for root, names in ((first, ("z", "a")), (second, ("a", "z"))):
        for name in names:
            path = root / name
            path.mkdir(parents=True, exist_ok=True)
            (path / "ref").write_text(name)

    assert manifests._metadata_tree_digest(
        project, ("refs",)
    ) == manifests._metadata_tree_digest(second_root, ("refs",))


def test_git_attestation_is_separate_from_project_snapshot(project: Path) -> None:
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (project / ".git" / "index").write_bytes(b"index")

    snapshot = capture_project(project)
    git = capture_git_attestation(project)

    assert all(not entry.path.startswith(".git") for entry in snapshot.entries)
    assert git is not None
    assert git.head_sha256


def test_capture_project_enforces_common_tree_entry_limit(project: Path) -> None:
    (project / "one").write_text("1")
    (project / "two").write_text("2")

    with pytest.raises(StorageLimitExceeded, match="entries"):
        capture_project(project, limits=ProjectTreeLimits(max_entries=1))


def test_capture_project_applies_same_limits_to_git_metadata(project: Path) -> None:
    refs = project / ".git" / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "one").write_text("1" * 40)
    (refs / "two").write_text("2" * 40)

    with pytest.raises(StorageLimitExceeded, match="Git metadata entries"):
        capture_project(project, limits=ProjectTreeLimits(max_entries=1))
