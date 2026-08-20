from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os

import pytest


@pytest.fixture
def recipe_tree(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    root = source / "root.recipe.yaml"
    child = source / "child.recipe.yaml"
    prompts = source / "prompts"
    prompts.mkdir()
    prompt = prompts / "review.md"
    root.write_text("include_graph: child.recipe.yaml\n")
    child.write_text("name: child\n")
    prompt.write_text("Review carefully.\n")
    return root, child, prompt


@pytest.fixture
def bundle_store(tmp_path):
    from lockstep.runtime.recipe_bundles import RecipeBundleStore

    return RecipeBundleStore(tmp_path / "owner-state")


def test_bundle_manifest_is_deterministic_and_ordered(bundle_store, recipe_tree):
    root, _child, _prompt = recipe_tree
    first = bundle_store.capture(root, ["prompts/review.md", "child.recipe.yaml"])
    second = bundle_store.capture(root, ["child.recipe.yaml", "prompts/review.md"])

    assert first == second
    manifest = bundle_store.read_manifest(first)
    assert manifest.root == "root.recipe.yaml"
    assert [entry.path for entry in manifest.files] == [
        "child.recipe.yaml",
        "prompts/review.md",
        "root.recipe.yaml",
    ]


@pytest.mark.parametrize("dependency", ["/absolute.yaml", "../outside.yaml", "a/../../outside.yaml"])
def test_bundle_rejects_unsafe_dependency_paths(bundle_store, recipe_tree, dependency):
    from lockstep.runtime.recipe_bundles import UnsafeBundlePath

    root, _child, _prompt = recipe_tree
    with pytest.raises(UnsafeBundlePath):
        bundle_store.capture(root, [dependency])


def test_bundle_rejects_duplicate_normalized_paths(bundle_store, recipe_tree):
    from lockstep.runtime.recipe_bundles import DuplicateBundlePath

    root, _child, _prompt = recipe_tree
    with pytest.raises(DuplicateBundlePath):
        bundle_store.capture(root, ["child.recipe.yaml", "./child.recipe.yaml"])


def test_bundle_rejects_symlink_inputs(bundle_store, recipe_tree):
    from lockstep.runtime.recipe_bundles import SymlinkRejected

    root, child, _prompt = recipe_tree
    alias = root.parent / "alias.recipe.yaml"
    try:
        alias.symlink_to(child.name)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(SymlinkRejected):
        bundle_store.capture(root, ["alias.recipe.yaml"])


def test_bundle_rejects_symlink_source_root(bundle_store, recipe_tree, tmp_path):
    from lockstep.runtime.recipe_bundles import SymlinkRejected

    root, _child, _prompt = recipe_tree
    alias = tmp_path / "source-alias"
    try:
        alias.symlink_to(root.parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(SymlinkRejected):
        bundle_store.capture(alias / root.name, ["child.recipe.yaml"])


def test_materialization_survives_original_parent_and_child_changes(bundle_store, recipe_tree):
    root, child, prompt = recipe_tree
    ref = bundle_store.capture(root, ["child.recipe.yaml", "prompts/review.md"])

    root.write_text("changed parent")
    child.unlink()
    prompt.write_text("changed prompt")
    materialized = bundle_store.materialize_for_compile(ref)

    assert materialized.source_path.read_text() == "include_graph: child.recipe.yaml\n"
    assert (materialized.directory / "child.recipe.yaml").read_text() == "name: child\n"
    assert (materialized.directory / "prompts/review.md").read_text() == "Review carefully.\n"
    assert not os.access(materialized.source_path, os.W_OK)


def test_materialization_rejects_manifest_digest_mismatch(bundle_store, recipe_tree):
    from lockstep.runtime.recipe_bundles import DigestMismatch

    root, _child, _prompt = recipe_tree
    ref = bundle_store.capture(root, ["child.recipe.yaml"])
    path = bundle_store.manifest_path(ref)
    data = json.loads(path.read_text())
    data["root"] = "different.recipe.yaml"
    path.chmod(0o600)
    path.write_text(json.dumps(data))

    with pytest.raises(DigestMismatch):
        bundle_store.materialize_for_compile(ref)


def test_bundle_ref_cannot_escape_owner_state(bundle_store):
    from lockstep.runtime.recipe_bundles import RecipeBundleRef

    with pytest.raises(ValueError):
        bundle_store.manifest_path(RecipeBundleRef("../escape"))


def _replace_manifest_with_symlink(path, outside):
    path.rename(outside)
    try:
        path.symlink_to(outside)
    except OSError:
        outside.rename(path)
        pytest.skip("symlinks unavailable")


def test_bundle_read_rejects_symlink_backed_manifest(bundle_store, recipe_tree, tmp_path):
    from lockstep.runtime.recipe_bundles import SymlinkRejected

    root, _child, _prompt = recipe_tree
    ref = bundle_store.capture(root, ["child.recipe.yaml"])
    _replace_manifest_with_symlink(
        bundle_store.manifest_path(ref), tmp_path / "outside-bundle-manifest.json"
    )

    with pytest.raises(SymlinkRejected):
        bundle_store.read_manifest(ref)


def test_bundle_reuse_rejects_symlink_backed_manifest(bundle_store, recipe_tree, tmp_path):
    from lockstep.runtime.recipe_bundles import SymlinkRejected

    root, _child, _prompt = recipe_tree
    ref = bundle_store.capture(root, ["child.recipe.yaml"])
    _replace_manifest_with_symlink(
        bundle_store.manifest_path(ref), tmp_path / "outside-bundle-manifest.json"
    )

    with pytest.raises(SymlinkRejected):
        bundle_store.capture(root, ["child.recipe.yaml"])


def test_materialization_rejects_symlink_in_existing_tree(bundle_store, recipe_tree):
    from lockstep.runtime.recipe_bundles import SymlinkRejected

    root, _child, _prompt = recipe_tree
    ref = bundle_store.capture(root, ["child.recipe.yaml"])
    materialized = bundle_store.materialize_for_compile(ref)
    child = materialized.directory / "child.recipe.yaml"
    materialized.directory.chmod(0o700)
    child.unlink()
    child.symlink_to(root)

    with pytest.raises(SymlinkRejected):
        bundle_store.materialize_for_compile(ref)


def test_materialization_rejects_writable_existing_root(bundle_store, recipe_tree):
    from lockstep.runtime.recipe_bundles import MaterializationError

    root, _child, _prompt = recipe_tree
    ref = bundle_store.capture(root, ["child.recipe.yaml"])
    materialized = bundle_store.materialize_for_compile(ref)
    materialized.directory.chmod(0o755)

    with pytest.raises(MaterializationError, match="directory is writable"):
        bundle_store.materialize_for_compile(ref)


def test_materialization_rejects_writable_existing_nested_directory(
    bundle_store, recipe_tree
):
    from lockstep.runtime.recipe_bundles import MaterializationError

    root, _child, _prompt = recipe_tree
    ref = bundle_store.capture(root, ["prompts/review.md"])
    materialized = bundle_store.materialize_for_compile(ref)
    (materialized.directory / "prompts").chmod(0o755)

    with pytest.raises(MaterializationError, match="directory is writable"):
        bundle_store.materialize_for_compile(ref)


def test_materialization_rejects_unexpected_empty_directory(bundle_store, recipe_tree):
    from lockstep.runtime.recipe_bundles import MaterializationError

    root, _child, _prompt = recipe_tree
    ref = bundle_store.capture(root, ["child.recipe.yaml"])
    materialized = bundle_store.materialize_for_compile(ref)
    materialized.directory.chmod(0o755)
    (materialized.directory / "unexpected").mkdir()
    materialized.directory.chmod(0o555)
    (materialized.directory / "unexpected").chmod(0o555)

    with pytest.raises(MaterializationError, match="directory layout"):
        bundle_store.materialize_for_compile(ref)


def test_concurrent_capture_and_materialization_reuse_one_bundle(bundle_store, recipe_tree):
    root, _child, _prompt = recipe_tree

    def capture_and_materialize(_index):
        ref = bundle_store.capture(root, ["child.recipe.yaml", "prompts/review.md"])
        return ref, bundle_store.materialize_for_compile(ref).source_path.read_bytes()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(capture_and_materialize, range(16)))

    assert len({ref for ref, _content in results}) == 1
    assert {content for _ref, content in results} == {b"include_graph: child.recipe.yaml\n"}
