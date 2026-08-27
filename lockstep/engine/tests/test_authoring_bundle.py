"""Gate C: one authoring command denotes one immutable whole-DAG plan."""

from __future__ import annotations

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
