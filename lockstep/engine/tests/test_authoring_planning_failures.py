"""All planning controls reject before project mutation."""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from lockstep.authoring import project_paths
from lockstep.errors import AuthoringError
from lockstep.workflow.compiler import GeneratedFile, _create_compiler_provenance, canonical_execution_bytes, generated_bundle_sha256
from tests._authoring_gate import compile_closure, tree_image, write_workflow


def _reject(project, root, state, monkeypatch, capsys, detail=None):
    from lockstep import cli
    before = tree_image(project); monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state)); monkeypatch.chdir(project)
    assert cli.main(["recipe", "compile", root]) == 2; error = capsys.readouterr().err
    assert tree_image(project) == before and (not state.exists() or not tuple(state.rglob("transaction.json")))
    assert not tuple(project.rglob(".lockstep-authoring-*.tmp"))
    if detail: assert detail in error


def _star(project, count):
    children = tuple(f"child-{index}" for index in range(count - 1))
    for name in children: write_workflow(project, name)
    write_workflow(project, "root", children=children); return "root"


def _chain(project, count):
    names = tuple(f"node-{index:03}" for index in range(count))
    for index in range(count - 1, -1, -1): write_workflow(project, names[index], children=() if index == count - 1 else (names[index + 1],))
    return names[0]


@pytest.mark.parametrize("kind", ("source-symlink", "source-fifo", "destination-symlink", "destination-directory"))
def test_public_compile_rejects_nonregular_or_linked_input_write_free(tmp_path, monkeypatch, capsys, kind) -> None:
    project = tmp_path / "project"; child = write_workflow(project, "child"); write_workflow(project, "parent", children=("child",)); compile_closure(project, "child", "parent")
    if kind == "source-symlink":
        outside = tmp_path / "outside.workflow.yaml"; outside.write_bytes(child.read_bytes()); child.unlink(); child.symlink_to(outside); detail = "workflow source"
    elif kind == "source-fifo": child.unlink(); os.mkfifo(child); detail = "workflow source"
    else:
        target = project_paths(project, "child").recipe_path; target.unlink()
        if kind == "destination-symlink": outside = tmp_path / "outside.recipe.yaml"; outside.write_text("outside\n"); target.symlink_to(outside)
        else: target.mkdir()
        detail = "compilation destination"
    _reject(project, "parent", tmp_path / "state", monkeypatch, capsys, detail)


@pytest.mark.parametrize("failure", ("parse", "semantic", "missing", "cycle"))
def test_public_compile_parse_semantic_and_graph_failures_are_write_free(tmp_path, monkeypatch, capsys, failure) -> None:
    project = tmp_path / "project"; child = write_workflow(project, "child"); parent = write_workflow(project, "parent", children=("child",)); compile_closure(project, "child", "parent")
    if failure == "parse": child.write_text("not: [valid")
    elif failure == "semantic": child.write_text(child.read_text().replace("protect: ['**']", "protect: ['src/**']"))
    elif failure == "missing": child.rename(child.with_suffix(".missing"))
    else: parent.write_text(parent.read_text().replace("workflow: child", "workflow: parent"))
    _reject(project, "parent", tmp_path / "state", monkeypatch, capsys)


def test_public_compile_rejects_excessive_yaml_depth_before_project_mutation(
    tmp_path, monkeypatch, capsys
) -> None:
    project = tmp_path / "project"
    source = write_workflow(project, "leaf")
    source.write_bytes(source.read_bytes() + b"x-depth: " + b"[" * 65 + b"0" + b"]" * 65 + b"\n")

    _reject(
        project,
        "leaf",
        tmp_path / "state",
        monkeypatch,
        capsys,
        "LSW111",
    )


@pytest.mark.parametrize(("kind", "detail"), (("reads", "read set exceeds 256"), ("writes", "after images exceeds 256")))
def test_public_compile_rejects_257th_record_before_project_mutation(tmp_path, monkeypatch, capsys, kind, detail) -> None:
    project = tmp_path / "project"; root = _chain(project, 257) if kind == "reads" else _star(project, 86)
    _reject(project, root, tmp_path / "state", monkeypatch, capsys, detail)


