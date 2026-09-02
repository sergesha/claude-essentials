"""Frozen immutable contracts for one whole-project authoring plan."""
from __future__ import annotations

import dataclasses
import hashlib
import inspect
import stat
from pathlib import Path

import pytest
from lockstep.authoring import project_paths
from lockstep.authoring_bundle import (
    AuthoringPlan,
    DirectoryIdentity,
    FileIdentity,
    PlannedTarget,
    ProjectCompilation,
    SourceSnapshot,
)
from lockstep.authoring_compilation import compile_project, plan_project_compilation
from lockstep.authoring_publisher import AuthoringPublisher

from tests._authoring_gate import write_workflow


def _plan(tmp_path: Path) -> AuthoringPlan:
    project = tmp_path / "project"
    project.mkdir()
    write_workflow(project, "child")
    write_workflow(project, "parent", children=("child",))
    return plan_project_compilation(project_paths(project, "parent"))


def test_whole_dag_plan_contracts_are_exact() -> None:
    expected = {
        DirectoryIdentity: ("path", "device", "inode"),
        FileIdentity: ("device", "inode", "mode", "size", "mtime_ns", "ctime_ns"),
        SourceSnapshot: ("role", "path", "content", "sha256", "file", "parents"),
        PlannedTarget: (
            "role", "path", "before", "before_sha256", "before_file",
            "after", "after_sha256", "mode", "parents",
        ),
        AuthoringPlan: ("project", "project_identity", "sources", "dependency_edges", "targets"),
        ProjectCompilation: ("plan", "root_validated", "root_catalog", "root_result"),
    }
    assert {
        contract: tuple(field.name for field in dataclasses.fields(contract))
        for contract in expected
    } == expected


def test_publisher_surface_has_only_frozen_authoring_operations() -> None:
    operations = {
        name for name, value in inspect.getmembers(AuthoringPublisher, inspect.isfunction)
        if not name.startswith("_")
    }
    assert operations == {"require_ready", "publish", "observe"}


def test_compile_project_retains_same_pass_root_and_child_first_plan(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_workflow(project, "child")
    write_workflow(project, "parent", children=("child",))
    compilation = compile_project(project_paths(project, "parent"))
    assert isinstance(compilation, ProjectCompilation)
    assert compilation.plan.dependency_edges == (("child", ()), ("parent", ("child",)))
    assert tuple(source.role for source in compilation.plan.sources) == ("child", "parent")
    assert {target.role for target in compilation.plan.targets} == {"child", "parent"}


def test_compile_project_resolves_captured_include_graph(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workflows = project / ".lockstep" / "workflows"
    fragments = workflows / "fragments"
    fragments.mkdir(parents=True)
    fragment = fragments / "shared.graph.yaml"
    fragment.write_text(
        "fragment:\n"
        "  entry: begin\n"
        "  exits: {pass: finish}\n"
        "  effects: {mode: read-only, writes: []}\n"
        "state: {note: str}\n"
        "nodes:\n"
        "  begin: {type: passthrough, output: {note: included}}\n"
        "  finish: {type: passthrough}\n"
        "edges:\n"
        "  - {from: begin, to: finish}\n"
    )
    source = workflows / "included.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\n"
        "name: included\n"
        "description: included graph\n"
        "protect: ['**']\n"
        "flow:\n"
        "  - include_graph:\n"
        "      id: shared\n"
        "      path: fragments/shared.graph.yaml\n"
        "      on: {pass: next}\n"
    )

    compilation = compile_project(project_paths(project, "included"))

    captured = {item.path: item for item in compilation.plan.sources}
    assert set(captured) == {source.resolve(), fragment.resolve()}
    assert captured[source.resolve()].content == source.read_bytes()
    assert captured[fragment.resolve()].content == fragment.read_bytes()
    resolved = compilation.root_catalog.fragment_for(
        "fragments/shared.graph.yaml"
    )
    assert resolved is not None
    assert resolved.source_definition_sha256 == hashlib.sha256(
        fragment.read_bytes()
    ).hexdigest()
    dependency = compilation.root_result.dependency_manifest.entries
    assert [
        (item.kind, item.logical_name, item.use_pointer, item.definition_sha256)
        for item in dependency
    ] == [
        (
            "fragment",
            "fragments/shared.graph.yaml",
            "/flow/0",
            hashlib.sha256(fragment.read_bytes()).hexdigest(),
        )
    ]


def test_targets_are_unique_owned_and_project_bound(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    paths = tuple(target.path for target in plan.targets)
    assert len(paths) == len(set(paths))
    assert all(target.parents[0] == plan.project_identity for target in plan.targets)


def test_plan_rejects_partial_inventory_or_open_topology(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    with pytest.raises(ValueError, match="complete or empty"):
        dataclasses.replace(plan, sources=plan.sources[:1])
    with pytest.raises(ValueError, match="complete or empty"):
        dataclasses.replace(plan, sources=(*plan.sources, plan.sources[0]))
    with pytest.raises(ValueError, match="non-empty"):
        dataclasses.replace(plan, sources=(), dependency_edges=())
    with pytest.raises(ValueError, match="earlier child"):
        dataclasses.replace(
            plan,
            dependency_edges=(("parent", ("child",)), ("child", ())),
        )


def test_plan_rejects_unowned_duplicate_or_foreign_parent_targets(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    foreign = dataclasses.replace(plan.targets[0], role="foreign")
    with pytest.raises(ValueError, match="roles"):
        dataclasses.replace(plan, targets=(foreign, *plan.targets[1:]))
    with pytest.raises(ValueError, match="unique"):
        dataclasses.replace(plan, targets=(plan.targets[0], *plan.targets))
    unbound = dataclasses.replace(plan.targets[0], parents=plan.targets[0].parents[1:])
    with pytest.raises(ValueError, match="project|parent"):
        dataclasses.replace(plan, targets=(unbound, *plan.targets[1:]))


def test_planned_target_rejects_inconsistent_absence_or_presence(tmp_path: Path) -> None:
    target = _plan(tmp_path).targets[0]
    assert target.before is target.before_sha256 is target.before_file is None
    with pytest.raises(ValueError, match="absence"):
        dataclasses.replace(target, before_sha256="0" * 64)
    with pytest.raises(ValueError, match="before"):
        dataclasses.replace(target, before=b"old", before_sha256=hashlib.sha256(b"old").hexdigest())
    before_file = FileIdentity(1, 2, stat.S_IFREG | 0o600, 3, 4, 5)
    present = dataclasses.replace(
        target,
        before=b"old",
        before_sha256=hashlib.sha256(b"old").hexdigest(),
        before_file=before_file,
    )
    assert present.before_file.mode == stat.S_IFREG | 0o600
    assert present.mode == 0o644
