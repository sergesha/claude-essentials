"""Public minimal recipe writes through one ready, complete authoring plan."""
from __future__ import annotations

import inspect, stat
from pathlib import Path

import pytest

from lockstep import authoring, cli
from lockstep.authoring_compilation import plan_project_compilation
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.mcp import server
from tests._authoring_gate import (
    expected_compilation_image, mcp_context, observed_compilation_image,
    replace_marker, tree_image, write_workflow,
)
from tests.test_authoring_legacy_v4_refusal import (
    _create_test_namespace,
    _retain,
    live_v4_bytes,
)


def _state(project: Path) -> Path:
    return (project.parent / f"{project.name}-state").resolve()


def _init(adapter, project, monkeypatch, capsys):
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(_state(project)))
    if adapter == "mcp": return server.recipe_init("release", ctx=mcp_context(project))
    monkeypatch.chdir(project); code = cli.main(["recipe", "init", "release"])
    output = capsys.readouterr(); return code, output.out, output.err


def test_direct_recipe_writers_require_explicit_owner_state_without_mutation(tmp_path: Path) -> None:
    project = tmp_path / "project"; project.mkdir(); write_workflow(project, "release")
    before = tree_image(project)
    for boundary, arguments in ((authoring.initialize_minimal, (project, "other")), (authoring.write_compilation, (authoring.project_paths(project, "release"),))):
        state = inspect.signature(boundary).parameters["state_dir"]
        assert state.kind is inspect.Parameter.KEYWORD_ONLY and state.default is inspect.Parameter.empty
        with pytest.raises(TypeError, match="state_dir"): boundary(*arguments)
        assert tree_image(project) == before


@pytest.mark.parametrize("adapter", ("cli", "mcp"))
def test_public_recipe_init_routes_one_complete_plan_through_ready_publisher(
    tmp_path, monkeypatch, capsys, adapter
) -> None:
    project = tmp_path / adapter; project.mkdir(); events = []
    original_plan = authoring.plan_captured_workflow_installation
    original_publish = AuthoringPublisher.publish
    def ready(self, root): events.append(("ready", root))
    def plan(*args, **kwargs):
        value = original_plan(*args, **kwargs); events.append(("plan", value.plan)); return value
    def publish(self, plan): events.append(("publish", plan)); return original_publish(self, plan)
    monkeypatch.setattr(AuthoringPublisher, "require_ready", ready, raising=False)
    monkeypatch.setattr(authoring, "plan_captured_workflow_installation", plan)
    monkeypatch.setattr(AuthoringPublisher, "publish", publish)

    result = _init(adapter, project, monkeypatch, capsys)

    expected = (0, "initialized release\n", "") if adapter == "cli" else {
        "name": "release", "workflow": ".lockstep/workflows/release.workflow.yaml",
        "recipe": ".lockstep/recipes/release.recipe.yaml",
    }
    assert result == expected and [item[0] for item in events] == ["ready", "plan", "publish"]
    plan = events[1][1]
    assert plan.project == project.resolve() and plan.sources == ()
    assert plan.dependency_edges == (("release", ()),)
    assert all(item.before is None for item in plan.targets)
    assert {item.path for item in plan.targets} == {p.resolve() for p in project.rglob("*") if p.is_file()}


def test_recipe_init_rejects_every_occupied_destination_without_mutation(tmp_path, monkeypatch, capsys) -> None:
    reference = tmp_path / "reference"; reference.mkdir(); assert _init("cli", reference, monkeypatch, capsys)[0] == 0
    destinations = tuple(p.relative_to(reference) for p in reference.rglob("*") if p.is_file())
    for index, relative in enumerate(destinations):
        project = tmp_path / f"collision-{index}"; project.mkdir(); collision = project / relative
        collision.parent.mkdir(parents=True); collision.write_bytes(b"foreign\n"); before = tree_image(project)
        assert _init("cli", project, monkeypatch, capsys)[0] == 2
        assert tree_image(project) == before


def test_write_compilation_republishes_complete_changed_child_dag(tmp_path: Path) -> None:
    project = tmp_path / "project"; project.mkdir(); child = write_workflow(project, "child")
    write_workflow(project, "release", children=("child",)); state = _state(project)
    authoring.publish_project_compilation(project, "release", state_dir=state)
    original = (project / ".lockstep/recipes/child.recipe.yaml").read_bytes(); replace_marker(child, "initial", "updated")
    authoring.write_compilation(authoring.project_paths(project, "release"), state_dir=state)
    expected = expected_compilation_image(project, ("child", "release"))
    assert observed_compilation_image(expected) == expected
    assert (project / ".lockstep/recipes/child.recipe.yaml").read_bytes() != original


def test_successful_public_compile_materializes_only_the_planned_namespace(tmp_path: Path) -> None:
    project = tmp_path / "project"; project.mkdir(); source = write_workflow(project, "release")
    sentinel = project / "owner-note.txt"; sentinel.write_bytes(b"owner sentinel\n"); sentinel.chmod(0o640)
    plan = plan_project_compilation(authoring.project_paths(project, "release"))
    before = tree_image(project); expected = {item.path: item for item in plan.targets}

    authoring.publish_project_compilation(project, "release", state_dir=_state(project))

    after = tree_image(project); added = set(after) - set(before)
    planned_files = {path.relative_to(project).as_posix() for path in expected}
    planned_parents = {parent.relative_to(project).as_posix() for path in expected for parent in path.parents if project in parent.parents}
    assert added == (planned_files | planned_parents) - set(before)
    assert source.read_bytes() == before[source.relative_to(project).as_posix()].content
    assert sentinel.read_bytes() == b"owner sentinel\n" and stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    for path, image in expected.items():
        assert path.read_bytes() == image.after and stat.S_IMODE(path.stat().st_mode) == image.mode


def test_recipe_init_refuses_legacy_before_planning(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"; project.mkdir(); state = _state(project)
    namespace, _identity = _create_test_namespace(state, project)
    _retain(namespace, live_v4_bytes(project))
    before_project, before_state = tree_image(project), tree_image(state)
    planned = []
    monkeypatch.setattr(authoring, "plan_captured_workflow_installation", lambda *_a, **_k: planned.append(True))
    with pytest.raises(Exception, match="pre-simplification"):
        authoring.initialize_minimal(project, "release", state_dir=state)
    assert planned == [] and tree_image(project) == before_project and tree_image(state) == before_state
