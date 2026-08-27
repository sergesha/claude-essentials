"""Gate C: one authoring command denotes one immutable whole-DAG plan."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest

from lockstep.authoring import (
    AuthoringError,
    canonical_match,
    check_recipe,
    diff_recipe,
    project_paths,
)

from tests._authoring_gate import (
    compile_closure,
    expected_compilation_image,
    observed_compilation_image,
    public_compile,
    replace_marker,
    tree_image,
    write_workflow,
)


def test_whole_dag_bundle_contracts_are_explicit() -> None:
    from lockstep.authoring import AuthoredRecipe as PublicAuthoredRecipe
    from lockstep.authoring_bundle import (
        AuthoredRecipe,
        DestinationImage,
        ProjectCompilationBundle,
        SourceIdentity,
    )

    assert all(
        isinstance(contract, type)
        for contract in (SourceIdentity, DestinationImage, ProjectCompilationBundle)
    )
    assert PublicAuthoredRecipe is AuthoredRecipe


def test_authoring_publisher_surface_has_only_the_frozen_operations() -> None:
    from lockstep.authoring_publisher import AuthoringPublisher

    assert tuple(inspect.signature(AuthoringPublisher.__init__).parameters) == (
        "self",
        "state_dir",
    )
    assert tuple(inspect.signature(AuthoringPublisher.publish).parameters) == (
        "self",
        "bundle",
    )
    assert tuple(inspect.signature(AuthoringPublisher.recover).parameters) == (
        "self",
        "project",
    )


def _assert_leaf_source_identity(bundle, project: Path, source_path: Path) -> None:
    project_info = project.lstat()
    assert bundle.resolved_project == project.resolve()
    assert (bundle.project_identity.device, bundle.project_identity.inode) == (
        project_info.st_dev,
        project_info.st_ino,
    )
    assert tuple(source.role for source in bundle.sources) == ("leaf",)
    source = bundle.sources[0]
    source_info = source_path.lstat()
    assert source.resolved_path == source_path.resolve()
    assert source.content == source_path.read_bytes()
    assert source.sha256 == hashlib.sha256(source.content).hexdigest()
    assert (
        source.leaf.device,
        source.leaf.inode,
        source.leaf.mode,
        source.leaf.size,
        source.leaf.mtime_ns,
    ) == (
        source_info.st_dev,
        source_info.st_ino,
        source_info.st_mode,
        source_info.st_size,
        source_info.st_mtime_ns,
    )
    expected_paths = (
        project.resolve(),
        (project / ".lockstep").resolve(),
        source_path.parent.resolve(),
    )
    assert tuple(item.resolved_path for item in source.ancestors) == expected_paths
    assert tuple(
        (item.device, item.inode) for item in source.ancestors
    ) == tuple((path.lstat().st_dev, path.lstat().st_ino) for path in expected_paths)
    assert bundle.dependency_edges == (("leaf", ()),)


def _assert_identity_paths(identities, expected_paths: tuple[Path, ...]) -> None:
    assert tuple(item.resolved_path for item in identities) == expected_paths
    assert tuple((item.device, item.inode) for item in identities) == tuple(
        (path.lstat().st_dev, path.lstat().st_ino) for path in expected_paths
    )


def _assert_leaf_destination_images(
    bundle, project: Path, expected: dict[Path, bytes]
) -> None:
    before_paths = tuple(image.resolved_path for image in bundle.before_images)
    after_paths = tuple(image.resolved_path for image in bundle.after_images)
    assert len(before_paths) == len(set(before_paths)) == len(expected)
    assert len(after_paths) == len(set(after_paths)) == len(expected)
    assert before_paths == after_paths
    assert {image.resolved_path: image.content for image in bundle.after_images} == expected
    assert all(
        image.content is None
        and image.sha256 is None
        and image.mode is None
        and image.leaf is None
        for image in bundle.before_images
    )
    assert all(
        image.content is not None
        and image.sha256 == hashlib.sha256(image.content).hexdigest()
        and image.mode == 0o644
        and image.leaf is None
        for image in bundle.after_images
    )
    expected_ancestors = (project.resolve(), (project / ".lockstep").resolve())
    for before, after in zip(bundle.before_images, bundle.after_images, strict=True):
        _assert_identity_paths(before.ancestors, expected_ancestors)
        _assert_identity_paths(after.ancestors, expected_ancestors)


def _assert_leaf_bundle_is_deeply_immutable(bundle) -> None:
    source = bundle.sources[0]
    original = (
        bundle.sources,
        bundle.dependency_edges,
        bundle.before_images,
        bundle.after_images,
        source,
    )
    for target, attribute, replacement in (
        (bundle, "sources", ()),
        (bundle.project_identity, "inode", bundle.project_identity.inode + 1),
        (
            source.ancestors[0],
            "inode",
            source.ancestors[0].inode + 1,
        ),
        (source, "content", b"changed"),
        (source.leaf, "inode", source.leaf.inode + 1),
        (bundle.before_images[0], "content", b"changed"),
        (bundle.after_images[0], "content", b"changed"),
    ):
        with pytest.raises((AttributeError, TypeError)):
            setattr(target, attribute, replacement)
    assert original == (
        bundle.sources,
        bundle.dependency_edges,
        bundle.before_images,
        bundle.after_images,
        source,
    )


def test_leaf_planner_captures_exact_immutable_bundle_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.authoring_bundle import (
        ProjectCompilationBundle,
        plan_project_compilation,
    )

    project = tmp_path / "project"
    source_path = write_workflow(project, "leaf")
    controlled_cwd = tmp_path / "controlled-cwd"
    controlled_cwd.mkdir()
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "owner-state"))
    monkeypatch.chdir(controlled_cwd)
    before = tree_image(tmp_path)

    bundle = plan_project_compilation(project_paths(project, "leaf"))

    assert isinstance(bundle, ProjectCompilationBundle)
    assert tree_image(tmp_path) == before
    assert isinstance(bundle.sources, tuple)
    assert isinstance(bundle.dependency_edges, tuple)
    assert isinstance(bundle.before_images, tuple)
    assert isinstance(bundle.after_images, tuple)
    _assert_leaf_source_identity(bundle, project, source_path)
    full_expected = expected_compilation_image(project, ("leaf",))
    expected_paths = {
        (project / ".lockstep/recipes/leaf.recipe.yaml").resolve(),
        (project / ".lockstep/recipes/leaf.dependencies.json").resolve(),
        (project / ".lockstep/recipes/leaf.source-map.json").resolve(),
    }
    assert set(full_expected) == expected_paths
    _assert_leaf_destination_images(bundle, project, full_expected)
    assert {
        image.resolved_path for image in bundle.after_images
    } == expected_paths
    _assert_leaf_bundle_is_deeply_immutable(bundle)


@pytest.mark.parametrize("template", ("reviewed-change", "parallel-review"))
@pytest.mark.parametrize("adapter", ("cli", "mcp"))
def test_parent_compile_rebuilds_changed_direct_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adapter: str,
    template: str,
) -> None:
    from lockstep.templates import install_template, show_template

    project = tmp_path / "project"
    install_template(template, "release", project)
    shown = show_template(template, "release")
    changed_child = next(name for name in shown.compile_order if name != "release")
    child = project / ".lockstep/workflows" / f"{changed_child}.workflow.yaml"
    child.write_text(child.read_text(encoding="utf-8") + "\n# changed child\n")

    result = public_compile(adapter, project, "release", monkeypatch)
    captured = capsys.readouterr()
    if adapter == "cli":
        assert result == 0
        assert captured.err == ""
    else:
        assert result["name"] == "release"

    for name in shown.compile_order:
        assert diff_recipe(project, name) == ""
        assert check_recipe(project, name)["ok"] is True
        canonical_match(project_paths(project, name))
    expected = expected_compilation_image(project, shown.compile_order)
    assert observed_compilation_image(expected) == expected


def test_parent_compile_rebuilds_transitive_grandchild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    leaf = write_workflow(project, "leaf")
    write_workflow(project, "child", children=("leaf",))
    write_workflow(project, "parent", children=("child",))
    compile_closure(project, "leaf", "child", "parent")
    replace_marker(leaf, "initial", "changed")

    assert public_compile("cli", project, "parent", monkeypatch) == 0
    capsys.readouterr()

    for name in ("leaf", "child", "parent"):
        assert diff_recipe(project, name) == ""
        canonical_match(project_paths(project, name))
    expected = expected_compilation_image(project, ("leaf", "child", "parent"))
    assert observed_compilation_image(expected) == expected


def test_parent_check_and_diff_cover_child_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from lockstep import cli

    project = tmp_path / "project"
    child = write_workflow(project, "child")
    write_workflow(project, "parent", children=("child",))
    compile_closure(project, "child", "parent")
    replace_marker(child, "initial", "changed")

    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "owner-state"))
    monkeypatch.chdir(project)
    assert cli.main(["recipe", "diff", "parent"]) == 0
    assert capsys.readouterr().out != ""
    assert cli.main(["recipe", "check", "parent"]) == 2
    capsys.readouterr()
    assert diff_recipe(project, "parent") != ""
    with pytest.raises(AuthoringError, match="canonical|byte-for-byte|missing"):
        check_recipe(project, "parent")

    assert public_compile("cli", project, "parent", monkeypatch) == 0
    capsys.readouterr()

    assert diff_recipe(project, "parent") == ""
    assert check_recipe(project, "parent")["ok"] is True
    assert diff_recipe(project, "child") == ""
    assert check_recipe(project, "child")["ok"] is True
    expected = expected_compilation_image(project, ("child", "parent"))
    assert observed_compilation_image(expected) == expected
    assert cli.main(["recipe", "diff", "parent"]) == 0
    assert capsys.readouterr().out == ""
    assert cli.main(["recipe", "check", "parent"]) == 0


@pytest.mark.parametrize("failure", ("parse", "semantic", "missing", "cycle"))
def test_compile_planning_failure_is_write_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    project = tmp_path / "project"
    state = tmp_path / "owner-state"
    child = write_workflow(project, "child")
    parent = write_workflow(project, "parent", children=("child",))
    compile_closure(project, "child", "parent")

    if failure == "parse":
        child.write_text("not: [valid", encoding="utf-8")
    elif failure == "semantic":
        child.write_text(child.read_text().replace("protect: ['**']", "protect: ['src/**']"))
    elif failure == "missing":
        child.rename(child.with_suffix(".missing"))
    else:
        parent.write_text(parent.read_text().replace("workflow: child", "workflow: parent"))
    expected_project = tree_image(project)

    from lockstep import cli

    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state))
    monkeypatch.chdir(project)
    assert cli.main(["recipe", "compile", "parent"]) == 2
    capsys.readouterr()

    assert tree_image(project) == expected_project
