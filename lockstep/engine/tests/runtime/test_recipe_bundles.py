from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest


class PoisonAfter:
    """Iterable that fails if a bounded consumer asks past supplied values."""

    def __init__(self, values):
        self._values = values

    def __iter__(self):
        yield from self._values
        raise AssertionError("consumer read past max+1")


@pytest.fixture
def recipe_tree(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    root = source / "root.recipe.yaml"
    child = source / "child.recipe.yaml"
    prompts = source / "prompts"
    prompts.mkdir()
    prompt = prompts / "review.md"
    root.write_text("opaque recipe bytes\n")
    child.write_text("opaque child bytes\n")
    prompt.write_text("Review carefully.\n")
    return root, child, prompt


@pytest.fixture
def bundle_store(tmp_path):
    from lockstep.runtime.recipe_bundles import RecipeBundleStore

    return RecipeBundleStore(tmp_path / "owner-state")


def _dag(root, *dependencies):
    from lockstep.runtime.recipe_bundles import ValidatedDependencyDAG

    return ValidatedDependencyDAG.from_validated(
        root.name,
        [root.name, *dependencies],
    )


def _capture(store, root, *dependencies):
    return store.capture(root.parent, _dag(root, *dependencies))


def test_validated_dag_bounds_max_plus_one_without_consuming_poison():
    from lockstep.runtime.owner_state import StorageLimitExceeded
    from lockstep.runtime.recipe_bundles import ValidatedDependencyDAG

    files = PoisonAfter(["root.yaml", "a.yaml", "b.yaml"])
    with pytest.raises(StorageLimitExceeded, match="files"):
        ValidatedDependencyDAG.from_validated(
            "root.yaml",
            files,
            max_files=2,
            max_dependencies=1,
        )


def test_validated_dag_bounds_dependency_iterator_at_max_plus_one():
    from lockstep.runtime.owner_state import StorageLimitExceeded
    from lockstep.runtime.recipe_bundles import ValidatedDependencyDAG

    files = PoisonAfter(["root.yaml", "a.yaml", "b.yaml"])
    with pytest.raises(StorageLimitExceeded, match="dependencies"):
        ValidatedDependencyDAG.from_validated(
            "root.yaml",
            files,
            max_files=100,
            max_dependencies=1,
        )


@pytest.mark.parametrize(
    "files",
    [
        ("root.yaml", "/absolute.yaml"),
        ("root.yaml", "../outside.yaml"),
        ("root.yaml", "a/../../outside.yaml"),
    ],
)
def test_validated_dag_rejects_unsafe_paths(files):
    from lockstep.runtime.recipe_bundles import UnsafeBundlePath, ValidatedDependencyDAG

    with pytest.raises(UnsafeBundlePath):
        ValidatedDependencyDAG.from_validated("root.yaml", files)


def test_validated_dag_rejects_duplicate_or_missing_root():
    from lockstep.runtime.recipe_bundles import (
        DuplicateBundlePath,
        InvalidDependencyDAG,
        ValidatedDependencyDAG,
    )

    with pytest.raises(DuplicateBundlePath):
        ValidatedDependencyDAG.from_validated(
            "root.yaml", ["root.yaml", "./root.yaml"]
        )
    with pytest.raises(InvalidDependencyDAG, match="root"):
        ValidatedDependencyDAG.from_validated("root.yaml", ["child.yaml"])


def test_bundle_capture_requires_validated_dag(bundle_store, recipe_tree):
    root, _child, _prompt = recipe_tree

    with pytest.raises(TypeError, match="ValidatedDependencyDAG"):
        bundle_store.capture(root.parent, [root.name])


def test_bundle_manifest_is_deterministic_and_ordered(bundle_store, recipe_tree):
    root, _child, _prompt = recipe_tree
    first = _capture(
        bundle_store, root, "prompts/review.md", "child.recipe.yaml"
    )
    second = _capture(
        bundle_store, root, "child.recipe.yaml", "prompts/review.md"
    )

    assert first == second
    manifest = bundle_store.read_manifest(first)
    assert manifest.root == "root.recipe.yaml"
    assert [entry.path for entry in manifest.files] == [
        "child.recipe.yaml",
        "prompts/review.md",
        "root.recipe.yaml",
    ]


def test_bundle_rejects_symlink_inputs(bundle_store, recipe_tree):
    from lockstep.runtime.recipe_bundles import SymlinkRejected

    root, child, _prompt = recipe_tree
    alias = root.parent / "alias.recipe.yaml"
    try:
        alias.symlink_to(child.name)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(SymlinkRejected):
        _capture(bundle_store, root, "alias.recipe.yaml")


def test_bundle_rejects_symlink_project_root(bundle_store, recipe_tree, tmp_path):
    from lockstep.runtime.recipe_bundles import SymlinkRejected

    root, _child, _prompt = recipe_tree
    alias = tmp_path / "source-alias"
    try:
        alias.symlink_to(root.parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(SymlinkRejected):
        bundle_store.capture(alias, _dag(root, "child.recipe.yaml"))


def test_materialization_survives_original_file_changes(bundle_store, recipe_tree):
    root, child, prompt = recipe_tree
    ref = _capture(
        bundle_store, root, "child.recipe.yaml", "prompts/review.md"
    )

    root.write_text("changed parent")
    child.unlink()
    prompt.write_text("changed prompt")
    materialized = bundle_store.materialize_for_compile(ref)

    assert materialized.source_path.read_text() == "opaque recipe bytes\n"
    assert (materialized.directory / "child.recipe.yaml").read_text() == (
        "opaque child bytes\n"
    )
    assert (materialized.directory / "prompts/review.md").read_text() == (
        "Review carefully.\n"
    )
    assert not os.access(materialized.source_path, os.W_OK)


def test_materialization_rejects_manifest_digest_mismatch(bundle_store, recipe_tree):
    from lockstep.runtime.recipe_bundles import DigestMismatch

    root, _child, _prompt = recipe_tree
    ref = _capture(bundle_store, root, "child.recipe.yaml")
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


def test_bundle_read_rejects_symlink_backed_manifest(
    bundle_store, recipe_tree, tmp_path
):
    from lockstep.runtime.recipe_bundles import SymlinkRejected

    root, _child, _prompt = recipe_tree
    ref = _capture(bundle_store, root, "child.recipe.yaml")
    _replace_manifest_with_symlink(
        bundle_store.manifest_path(ref), tmp_path / "outside-bundle-manifest.json"
    )

    with pytest.raises(SymlinkRejected):
        bundle_store.read_manifest(ref)


def test_bundle_reuse_rejects_symlink_backed_manifest(
    bundle_store, recipe_tree, tmp_path
):
    from lockstep.runtime.recipe_bundles import SymlinkRejected

    root, _child, _prompt = recipe_tree
    dag = _dag(root, "child.recipe.yaml")
    ref = bundle_store.capture(root.parent, dag)
    _replace_manifest_with_symlink(
        bundle_store.manifest_path(ref), tmp_path / "outside-bundle-manifest.json"
    )

    with pytest.raises(SymlinkRejected):
        bundle_store.capture(root.parent, dag)


def test_materialization_rejects_symlink_in_existing_tree(bundle_store, recipe_tree):
    from lockstep.runtime.recipe_bundles import SymlinkRejected

    root, _child, _prompt = recipe_tree
    ref = _capture(bundle_store, root, "child.recipe.yaml")
    materialized = bundle_store.materialize_for_compile(ref)
    child = materialized.directory / "child.recipe.yaml"
    materialized.directory.chmod(0o700)
    child.unlink()
    child.symlink_to(root)
    materialized.directory.chmod(0o500)

    with pytest.raises(SymlinkRejected):
        bundle_store.materialize_for_compile(ref)


def test_materialization_rejects_writable_existing_root(bundle_store, recipe_tree):
    from lockstep.runtime.recipe_bundles import MaterializationError

    root, _child, _prompt = recipe_tree
    ref = _capture(bundle_store, root, "child.recipe.yaml")
    materialized = bundle_store.materialize_for_compile(ref)
    materialized.directory.chmod(0o755)

    with pytest.raises(MaterializationError, match="directory is writable"):
        bundle_store.materialize_for_compile(ref)


def test_materialization_rejects_non_owner_only_existing_tree(
    bundle_store, recipe_tree
):
    from lockstep.runtime.owner_state import InsecureStatePath

    root, _child, _prompt = recipe_tree
    ref = _capture(bundle_store, root, "child.recipe.yaml")
    materialized = bundle_store.materialize_for_compile(ref)
    materialized.directory.chmod(0o555)

    with pytest.raises(InsecureStatePath, match="owner-only"):
        bundle_store.materialize_for_compile(ref)


def test_materialization_rejects_writable_existing_nested_directory(
    bundle_store, recipe_tree
):
    from lockstep.runtime.recipe_bundles import MaterializationError

    root, _child, _prompt = recipe_tree
    ref = _capture(bundle_store, root, "child.recipe.yaml", "prompts/review.md")
    materialized = bundle_store.materialize_for_compile(ref)
    (materialized.directory / "prompts").chmod(0o755)

    with pytest.raises(MaterializationError, match="directory is writable"):
        bundle_store.materialize_for_compile(ref)


def test_materialization_rejects_unexpected_empty_directory(bundle_store, recipe_tree):
    from lockstep.runtime.recipe_bundles import MaterializationError

    root, _child, _prompt = recipe_tree
    ref = _capture(bundle_store, root, "child.recipe.yaml")
    materialized = bundle_store.materialize_for_compile(ref)
    materialized.directory.chmod(0o755)
    (materialized.directory / "unexpected").mkdir()
    materialized.directory.chmod(0o500)
    (materialized.directory / "unexpected").chmod(0o500)

    with pytest.raises(MaterializationError, match="directory layout"):
        bundle_store.materialize_for_compile(ref)


def test_concurrent_capture_and_materialization_reuse_one_bundle(
    bundle_store, recipe_tree
):
    root, _child, _prompt = recipe_tree
    dag = _dag(root, "child.recipe.yaml", "prompts/review.md")

    def capture_and_materialize(_index):
        ref = bundle_store.capture(root.parent, dag)
        return ref, bundle_store.materialize_for_compile(ref).source_path.read_bytes()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(capture_and_materialize, range(16)))

    assert len({ref for ref, _content in results}) == 1
    assert {content for _ref, content in results} == {b"opaque recipe bytes\n"}


def test_bundle_limits_fail_before_manifest_or_blob_publication(tmp_path, recipe_tree):
    from lockstep.runtime.recipe_bundles import (
        RecipeBundleLimits,
        RecipeBundleStore,
        StorageLimitExceeded,
    )

    root, _child, _prompt = recipe_tree
    owner = tmp_path / "limited-state"
    store = RecipeBundleStore(
        owner,
        limits=RecipeBundleLimits(
            max_dependencies=1,
            max_files=2,
            max_total_bytes=1024,
            max_manifest_bytes=1024,
        ),
    )

    with pytest.raises(StorageLimitExceeded, match="dependencies"):
        store.capture(
            root.parent,
            _dag(root, "child.recipe.yaml", "prompts/review.md"),
        )
    assert not list((owner / "recipe-bundles").glob("*.json"))
    assert not list((owner / "blobs" / "sha256").rglob("[0-9a-f]" * 64))


def test_bundle_growth_after_fstat_reads_only_remaining_budget_plus_one(
    tmp_path, monkeypatch
):
    from lockstep.runtime.owner_state import StorageLimitExceeded
    from lockstep.runtime.recipe_bundles import RecipeBundleLimits, RecipeBundleStore

    source = tmp_path / "growing-source"
    source.mkdir()
    root = source / "root.yaml"
    root.write_bytes(b"root")
    growing = source / "growing.bin"
    growing.write_bytes(b"ok")
    growing_inode = growing.stat().st_ino
    owner = tmp_path / "growing-state"
    store = RecipeBundleStore(owner, limits=RecipeBundleLimits(max_total_bytes=8))
    dag = _dag(root, growing.name)
    original_fstat = os.fstat
    original_fdopen = os.fdopen
    grew = False
    read_sizes = []

    def growing_fstat(descriptor):
        nonlocal grew
        info = original_fstat(descriptor)
        if info.st_ino == growing_inode and not grew:
            with growing.open("ab") as stream:
                stream.write(b"overflow")
            grew = True
        return info

    class RecordingReader:
        def __init__(self, stream):
            self._stream = stream

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def read(self, size=-1):
            read_sizes.append(size)
            if size != 5:
                raise AssertionError(f"unexpected read size {size}")
            return self._stream.read(size)

    def recording_fdopen(descriptor, *args, **kwargs):
        stream = original_fdopen(descriptor, *args, **kwargs)
        if original_fstat(descriptor).st_ino == growing_inode:
            return RecordingReader(stream)
        return stream

    monkeypatch.setattr(os, "fstat", growing_fstat)
    monkeypatch.setattr(os, "fdopen", recording_fdopen)

    with pytest.raises(StorageLimitExceeded, match="byte admission"):
        store.capture(source, dag)

    assert grew
    assert read_sizes == [5]
    assert not list((owner / "recipe-bundles").glob("*.json"))
    assert not list((owner / "blobs" / "sha256").rglob("[0-9a-f]" * 64))


def test_bundle_rejects_symlink_in_intermediate_component(
    bundle_store, recipe_tree
):
    from lockstep.runtime.recipe_bundles import SymlinkRejected

    root, _child, _prompt = recipe_tree
    alias = root.parent / "prompt-alias"
    try:
        alias.symlink_to("prompts", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(SymlinkRejected):
        _capture(bundle_store, root, "prompt-alias/review.md")


def test_bundle_rejects_fifo_without_blocking(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs unavailable")
    source = tmp_path / "fifo-source"
    source.mkdir()
    root = source / "root.yaml"
    root.write_text("opaque\n")
    fifo = source / "input.yaml"
    os.mkfifo(fifo)
    program = """
from pathlib import Path
import sys
from lockstep.runtime.recipe_bundles import RecipeBundleStore, ValidatedDependencyDAG
try:
    dag = ValidatedDependencyDAG.from_validated("root.yaml", ["root.yaml", "input.yaml"])
    RecipeBundleStore(Path(sys.argv[1])).capture(Path(sys.argv[2]), dag)
except (ValueError, RuntimeError):
    raise SystemExit(0)
raise SystemExit(1)
"""

    completed = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "fifo-state"), str(source)],
        timeout=2,
        check=False,
    )
    assert completed.returncode == 0


def test_bundle_capture_uses_held_root_descriptor_during_parent_swap(
    bundle_store, recipe_tree, monkeypatch
):
    root, child, _prompt = recipe_tree
    original_open = os.open
    swapped = root.parent.with_name("source-swapped")
    replacement = root.parent.with_name("source-replacement")
    replacement.mkdir()
    (replacement / root.name).write_text("replacement root\n")
    (replacement / child.name).write_text("hostile replacement\n")
    triggered = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal triggered
        if path == child.name and dir_fd is not None and not triggered:
            triggered = True
            root.parent.rename(swapped)
            replacement.rename(root.parent)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)
    try:
        ref = bundle_store.capture(root.parent, _dag(root, child.name))
    finally:
        monkeypatch.setattr(os, "open", original_open)

    materialized = bundle_store.materialize_for_compile(ref)
    assert (materialized.directory / child.name).read_bytes() == (
        b"opaque child bytes\n"
    )
