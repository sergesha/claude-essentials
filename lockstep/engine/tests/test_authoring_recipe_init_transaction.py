"""Transactional boundary for minimal recipe initialization."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from lockstep import authoring, cli
from lockstep.authoring_bundle import ProjectCompilationBundle
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.mcp import server

from tests._authoring_crash_gate import namespace_image
from tests._authoring_gate import (
    expected_compilation_image,
    mcp_context,
    observed_compilation_image,
    replace_marker,
    write_workflow,
)


def _owner_state(project: Path) -> Path:
    return (project.parent / f"{project.name}-owner-state").resolve()


def _invoke_init(
    adapter: str,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> object:
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(_owner_state(project)))
    if adapter == "cli":
        monkeypatch.chdir(project)
        result = cli.main(["recipe", "init", "release"])
        captured = capsys.readouterr()
        return result, captured.out, captured.err
    return server.recipe_init("release", ctx=mcp_context(project))


def test_direct_recipe_writers_require_explicit_owner_state_without_mutation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = write_workflow(project, "release")
    recipe = authoring.project_paths(project, "release")
    before = namespace_image(project)

    for boundary, arguments in (
        (authoring.initialize_minimal, (project, "other")),
        (authoring.write_compilation, (recipe,)),
    ):
        state = inspect.signature(boundary).parameters["state_dir"]
        assert state.kind is inspect.Parameter.KEYWORD_ONLY
        assert state.default is inspect.Parameter.empty
        with pytest.raises(TypeError, match="state_dir"):
            boundary(*arguments)
        assert namespace_image(project) == before

    assert source.is_file()


@pytest.mark.parametrize("adapter", ("cli", "mcp"))
def test_public_recipe_init_routes_one_complete_bundle_through_owner_publisher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adapter: str,
) -> None:
    project = tmp_path / adapter
    project.mkdir()
    owner_state = _owner_state(project)
    project_before = namespace_image(project)
    constructed_with: list[Path] = []
    events: list[tuple[str, object]] = []
    original_init = AuthoringPublisher.__init__
    original_recover = AuthoringPublisher.recover
    original_publish = AuthoringPublisher.publish
    original_plan = authoring.plan_captured_workflow_installation

    def initialize(publisher: AuthoringPublisher, state: Path) -> None:
        constructed_with.append(state)
        original_init(publisher, state)

    def recover(publisher: AuthoringPublisher, observed_project: Path) -> None:
        events.append(("recover", observed_project))
        original_recover(publisher, observed_project)

    def publish(
        publisher: AuthoringPublisher, bundle: ProjectCompilationBundle
    ) -> None:
        assert namespace_image(project) == project_before
        events.append(("publish", bundle))
        original_publish(publisher, bundle)

    def plan(*args, **kwargs):
        events.append(("plan", project))
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(AuthoringPublisher, "__init__", initialize)
    monkeypatch.setattr(AuthoringPublisher, "recover", recover)
    monkeypatch.setattr(AuthoringPublisher, "publish", publish)
    monkeypatch.setattr(authoring, "plan_captured_workflow_installation", plan)

    result = _invoke_init(adapter, project, monkeypatch, capsys)

    expected_result = (
        (0, "initialized release\n", "")
        if adapter == "cli"
        else {
            "name": "release",
            "workflow": ".lockstep/workflows/release.workflow.yaml",
            "recipe": ".lockstep/recipes/release.recipe.yaml",
        }
    )
    assert result == expected_result
    assert constructed_with == [owner_state]
    assert [event[0] for event in events] == ["recover", "plan", "publish"]
    assert events[0][1] == project.resolve()
    bundle = events[2][1]
    assert isinstance(bundle, ProjectCompilationBundle)
    assert bundle.resolved_project == project.resolve()
    assert bundle.sources == ()
    assert bundle.dependency_edges == (("release", ()),)
    assert all(image.content is None for image in bundle.before_images)
    assert {image.resolved_path for image in bundle.after_images} == {
        path.resolve() for path in project.rglob("*") if path.is_file()
    }


def test_recipe_init_rejects_every_occupied_destination_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    assert _invoke_init("cli", reference, monkeypatch, capsys) == (
        0,
        "initialized release\n",
        "",
    )
    destinations = tuple(
        path.relative_to(reference)
        for path in reference.rglob("*")
        if path.is_file()
    )
    assert len(destinations) >= 4

    for index, relative in enumerate(destinations):
        project = tmp_path / f"collision-{index}"
        project.mkdir()
        collision = project / relative
        collision.parent.mkdir(parents=True)
        collision.write_bytes(b"foreign bytes\n")
        collision.chmod(0o640)
        before = namespace_image(project)

        result = _invoke_init("cli", project, monkeypatch, capsys)

        assert result[0] == 2
        assert str(relative) in result[2]
        assert namespace_image(project) == before


def test_write_compilation_republishes_the_complete_changed_child_dag(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    child = write_workflow(project, "child", marker="initial")
    write_workflow(project, "release", children=("child",))
    owner_state = _owner_state(project)
    authoring.publish_project_compilation(
        project, "release", state_dir=owner_state
    )
    child_recipe = project / ".lockstep/recipes/child.recipe.yaml"
    original_child = child_recipe.read_bytes()
    replace_marker(child, "initial", "updated")

    result = authoring.write_compilation(
        authoring.project_paths(project, "release"), state_dir=owner_state
    )

    expected = expected_compilation_image(project, ("child", "release"))
    assert observed_compilation_image(expected) == expected
    assert child_recipe.read_bytes() != original_child
    assert result == authoring.compile_project_source(
        project / ".lockstep/workflows/release.workflow.yaml"
    )[2]


def test_direct_write_compilation_uses_owner_state_and_recovers_before_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_workflow(project, "release")
    recipe = authoring.project_paths(project, "release")
    owner_state = _owner_state(project)
    constructed_with: list[Path] = []
    events: list[str] = []
    original_init = AuthoringPublisher.__init__
    original_recover = AuthoringPublisher.recover
    original_plan = authoring._plan_project_compilation
    original_publish = AuthoringPublisher.publish

    def initialize(publisher: AuthoringPublisher, state: Path) -> None:
        constructed_with.append(state)
        original_init(publisher, state)

    def recover(publisher: AuthoringPublisher, observed_project: Path) -> None:
        assert observed_project == project.resolve()
        events.append("recover")
        original_recover(publisher, observed_project)

    def plan(observed_recipe):
        events.append("plan")
        return original_plan(observed_recipe)

    def publish(
        publisher: AuthoringPublisher, bundle: ProjectCompilationBundle
    ) -> None:
        events.append("publish")
        original_publish(publisher, bundle)

    monkeypatch.setattr(AuthoringPublisher, "__init__", initialize)
    monkeypatch.setattr(AuthoringPublisher, "recover", recover)
    monkeypatch.setattr(authoring, "_plan_project_compilation", plan)
    monkeypatch.setattr(AuthoringPublisher, "publish", publish)

    authoring.write_compilation(recipe, state_dir=owner_state)

    assert constructed_with == [owner_state]
    assert events == ["recover", "plan", "publish"]
