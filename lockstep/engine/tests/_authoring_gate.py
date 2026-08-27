"""Small black-box helpers for the Task 12A authoring transaction gate."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from lockstep import cli
from lockstep.mcp import server


@dataclass(frozen=True)
class TreeEntry:
    kind: str
    mode: int
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    content: bytes | None = None
    symlink_target: str | None = None


def _tree_entry(path: Path) -> TreeEntry:
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    identity = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }
    if stat.S_ISREG(info.st_mode):
        return TreeEntry("regular", mode, **identity, content=path.read_bytes())
    if stat.S_ISDIR(info.st_mode):
        return TreeEntry("directory", mode, **identity)
    if stat.S_ISLNK(info.st_mode):
        return TreeEntry("symlink", mode, **identity, symlink_target=os.readlink(path))
    return TreeEntry("non-regular", mode, **identity)


def tree_image(root: Path) -> dict[str, TreeEntry]:
    if not root.exists():
        return {}
    return {".": _tree_entry(root), **{
        path.relative_to(root).as_posix(): _tree_entry(path)
        for path in sorted(root.rglob("*"))
    }}


def write_workflow(
    project: Path,
    name: str,
    *,
    children: tuple[str, ...] = (),
    marker: str = "initial",
) -> Path:
    root = project / ".lockstep" / "workflows"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.workflow.yaml"
    flow = "".join(
        "  - call:\n"
        f"      workflow: {child}\n"
        "      runner: codex\n"
        for child in children
    )
    if not flow:
        flow = "  - escalate: {}\n"
    path.write_text(
        "workflow_version: '1'\n"
        f"name: {name}\n"
        f"description: {marker}\n"
        "protect: ['**']\n"
        "flow:\n"
        f"{flow}",
        encoding="utf-8",
    )
    return path


def mcp_context(project: Path) -> SimpleNamespace:
    return SimpleNamespace(
        request_context=SimpleNamespace(
            meta={"x-codex-turn-metadata": {"workspaces": {str(project): {}}}}
        )
    )


def public_compile(
    adapter: str,
    project: Path,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> object:
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(project.parent / "owner-state"))
    if adapter == "cli":
        monkeypatch.chdir(project)
        return cli.main(["recipe", "compile", name])
    if adapter == "mcp":
        return server.recipe_compile(name, ctx=mcp_context(project))
    raise ValueError(f"unknown public authoring adapter: {adapter}")


def replace_marker(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def compile_closure(project: Path, *names: str) -> None:
    from lockstep.authoring import project_paths, write_compilation

    for name in names:
        write_compilation(project_paths(project, name))


def expected_compilation_image(
    project: Path, names: tuple[str, ...]
) -> dict[Path, bytes]:
    from lockstep.authoring import (
        canonical_recipe_bytes,
        compile_project_source,
        project_paths,
    )

    expected: dict[Path, bytes] = {}
    for name in names:
        recipe = project_paths(project, name)
        if recipe.workflow_path is None:
            raise ValueError(f"workflow source is required for {name}")
        _validated, _catalog, compiled = compile_project_source(recipe.workflow_path)
        role = {
            recipe.recipe_path: canonical_recipe_bytes(recipe.workflow_path, compiled),
            recipe.dependency_path: compiled.dependency_manifest_bytes,
            recipe.source_map_path: compiled.source_map_bytes,
            **{
                recipe.recipe_path.parent / item.relative_path: item.content
                for item in compiled.generated_files
            },
        }
        for path, content in role.items():
            if path is None:
                raise ValueError(f"compilation destination is missing for {name}")
            previous = expected.setdefault(path, content)
            if previous != content:
                raise ValueError(f"conflicting expected compilation output: {path}")
    return expected


def observed_compilation_image(expected: dict[Path, bytes]) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in expected}
