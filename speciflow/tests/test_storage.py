import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "speciflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import storage


def request(cwd: Path, **kwargs: object) -> storage.ResolveRequest:
    return storage.ResolveRequest(cwd=cwd, **kwargs)


def key_for(anchor: Path) -> str:
    return hashlib.sha256(os.fsencode(str(anchor.resolve()))).hexdigest()


def locator_payload(anchor: Path, base: Path, data_root: Path | None = None) -> dict[str, object]:
    anchor = anchor.resolve()
    base = base.resolve()
    key = key_for(anchor)
    return {
        "version": 1,
        "anchor": str(anchor),
        "project_key": key,
        "storage_base": str(base),
        "data_root": str((base / "projects" / key if data_root is None else data_root).resolve()),
    }


def write_locator(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def snapshot_tree(root: Path) -> tuple[tuple[str, bytes | None], ...]:
    return tuple(
        (str(path.relative_to(root)), path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    )


def run_git(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *map(str, args)], check=True, capture_output=True, text=True
    )


def test_resolution_precedence_and_read_only(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    inherited = project / ".speciflow"
    inherited.mkdir(mode=0o700)
    anchor = project.resolve()
    key = key_for(anchor)
    data_root = inherited / "projects" / key
    payload = locator_payload(anchor, inherited, data_root)
    write_locator(inherited / "locators" / key / "locator-v1.json", payload)
    write_locator(inherited / "anchor-locator-v1.json", payload)
    (project / "src").mkdir()
    before = snapshot_tree(tmp_path)

    result = storage.resolve(request(project / "src"))

    assert result.source == "ancestor"
    assert result.storage_base == inherited.resolve()
    assert result.data_root == data_root.resolve()
    assert snapshot_tree(tmp_path) == before


def test_explicit_base_wins_over_ancestor(tmp_path: Path) -> None:
    project = tmp_path / "project"
    inherited = project / ".speciflow"
    chosen = tmp_path / "chosen"
    (project / "src").mkdir(parents=True)
    inherited.mkdir()

    selection = storage.resolve(request(project / "src", explicit_base=chosen))

    assert selection.source == "explicit"
    assert selection.storage_base == chosen.resolve()


def test_linked_worktrees_share_and_clones_do_not(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    run_git("init", repo)
    run_git("-C", repo, "config", "user.name", "SpeciFlow Test")
    run_git("-C", repo, "config", "user.email", "speciflow@example.invalid")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    run_git("-C", repo, "add", "seed.txt")
    run_git("-C", repo, "commit", "-m", "seed")
    worktree_b = tmp_path / "worktree-b"
    clone_a, clone_b = tmp_path / "clone-a", tmp_path / "clone-b"
    run_git("-C", repo, "worktree", "add", "-b", "worktree-b", worktree_b)
    run_git("clone", repo, clone_a)
    run_git("clone", repo, clone_b)

    assert storage.resolve(request(repo)).data_root == storage.resolve(request(worktree_b)).data_root
    assert storage.resolve(request(clone_a)).data_root != storage.resolve(request(clone_b)).data_root


def test_bare_git_anchor_is_common_directory_and_has_no_native_layout(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    run_git("init", "--bare", bare)
    chosen = tmp_path / "chosen"

    selection = storage.resolve(request(bare, explicit_base=chosen))
    preview = storage.preview_init(selection)

    assert selection.anchor == bare.resolve()
    assert selection.working_root == bare.resolve()
    assert preview.directories_to_create == (
        chosen.resolve(),
        chosen.resolve() / "locators",
        chosen.resolve() / "locators" / selection.project_key,
        chosen.resolve() / "projects",
        chosen.resolve() / "projects" / selection.project_key,
    )
    assert all(path.name not in {"planning", "beads"} for path in preview.directories_to_create)


def test_default_storage_uses_standard_posix_home_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(storage.Path, "home", classmethod(lambda _cls: profile))

    assert storage._account_home() == profile.resolve()


def test_nearest_project_storage_does_not_require_account_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    base = project / ".speciflow"
    project.mkdir()
    base.mkdir()
    monkeypatch.setattr(storage.Path, "home", classmethod(lambda _cls: tmp_path / "missing-home"))

    selection = storage.resolve(request(project))

    assert selection.source == "ancestor"
    assert selection.storage_base == base.resolve()


def test_default_home_storage_resolution_is_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    project = profile / "project" / "src"
    project.mkdir(parents=True)
    monkeypatch.setattr(storage.Path, "home", classmethod(lambda _cls: profile))

    first = storage.resolve(request(project))
    storage.init(storage.preview_init(first))
    second = storage.resolve(request(project))
    repeated_preview = storage.preview_init(second)

    assert first.source == second.source == "default"
    assert (first.anchor, first.storage_base, first.data_root) == (
        second.anchor,
        second.storage_base,
        second.data_root,
    )
    assert repeated_preview.locator_writes == ()
    assert repeated_preview.directories_to_create == ()


def test_init_rejects_symlink_collision_and_keeps_existing_bytes(tmp_path: Path) -> None:
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    preview = storage.preview_init(storage.resolve(request(tmp_path, explicit_base=chosen)))
    preview.selection.storage_locator_path.parent.mkdir(parents=True)
    preview.selection.storage_locator_path.symlink_to(outside)

    with pytest.raises(storage.StorageConflict):
        storage.init(preview)

    assert outside.read_bytes() == b"keep"


def test_locator_rejects_unknown_and_duplicate_fields(tmp_path: Path) -> None:
    base = tmp_path / "chosen"
    selection = storage.resolve(request(tmp_path, explicit_base=base))
    payload = locator_payload(selection.anchor, base)
    payload["unknown"] = "no"
    write_locator(selection.storage_locator_path, payload)

    with pytest.raises(storage.StorageConflict):
        storage.resolve(request(tmp_path, explicit_base=base))


def test_locator_rejects_data_root_outside_project_namespace(tmp_path: Path) -> None:
    base = tmp_path / "chosen"
    selection = storage.resolve(request(tmp_path, explicit_base=base))
    write_locator(
        selection.storage_locator_path,
        locator_payload(selection.anchor, base, tmp_path / "outside"),
    )

    with pytest.raises(storage.StorageConflict, match="ambiguous"):
        storage.resolve(request(tmp_path, explicit_base=base))

    selection.storage_locator_path.write_text(
        '{"version":1,"version":1,"anchor":"x","project_key":"x","storage_base":"x","data_root":"x"}',
        encoding="utf-8",
    )
    with pytest.raises(storage.StorageConflict):
        storage.resolve(request(tmp_path, explicit_base=base))


def test_git_common_locator_sharing_is_opt_in_and_hierarchy_wins(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    run_git("init", repo)
    chosen = tmp_path / "chosen"
    first = storage.resolve(request(repo, explicit_base=chosen))
    first_preview = storage.preview_init(first, share_from_anchor=True)
    storage.init(first_preview)
    inherited = repo / ".speciflow"
    inherited.mkdir()
    (repo / "src").mkdir()

    selection = storage.resolve(request(repo))

    assert selection.source == "ancestor"
    assert selection.storage_base == inherited.resolve()
    assert storage.resolve(request(repo / "src")).source == "ancestor"


def test_non_git_shared_ancestor_is_ambiguous_without_anchor(tmp_path: Path) -> None:
    container = tmp_path / ".speciflow"
    (tmp_path / "one" / "child").mkdir(parents=True)
    container.mkdir()

    with pytest.raises(storage.StorageConflict, match="ambiguous"):
        storage.resolve(request(tmp_path / "one" / "child"))


def test_non_git_explicit_sibling_anchors_have_distinct_keys(tmp_path: Path) -> None:
    container = tmp_path / ".speciflow"
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    container.mkdir()

    first = storage.resolve(request(one, explicit_anchor=one))
    second = storage.resolve(request(two, explicit_anchor=two))

    assert first.source == second.source == "ancestor"
    assert first.data_root != second.data_root


def test_non_git_project_local_marker_keeps_subdirectory_key_stable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    base = project / ".speciflow"
    preview = storage.preview_init(storage.resolve(request(project, explicit_base=base)))
    storage.init(preview)

    assert storage.resolve(request(nested)).project_key == storage.resolve(request(project)).project_key


def test_init_is_exact_noop_and_rejects_partial_state(tmp_path: Path) -> None:
    base = tmp_path / "chosen"
    preview = storage.preview_init(storage.resolve(request(tmp_path, explicit_base=base)))
    locator = storage.init(preview)
    before = snapshot_tree(tmp_path)

    assert storage.init(storage.preview_init(storage.resolve(request(tmp_path, explicit_base=base)))) == locator
    assert snapshot_tree(tmp_path) == before

    other = tmp_path / "other"
    partial = storage.resolve(request(tmp_path, explicit_base=other))
    partial.data_root.mkdir(parents=True)
    with pytest.raises(storage.StorageConflict, match="partial"):
        storage.init(storage.preview_init(partial))


def test_repeated_init_rejects_permission_drift(tmp_path: Path) -> None:
    base = tmp_path / "chosen"
    preview = storage.preview_init(storage.resolve(request(tmp_path, explicit_base=base)))
    storage.init(preview)
    preview.selection.storage_locator_path.chmod(0o644)
    preview.selection.data_root.chmod(0o755)

    with pytest.raises(storage.StorageConflict, match="permission"):
        storage.init(storage.preview_init(storage.resolve(request(tmp_path, explicit_base=base))))


def test_init_rechecks_live_facts_against_approved_preview(tmp_path: Path) -> None:
    base = tmp_path / "chosen"
    preview = storage.preview_init(storage.resolve(request(tmp_path, explicit_base=base)))
    base.mkdir()

    with pytest.raises(storage.StorageConflict, match="preview"):
        storage.init(preview)


def test_init_creates_exactly_the_previewed_nested_base_tree(tmp_path: Path) -> None:
    base = tmp_path / "outer" / "inner" / "chosen"
    preview = storage.preview_init(storage.resolve(request(tmp_path, explicit_base=base)))
    expected_directories = set(preview.directories_to_create)

    storage.init(preview)

    actual_directories = {path for path in tmp_path.rglob("*") if path.is_dir()}
    assert actual_directories == expected_directories


def test_git_common_locator_without_matching_storage_locator_is_partial(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    run_git("init", repo)
    base = tmp_path / "chosen"
    selection = storage.resolve(request(repo, explicit_base=base))
    write_locator(selection.anchor_locator_path, locator_payload(selection.anchor, base))

    with pytest.raises(storage.StorageConflict, match="partial"):
        storage.resolve(request(repo))


def test_git_common_and_storage_locators_must_be_identical(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    run_git("init", repo)
    base = tmp_path / "chosen"
    preview = storage.preview_init(storage.resolve(request(repo, explicit_base=base)), share_from_anchor=True)
    storage.init(preview)
    changed = locator_payload(preview.selection.anchor, base, tmp_path / "other-root")
    write_locator(preview.selection.anchor_locator_path, changed)

    with pytest.raises(storage.StorageConflict, match="locator"):
        storage.resolve(request(repo))


def test_non_git_anchor_locator_without_storage_locator_is_partial(tmp_path: Path) -> None:
    project = tmp_path / "project"
    child = project / "child"
    child.mkdir(parents=True)
    base = project / ".speciflow"
    write_locator(base / "anchor-locator-v1.json", locator_payload(project, base))

    with pytest.raises(storage.StorageConflict, match="partial"):
        storage.resolve(request(child))


def test_non_git_anchor_and_storage_locators_must_be_identical(tmp_path: Path) -> None:
    project = tmp_path / "project"
    child = project / "child"
    child.mkdir(parents=True)
    base = project / ".speciflow"
    preview = storage.preview_init(storage.resolve(request(project, explicit_base=base)))
    storage.init(preview)
    write_locator(base / "anchor-locator-v1.json", locator_payload(project, base, tmp_path / "other-root"))

    with pytest.raises(storage.StorageConflict, match="locator"):
        storage.resolve(request(child))


def test_git_identity_ignores_ambient_repository_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_a, repo_b, plain = tmp_path / "repo-a", tmp_path / "repo-b", tmp_path / "plain"
    run_git("init", repo_a)
    run_git("init", repo_b)
    plain.mkdir()
    monkeypatch.setenv("GIT_DIR", str(repo_a / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo_a))
    base = tmp_path / "chosen"

    repo_b_selection = storage.resolve(request(repo_b, explicit_base=base))
    plain_selection = storage.resolve(request(plain, explicit_base=base))

    assert repo_b_selection.anchor == (repo_b / ".git").resolve()
    assert plain_selection.anchor == plain.resolve()