@pytest.mark.parametrize("group", ("read", "before"))
def test_public_compile_rejects_independent_four_mib_capture_bounds(tmp_path, monkeypatch, capsys, group) -> None:
    project = tmp_path / "project"; root = _star(project, 5 if group == "read" else 2)
    if group == "read":
        files = tuple((project / ".lockstep/workflows").glob("*.yaml"))
        for path in files: path.write_text(path.read_text() + "#" + "r" * 839_000 + "\n")
    else:
        files = []
        for name in ("root", "child-0"):
            recipe = project_paths(project, name)
            for path in (recipe.recipe_path, recipe.dependency_path, recipe.source_map_path):
                assert path is not None; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"b" * 700_000); files.append(path)
    assert all(path.stat().st_size < 1_048_576 for path in files) and sum(path.stat().st_size for path in files) > 4_194_304
    _reject(project, root, tmp_path / "state", monkeypatch, capsys)


def test_public_compile_lowering_failure_after_semantics_is_write_free(tmp_path, monkeypatch, capsys) -> None:
    import lockstep.authoring_compilation as compilation
    project = tmp_path / "project"; write_workflow(project, "leaf"); original = compilation.compile_workflow_document; reached = []
    def fail(document, catalog): original(document, catalog); reached.append(True); raise AuthoringError("post-semantics lowering failure")
    monkeypatch.setattr(compilation, "compile_workflow_document", fail)
    _reject(project, "leaf", tmp_path / "state", monkeypatch, capsys, "post-semantics lowering failure"); assert reached == [True]


def _generated(compiled, files):
    outputs = (*compiled.generated_files, *files); digest = generated_bundle_sha256(compiled.root_relative_path, compiled.recipe_bytes, outputs)
    provenance = _create_compiler_provenance(compiled.recipe_bytes, context="compiler-output", root_relative_path=compiled.root_relative_path,
        generated_files={item.relative_path: item.content for item in outputs}, execution_recipe_bytes=canonical_execution_bytes(compiled.recipe_bytes, logical_path=compiled.root_relative_path),
        execution_generated_files={item.relative_path: canonical_execution_bytes(item.content, logical_path=item.relative_path) for item in outputs}, source_bundle_sha256=digest)
    return replace(compiled, generated_files=outputs, bundle_sha256=digest, compiler_provenance=provenance)


def test_public_compile_rejects_cross_role_generated_collision_write_free(tmp_path, monkeypatch, capsys) -> None:
    import lockstep.authoring_compilation as compilation
    project = tmp_path / "project"; write_workflow(project, "child"); write_workflow(project, "parent", children=("child",)); original = compilation.compile_captured_source; injected = []
    def compile(document, *, children=None):
        validated, catalog, result = original(document, children=children)
        if validated.workflow.name == "parent": result = _generated(result, (GeneratedFile.build("child.recipe.yaml", result.recipe_bytes),)); injected.append(True)
        return validated, catalog, result
    monkeypatch.setattr(compilation, "compile_captured_source", compile)
    _reject(project, "parent", tmp_path / "state", monkeypatch, capsys, "destinations must be unique"); assert injected == [True]


def test_public_compile_rejects_amplified_real_generated_outputs(tmp_path, monkeypatch, capsys) -> None:
    import lockstep.authoring_compilation as compilation
    project = tmp_path / "project"; write_workflow(project, "leaf"); original = compilation.compile_captured_source; injected = []
    def compile(document, *, children=None):
        validated, catalog, result = original(document, children=children); payload = result.recipe_bytes + b"#" + b"a" * 850_000
        files = tuple(GeneratedFile.build(f"generated-{index}.recipe.yaml", payload) for index in range(5)); injected.append(True)
        return validated, catalog, _generated(result, files)
    monkeypatch.setattr(compilation, "compile_captured_source", compile)
    _reject(project, "leaf", tmp_path / "state", monkeypatch, capsys); assert injected == [True]


def test_public_compile_rejects_destination_ancestor_swap_write_free(tmp_path, monkeypatch, capsys) -> None:
    from lockstep import cli
    project = tmp_path / "project"; write_workflow(project, "leaf"); compile_closure(project, "leaf"); recipes = project / ".lockstep/recipes"; original = Path.resolve; after = []
    def swap(path, *args, **kwargs):
        if path == recipes and not after: recipes.rename(recipes.with_name("old")); recipes.mkdir(); after.append(tree_image(project))
        return original(path, *args, **kwargs)
    state = tmp_path / "state"; monkeypatch.setattr(Path, "resolve", swap); monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state)); monkeypatch.chdir(project)
    assert cli.main(["recipe", "compile", "leaf"]) == 2
    assert "destination ancestor changed" in capsys.readouterr().err
    assert not state.exists() or not tuple(state.rglob("transaction.json"))
    assert tree_image(project) == after[0]
