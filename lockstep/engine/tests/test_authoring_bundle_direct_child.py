"""Child-first planning captures one complete generated closure without writes."""
from __future__ import annotations

import hashlib
from pathlib import Path

from lockstep.authoring import project_paths
from lockstep.authoring_compilation import plan_project_compilation, workflow_call_names
from lockstep.workflow.schema import load_workflow_bytes
from tests._authoring_gate import assert_source_identity, expected_compilation_image, tree_image, write_workflow


def _assert_plan(tmp_path: Path, roles: tuple[str, ...], edges) -> None:
    project = tmp_path / "project"
    paths = {}
    for index, role in enumerate(roles):
        children = () if index == 0 else (roles[index - 1],)
        paths[role] = write_workflow(project, role, children=children)
    expected = {}
    seen = set()
    for role in roles:
        closure = expected_compilation_image(project, (role,))
        expected[role] = {
            path: content for path, content in closure.items() if path not in seen
        }
        seen.update(closure)
    before = tree_image(tmp_path)

    plan = plan_project_compilation(project_paths(project, roles[-1]))

    assert tree_image(tmp_path) == before
    assert tuple(source.role for source in plan.sources) == roles
    assert plan.dependency_edges == edges
    for source in plan.sources:
        assert_source_identity(source, project, paths[source.role])
    assert plan.project_identity == plan.sources[0].parents[0]
    assert all(source.parents == plan.sources[0].parents for source in plan.sources)
    expected_paths = {role: set(images) for role, images in expected.items()}
    assert sum(map(len, expected_paths.values())) == len(set().union(*expected_paths.values()))
    for target in plan.targets:
        assert target.before is None
        assert target.before_sha256 is None
        assert target.before_file is None
        assert target.after == expected[target.role][target.path]
        assert target.after_sha256 == hashlib.sha256(target.after).hexdigest()
        assert target.mode == 0o644
        assert target.parents[0] == plan.project_identity
    assert {
        role: {item.path for item in plan.targets if item.role == role}
        for role in roles
    } == expected_paths
    generated = {
        role: {
            path: content for path, content in expected[role].items()
            if path not in {
                project_paths(project, role).recipe_path,
                project_paths(project, role).dependency_path,
                project_paths(project, role).source_map_path,
            }
        }
        for role in roles
    }
    assert all(generated[role] for role in roles[1:])
    for role in roles[1:]:
        assert {
            item.path: item.after for item in plan.targets
            if item.role == role and item.path in generated[role]
        } == generated[role]


def test_direct_child_planner_captures_complete_generated_bundle_without_writes(tmp_path: Path) -> None:
    _assert_plan(tmp_path, ("child", "parent"), (("child", ()), ("parent", ("child",))))


def test_transitive_planner_captures_complete_three_role_bundle_without_writes(tmp_path: Path) -> None:
    _assert_plan(
        tmp_path,
        ("grandchild", "child", "parent"),
        (("grandchild", ()), ("child", ("grandchild",)), ("parent", ("child",))),
    )


def test_call_topology_follows_only_typed_executable_flow_in_declaration_order(
    tmp_path: Path,
) -> None:
    source = b"""\
workflow_version: '1'
name: parent
description: parent
protect: ['**']
x-shadow: {call: {workflow: metadata-only}}
flow:
  - call: {workflow: first, runner: codex}
  - choose:
      value: decision
      cases:
        one: [{call: {workflow: second, runner: codex}}]
      default: [{call: {workflow: third, runner: codex}}]
  - repeat:
      limit: 1
      until: done.passed
      do: [{call: {workflow: fourth, runner: codex}}]
      exhausted: escalate
  - parallel:
      join: all
      branches:
        left: [{call: {workflow: fifth, runner: codex}}]
        right: [{call: {workflow: first, runner: codex}}]
"""

    calls = workflow_call_names(
        load_workflow_bytes(tmp_path / "parent.workflow.yaml", source)
    )

    assert calls == ("first", "second", "third", "fourth", "fifth")


def test_inert_call_shaped_metadata_cannot_add_dependencies_or_outputs(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    write_workflow(project, "child")
    parent = write_workflow(project, "parent", children=("child",))
    parent.write_text(
        parent.read_text() + "x-shadow: {call: {workflow: metadata-only}}\n"
    )

    plan = plan_project_compilation(project_paths(project, "parent"))

    assert plan.dependency_edges == (("child", ()), ("parent", ("child",)))
    assert {target.role for target in plan.targets} == {"child", "parent"}
