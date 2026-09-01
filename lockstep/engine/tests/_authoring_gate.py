"""Small black-box helpers shared by the focused authoring gate."""
from __future__ import annotations

import hashlib, os, stat
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from lockstep import cli
from lockstep.mcp import server
from lockstep.recipe.authority import RecipeAuthorityPolicy, StrictRecipeIngress
from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
from lockstep.runtime.effects.owner_provisioning import provision_runtime_snapshot
from tests.runtime._runtime_commitment_harness import _runtime_config


@dataclass(frozen=True)
class TreeEntry:
    kind: str; mode: int; device: int; inode: int; size: int; mtime_ns: int; ctime_ns: int
    content: bytes | None = None; symlink_target: str | None = None


def _entry(path: Path) -> TreeEntry:
    info = path.lstat(); facts = (stat.S_IMODE(info.st_mode), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if stat.S_ISREG(info.st_mode): return TreeEntry("regular", *facts, content=path.read_bytes())
    if stat.S_ISDIR(info.st_mode): return TreeEntry("directory", *facts)
    if stat.S_ISLNK(info.st_mode): return TreeEntry("symlink", *facts, symlink_target=os.readlink(path))
    return TreeEntry("non-regular", *facts)


def tree_image(root: Path) -> dict[str, TreeEntry]:
    return {} if not root.exists() else {".": _entry(root), **{p.relative_to(root).as_posix(): _entry(p) for p in sorted(root.rglob("*"))}}


def assert_source_identity(source, project: Path, source_path: Path) -> None:
    info = source_path.lstat(); assert source.path == source_path.resolve()
    assert source.content == source_path.read_bytes(); assert source.sha256 == hashlib.sha256(source.content).hexdigest()
    assert (source.file.device, source.file.inode, source.file.mode, source.file.size, source.file.mtime_ns) == (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
    paths = (project.resolve(), (project / ".lockstep").resolve(), source_path.parent.resolve())
    assert tuple(item.path for item in source.parents) == paths


def write_workflow(project: Path, name: str, *, children: tuple[str, ...] = (), marker: str = "initial") -> Path:
    path = project / ".lockstep/workflows" / f"{name}.workflow.yaml"; path.parent.mkdir(parents=True, exist_ok=True)
    flow = "".join(f"  - call:\n      workflow: {child}\n      runner: codex\n" for child in children) or "  - escalate: {}\n"
    path.write_text(f"workflow_version: '1'\nname: {name}\ndescription: {marker}\nprotect: ['**']\nflow:\n{flow}")
    return path


def mcp_context(project: Path) -> SimpleNamespace:
    return SimpleNamespace(request_context=SimpleNamespace(meta={"x-codex-turn-metadata": {"workspaces": {str(project): {}}}}))


def public_compile(adapter: str, project: Path, name: str, monkeypatch) -> object:
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(project.parent / "owner-state"))
    if adapter == "cli": monkeypatch.chdir(project); return cli.main(["recipe", "compile", name])
    if adapter == "mcp": return server.recipe_compile(name, ctx=mcp_context(project))
    raise ValueError(adapter)


def replace_marker(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text().replace(old, new))


def compile_closure(project: Path, *names: str) -> None:
    from lockstep.authoring import project_paths, write_compilation
    state = (project.parent / f"{project.name}-owner-state").resolve()
    for name in names: write_compilation(project_paths(project, name), state_dir=state)


def expected_compilation_image(project: Path, names: tuple[str, ...]) -> dict[Path, bytes]:
    from lockstep.authoring import project_paths
    from lockstep.authoring_compilation import plan_project_compilation
    result: dict[Path, bytes] = {}
    for name in names:
        for target in plan_project_compilation(project_paths(project, name)).targets:
            assert result.setdefault(target.path, target.after) == target.after
    return result


def observed_compilation_image(expected: dict[Path, bytes]) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in expected}


def assert_no_durable_runtime_change(before: dict[str, TreeEntry], state: Path) -> None:
    assert tree_image(state) == before


def provision_controlled_runtime(
    project: Path,
    state: Path,
    recipe: str,
) -> RuntimeRequirementIndex:
    recipes = project / ".lockstep" / "recipes"
    authorized = StrictRecipeIngress(recipes).inspect(
        f"{recipe}.recipe.yaml"
    ).authorize(RecipeAuthorityPolicy())
    index = RuntimeRequirementIndex.for_authorized_closure(
        authorized,
        project_identity=str(project.resolve()),
    )
    assert index.requirements
    runtime_root = state.parent / f"{state.name}-controlled-runtime"
    runtime_root.mkdir()
    config = _runtime_config(runtime_root)
    codex = config["codex"]
    pinned = config["pinned"]
    assert isinstance(codex, dict)
    assert isinstance(pinned, dict)
    provision_runtime_snapshot(
        state_dir=state,
        codex=codex,
        pinned=pinned,
        replacement_keys=tuple(
            requirement.grant_selection_key for requirement in index.requirements
        ),
        index=index,
        project=project,
    )
    return index
