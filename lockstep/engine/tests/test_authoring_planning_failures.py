"""Public authoring planning failures reject before trusted publication."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from lockstep.authoring import project_paths
from lockstep.errors import AuthoringError
from lockstep.workflow.compiler import (
    GeneratedFile,
    _create_compiler_provenance,
    canonical_execution_bytes,
    generated_bundle_sha256,
)
from tests._authoring_gate import compile_closure, tree_image, write_workflow


def _assert_no_journal(state: Path) -> None:
    assert not state.exists() or not tuple(state.rglob("transaction.json"))


def _public_rejection(
    project: Path,
    root: str,
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    detail: str | None = None,
) -> str:
    from lockstep import cli

    before = tree_image(project)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state))
    monkeypatch.chdir(project)
    assert cli.main(["recipe", "compile", root]) == 2
    stderr = capsys.readouterr().err
    assert tree_image(project) == before
    _assert_no_journal(state)
    if detail is not None:
        assert detail in stderr
    return stderr


def _star(project: Path, count: int, *, marker: str = "initial") -> str:
    children = tuple(f"child-{index}" for index in range(count - 1))
    for name in children:
        write_workflow(project, name, marker=marker)
    write_workflow(project, "root", children=children, marker=marker)
    return "root"


def _deep_chain(project: Path, count: int) -> str:
    names = tuple(f"node-{index:03}" for index in range(count))
    for index in range(count - 1, -1, -1):
        children = () if index == count - 1 else (names[index + 1],)
        write_workflow(project, names[index], children=children)
    return names[0]


@pytest.mark.parametrize(
    "kind",
    (
        "source-symlink",
        "source-fifo",
        "destination-symlink",
        "destination-directory",
    ),
)
def test_public_compile_rejects_non_regular_or_linked_planning_input_write_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    project = tmp_path / "project"
    child = write_workflow(project, "child")
    write_workflow(project, "parent", children=("child",))
    compile_closure(project, "child", "parent")
    if kind == "source-symlink":
        outside = tmp_path / "outside.workflow.yaml"
        outside.write_bytes(child.read_bytes())
        child.unlink()
        child.symlink_to(outside)
        detail = "workflow source"
    elif kind == "source-fifo":
        child.unlink()
        os.mkfifo(child)
        detail = "workflow source"
    else:
        target = project_paths(project, "child").recipe_path
        target.unlink()
        if kind == "destination-symlink":
            outside = tmp_path / "outside.recipe.yaml"
            outside.write_text("outside\n", encoding="utf-8")
            target.symlink_to(outside)
        else:
            target.mkdir()
        detail = "compilation destination"
    _public_rejection(
        project,
        "parent",
        tmp_path / "state",
        monkeypatch,
        capsys,
        detail,
    )


@pytest.mark.parametrize("failure", ("parse", "semantic", "missing", "cycle"))
def test_public_compile_planning_controls_are_write_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    project = tmp_path / "project"
    child = write_workflow(project, "child")
    parent = write_workflow(project, "parent", children=("child",))
    compile_closure(project, "child", "parent")
    if failure == "parse":
        child.write_text("not: [valid", encoding="utf-8")
    elif failure == "semantic":
        child.write_text(
            child.read_text(encoding="utf-8").replace(
                "protect: ['**']", "protect: ['src/**']"
            ),
            encoding="utf-8",
        )
    elif failure == "missing":
        child.rename(child.with_suffix(".missing"))
    else:
        parent.write_text(
            parent.read_text(encoding="utf-8").replace(
                "workflow: child", "workflow: parent"
            ),
            encoding="utf-8",
        )
    _public_rejection(project, "parent", tmp_path / "state", monkeypatch, capsys)


def test_public_compile_rejects_257th_read_record_before_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _public_rejection(
        project,
        _deep_chain(project, 257),
        tmp_path / "state",
        monkeypatch,
        capsys,
        "authoring read set exceeds 256 admission limit",
    )


def test_public_compile_rejects_257th_paired_write_set_record_before_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _public_rejection(
        project,
        _star(project, 86),
        tmp_path / "state",
        monkeypatch,
        capsys,
        "authoring after images exceeds 256 admission limit",
    )


def test_public_compile_rejects_read_set_bytes_before_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    root = _star(project, 5)
    sources = tuple((project / ".lockstep" / "workflows").glob("*.yaml"))
    for source in sources:
        source.write_text(
            source.read_text(encoding="utf-8") + "#" + "r" * 839_000 + "\n",
            encoding="utf-8",
        )
    sizes = tuple(source.stat().st_size for source in sources)
    assert all(size < 1_048_576 for size in sizes)
    assert sum(sizes) > 4_194_304
    _public_rejection(project, root, tmp_path / "state", monkeypatch, capsys)


def test_public_compile_rejects_before_image_bytes_before_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    root = _star(project, 2)
    destinations: list[Path] = []
    for name in ("root", "child-0"):
        recipe = project_paths(project, name)
        for path in (
            recipe.recipe_path,
            recipe.dependency_path,
            recipe.source_map_path,
        ):
            assert path is not None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"b" * 700_000)
            destinations.append(path)
    sizes = tuple(path.stat().st_size for path in destinations)
    assert all(size < 1_048_576 for size in sizes)
    assert sum(sizes) > 4_194_304
    _public_rejection(project, root, tmp_path / "state", monkeypatch, capsys)


def test_public_compile_lowering_failure_after_valid_parse_and_semantics_is_write_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import lockstep.authoring_compilation as compilation

    project = tmp_path / "project"
    write_workflow(project, "leaf")
    original = compilation.compile_workflow_document
    reached = False

    def fail(document: object, catalog: object) -> object:
        nonlocal reached
        original(document, catalog)
        reached = True
        raise AuthoringError("post-semantics lowering failure")

    monkeypatch.setattr(compilation, "compile_workflow_document", fail)
    _public_rejection(
        project,
        "leaf",
        tmp_path / "state",
        monkeypatch,
        capsys,
        "post-semantics lowering failure",
    )
    assert reached


def _compiled_with_generated(
    compiled: object,
    generated: tuple[GeneratedFile, ...],
) -> object:
    executable = tuple((*compiled.generated_files, *generated))
    bundle_sha256 = generated_bundle_sha256(
        compiled.root_relative_path,
        compiled.recipe_bytes,
        executable,
    )
    provenance = _create_compiler_provenance(
        compiled.recipe_bytes,
        context="compiler-output",
        root_relative_path=compiled.root_relative_path,
        generated_files={item.relative_path: item.content for item in executable},
        execution_recipe_bytes=canonical_execution_bytes(
            compiled.recipe_bytes,
            logical_path=compiled.root_relative_path,
        ),
        execution_generated_files={
            item.relative_path: canonical_execution_bytes(
                item.content,
                logical_path=item.relative_path,
            )
            for item in executable
        },
        source_bundle_sha256=bundle_sha256,
    )
    return replace(
        compiled,
        generated_files=executable,
        bundle_sha256=bundle_sha256,
        compiler_provenance=provenance,
    )


def test_public_compile_rejects_cross_role_generated_file_collision_write_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import lockstep.authoring_bundle as bundle_module

    project = tmp_path / "project"
    write_workflow(project, "child")
    write_workflow(project, "parent", children=("child",))
    original = bundle_module.compile_captured_source
    injected = False

    def compile_with_collision(document: object, *, children: object = None) -> object:
        nonlocal injected
        validated, catalog, compiled = original(document, children=children)
        if validated.workflow.name == "parent":
            collision = GeneratedFile.build("child.recipe.yaml", compiled.recipe_bytes)
            compiled = _compiled_with_generated(compiled, (collision,))
            injected = True
        return validated, catalog, compiled

    monkeypatch.setattr(
        bundle_module,
        "compile_captured_source",
        compile_with_collision,
    )
    _public_rejection(
        project,
        "parent",
        tmp_path / "state",
        monkeypatch,
        capsys,
        "compilation destinations must be unique",
    )
    assert injected


def test_public_compile_rejects_amplified_real_generated_outputs_before_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import lockstep.authoring_bundle as bundle_module

    project = tmp_path / "project"
    write_workflow(project, "leaf")
    original = bundle_module.compile_captured_source
    injected = False

    def compile_with_generated_outputs(
        document: object,
        *,
        children: object = None,
    ) -> object:
        nonlocal injected
        validated, catalog, compiled = original(document, children=children)
        payload = compiled.recipe_bytes + b"#" + b"a" * 850_000 + b"\n"
        generated = tuple(
            GeneratedFile.build(f"generated-{index}.recipe.yaml", payload)
            for index in range(5)
        )
        assert all(len(item.content) < 1_048_576 for item in generated)
        assert sum(len(item.content) for item in generated) > 4_194_304
        compiled = _compiled_with_generated(compiled, generated)
        injected = True
        return validated, catalog, compiled

    monkeypatch.setattr(
        bundle_module,
        "compile_captured_source",
        compile_with_generated_outputs,
    )
    _public_rejection(project, "leaf", tmp_path / "state", monkeypatch, capsys)
    assert injected


def test_public_compile_rejects_destination_ancestor_swap_write_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from lockstep import cli

    project = tmp_path / "project"
    write_workflow(project, "leaf")
    compile_closure(project, "leaf")
    recipes = project / ".lockstep" / "recipes"
    original = Path.resolve
    after_swap: dict[str, object] | None = None

    def swap(path: Path, *args: object, **kwargs: object) -> Path:
        nonlocal after_swap
        if path == recipes and after_swap is None:
            recipes.rename(recipes.with_name("recipes-old"))
            recipes.mkdir()
            after_swap = tree_image(project)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", swap)
    state = tmp_path / "state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state))
    monkeypatch.chdir(project)
    assert cli.main(["recipe", "compile", "leaf"]) == 2
    stderr = capsys.readouterr().err
    assert after_swap is not None
    assert tree_image(project) == after_swap
    _assert_no_journal(state)
    assert "destination ancestor changed" in stderr
