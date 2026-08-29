"""Child-first planning captures one complete generated closure without writes."""
from __future__ import annotations

import hashlib
from pathlib import Path

from lockstep.authoring import project_paths
from lockstep.authoring_bundle import plan_project_compilation
from tests._authoring_gate import assert_source_identity, expected_compilation_image, tree_image, write_workflow


def _assert_plan(tmp_path: Path, roles: tuple[str, ...], edges) -> None:
    project = tmp_path / "project"; paths = {}
    for index, role in enumerate(roles):
        children = () if index == 0 else (roles[index - 1],)
        paths[role] = write_workflow(project, role, children=children)
    expected = {}; seen = set()
    for role in roles:
        closure = expected_compilation_image(project, (role,))
        expected[role] = {path: content for path, content in closure.items() if path not in seen}
        seen.update(closure)
    before = tree_image(tmp_path)

    plan = plan_project_compilation(project_paths(project, roles[-1]))

    assert tree_image(tmp_path) == before
    assert tuple(source.role for source in plan.sources) == roles
    assert plan.dependency_edges == edges
    for source in plan.sources: assert_source_identity(source, project, paths[source.role])
    assert plan.project_identity == plan.sources[0].ancestors[0]
    assert all(source.ancestors == plan.sources[0].ancestors for source in plan.sources)
    expected_paths = {role: set(images) for role, images in expected.items()}
    assert sum(map(len, expected_paths.values())) == len(set().union(*expected_paths.values()))
    for before_image, after in zip(plan.before_images, plan.after_images, strict=True):
        assert before_image.role == after.role and before_image.resolved_path == after.resolved_path
        assert (before_image.content, before_image.sha256, before_image.mode, before_image.leaf) == (None, None, None, None)
        assert after.content == expected[after.role][after.resolved_path]
        assert after.sha256 == hashlib.sha256(after.content).hexdigest()
        assert after.mode == 0o644 and after.leaf is None
        assert before_image.ancestors == after.ancestors
        assert before_image.ancestors[0] == plan.project_identity
    for images in (plan.before_images, plan.after_images):
        assert {role: {item.resolved_path for item in images if item.role == role} for role in roles} == expected_paths
    generated = {
        role: {path: content for path, content in expected[role].items() if path not in {
            project_paths(project, role).recipe_path,
            project_paths(project, role).dependency_path,
            project_paths(project, role).source_map_path,
        }} for role in roles
    }
    assert all(generated[role] for role in roles[1:])
    for role in roles[1:]:
        assert {item.resolved_path: item.content for item in plan.after_images if item.role == role and item.resolved_path in generated[role]} == generated[role]


def test_direct_child_planner_captures_complete_generated_bundle_without_writes(tmp_path: Path) -> None:
    _assert_plan(tmp_path, ("child", "parent"), (("child", ()), ("parent", ("child",))))


def test_transitive_planner_captures_complete_three_role_bundle_without_writes(tmp_path: Path) -> None:
    _assert_plan(tmp_path, ("grandchild", "child", "parent"), (("grandchild", ()), ("child", ("grandchild",)), ("parent", ("child",))))
