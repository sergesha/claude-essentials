"""Immutable values at the whole-DAG authoring boundary."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from lockstep.errors import AuthoringError
from lockstep.workflow.canonical import canonical_yaml
from lockstep.workflow.compiler import CompilationResult, compile_workflow_document
from lockstep.workflow.schema import load_workflow_bytes
from lockstep.workflow.semantics import ResolvedCatalog

__all__ = [
    "DestinationImage",
    "ProjectCompilationBundle",
    "SourceIdentity",
    "plan_project_compilation",
]


@dataclass(frozen=True, slots=True)
class AuthoredRecipe:
    name: str
    kind: Literal["workflow", "manual"]
    workflow_path: Path | None
    recipe_path: Path
    dependency_path: Path | None
    source_map_path: Path | None


def _absolute(path: Path, label: str) -> Path:
    if not isinstance(path, Path):
        raise TypeError(f"{label} must be a Path")
    value = path
    if not value.is_absolute() or any(part in {".", ".."} for part in value.parts):
        raise ValueError(f"{label} must be absolute and lexically canonical")
    return value


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_recipe_bytes_for_children(
    recipe_bytes: bytes, children: tuple[str, ...]
) -> bytes:
    """Return canonical compiled bytes with the supplied child ingress links."""

    if not children:
        return recipe_bytes
    document = yaml.safe_load(recipe_bytes)
    nodes = document.get("nodes") if isinstance(document, dict) else None
    if not isinstance(nodes, dict):
        raise AuthoringError("compiled template recipe has no node catalog")
    for index, child in enumerate(children):
        nodes[f"template-dependency-{index}"] = {
            "type": "subgraph",
            "graph": f"{child}.recipe.yaml",
            "mode": "invoke",
        }
    return canonical_yaml(document)


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    resolved_path: Path
    device: int
    inode: int

    def __post_init__(self) -> None:
        _absolute(self.resolved_path, "identity path")
        if any(type(value) is not int for value in (self.device, self.inode)):
            raise TypeError("path identity values must be integers")
        if min(self.device, self.inode) < 0:
            raise ValueError("path identity values must be non-negative")


@dataclass(frozen=True, slots=True)
class _LeafIdentity:
    resolved_path: Path
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int

    def __post_init__(self) -> None:
        _absolute(self.resolved_path, "leaf identity path")
        if any(
            type(value) is not int
            for value in (self.device, self.inode, self.mode, self.size, self.mtime_ns)
        ):
            raise TypeError("leaf identity values must be integers")
        if min(self.device, self.inode, self.mode, self.size) < 0:
            raise ValueError("leaf identity values must be non-negative")
        if not stat.S_ISREG(self.mode):
            raise ValueError("leaf identity must describe a regular file")


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    role: str
    resolved_path: Path
    content: bytes
    sha256: str
    leaf: _LeafIdentity
    ancestors: tuple[_PathIdentity, ...]

    def __post_init__(self) -> None:
        path = _absolute(self.resolved_path, "source path")
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("source role must be non-empty")
        if not isinstance(self.content, bytes) or self.sha256 != _digest(self.content):
            raise ValueError("source digest does not match its exact bytes")
        if not isinstance(self.leaf, _LeafIdentity):
            raise TypeError("source leaf identity is invalid")
        if self.leaf.resolved_path != path or self.leaf.size != len(self.content):
            raise ValueError("source leaf identity does not match its source")
        if not isinstance(self.ancestors, tuple):
            raise TypeError("source ancestor identities must be a tuple")
        if any(not isinstance(item, _PathIdentity) for item in self.ancestors):
            raise TypeError("source ancestor identity is invalid")
        paths = tuple(item.resolved_path for item in self.ancestors)
        if len(paths) != len(set(paths)) or path in paths:
            raise ValueError("source ancestor identities are invalid")


@dataclass(frozen=True, slots=True)
class DestinationImage:
    role: str
    resolved_path: Path
    content: bytes | None
    sha256: str | None
    mode: int | None
    leaf: _LeafIdentity | None
    ancestors: tuple[_PathIdentity, ...]

    def __post_init__(self) -> None:
        path = _absolute(self.resolved_path, "destination path")
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("destination role must be non-empty")
        if not isinstance(self.ancestors, tuple):
            raise TypeError("destination ancestor identities must be a tuple")
        if any(not isinstance(item, _PathIdentity) for item in self.ancestors):
            raise TypeError("destination ancestor identity is invalid")
        ancestor_paths = tuple(item.resolved_path for item in self.ancestors)
        if len(ancestor_paths) != len(set(ancestor_paths)) or path in ancestor_paths:
            raise ValueError("destination ancestor identities are invalid")
        if self.content is None:
            if self.sha256 is not None or self.mode is not None or self.leaf is not None:
                raise ValueError("an absent destination must have one exact absence image")
            return
        if not isinstance(self.content, bytes) or self.sha256 != _digest(self.content):
            raise ValueError("destination digest does not match its exact bytes")
        if type(self.mode) is not int or not 0 <= self.mode <= 0o7777:
            raise ValueError("destination mode is invalid")
        if self.leaf is not None:
            if not isinstance(self.leaf, _LeafIdentity):
                raise TypeError("destination leaf identity is invalid")
            if (
                self.leaf.resolved_path != path
                or self.leaf.size != len(self.content)
                or stat.S_IMODE(self.leaf.mode) != self.mode
            ):
                raise ValueError("destination leaf identity does not match its image")


@dataclass(frozen=True, slots=True)
class ProjectCompilationBundle:
    resolved_project: Path
    project_identity: _PathIdentity
    sources: tuple[SourceIdentity, ...]
    dependency_edges: tuple[tuple[str, tuple[str, ...]], ...]
    before_images: tuple[DestinationImage, ...]
    after_images: tuple[DestinationImage, ...]

    def __post_init__(self) -> None:
        project = _absolute(self.resolved_project, "project path")
        if not isinstance(self.project_identity, _PathIdentity):
            raise TypeError("project identity is invalid")
        if self.project_identity.resolved_path != project:
            raise ValueError("project identity does not match its resolved path")
        for label, value in (
            ("sources", self.sources),
            ("dependency edges", self.dependency_edges),
            ("before images", self.before_images),
            ("after images", self.after_images),
        ):
            if not isinstance(value, tuple):
                raise TypeError(f"bundle {label} must be a tuple")
        if any(not isinstance(item, SourceIdentity) for item in self.sources):
            raise TypeError("bundle source identity is invalid")
        roles = tuple(item.role for item in self.sources)
        if not roles or len(roles) != len(set(roles)):
            raise ValueError("bundle source roles must be non-empty and unique")
        source_paths = tuple(item.resolved_path for item in self.sources)
        if len(source_paths) != len(set(source_paths)):
            raise ValueError("bundle source paths must be unique")
        if any(
            not isinstance(edge, tuple) or len(edge) != 2
            for edge in self.dependency_edges
        ):
            raise TypeError("bundle dependency edge is invalid")
        if tuple(role for role, _children in self.dependency_edges) != roles:
            raise ValueError("bundle dependency edges must match child-first source roles")
        seen: set[str] = set()
        for role, children in self.dependency_edges:
            if (
                not isinstance(role, str)
                or not isinstance(children, tuple)
                or any(not isinstance(child, str) for child in children)
                or len(children) != len(set(children))
                or any(child not in seen for child in children)
            ):
                raise ValueError("bundle dependencies must reference earlier child roles")
            seen.add(role)
        if any(not isinstance(item, DestinationImage) for item in self.before_images):
            raise TypeError("bundle before-image is invalid")
        if any(not isinstance(item, DestinationImage) for item in self.after_images):
            raise TypeError("bundle after-image is invalid")
        before_paths = tuple(item.resolved_path for item in self.before_images)
        after_paths = tuple(item.resolved_path for item in self.after_images)
        if before_paths != after_paths or len(after_paths) != len(set(after_paths)):
            raise ValueError("bundle before and after destination maps must match exactly")
        if any(item.content is None for item in self.after_images):
            raise ValueError("bundle after-images must contain exact destination bytes")
        for before, after in zip(self.before_images, self.after_images, strict=True):
            if before.role != after.role or before.role not in roles:
                raise ValueError("bundle destination roles must match source roles")
            if before.ancestors != after.ancestors:
                raise ValueError("paired destination ancestors must match")
            if before.content is not None and before.leaf is None:
                raise ValueError("a present before-image requires its captured leaf identity")
            if after.leaf is not None:
                raise ValueError("a planned after-image cannot contain a captured leaf identity")


def plan_project_compilation(recipe: AuthoredRecipe) -> ProjectCompilationBundle:
    """Plan one immutable authored closure without publishing it."""

    if recipe.kind != "workflow" or recipe.workflow_path is None:
        raise AuthoringError("only ordinary workflow sources can be planned")
    project, source_path = _workflow_project_and_source(recipe)
    directory_identities: dict[Path, _PathIdentity] = {}
    project_identity = _cached_directory_identity(directory_identities, project)
    source = _capture_source(recipe.name, source_path, project, directory_identities)
    document = load_workflow_bytes(source.resolved_path, source.content)
    _validated, compiled = compile_workflow_document(document, ResolvedCatalog())
    if compiled.generated_files:
        raise AuthoringError("generated files require direct-child compilation planning")
    destinations = _leaf_destinations(recipe, compiled)
    ancestors_by_parent = {
        parent: _destination_ancestors(project, parent, directory_identities)
        for parent in dict.fromkeys(path.parent for path in destinations)
    }
    before_images = tuple(
        _absent_destination(recipe.name, path, ancestors_by_parent[path.parent])
        for path in destinations
    )
    after_images = tuple(
        DestinationImage(
            recipe.name,
            path,
            content,
            _digest(content),
            0o644,
            None,
            ancestors_by_parent[path.parent],
        )
        for path, content in destinations.items()
    )
    return ProjectCompilationBundle(
        project,
        project_identity,
        (source,),
        ((recipe.name, ()),),
        before_images,
        after_images,
    )


def _workflow_project_and_source(recipe: AuthoredRecipe) -> tuple[Path, Path]:
    workflow_path = recipe.workflow_path
    if workflow_path is None:
        raise AuthoringError("workflow source is required")
    source_path = workflow_path.resolve()
    project = source_path.parent.parent.parent
    expected = project / ".lockstep" / "workflows" / f"{recipe.name}.workflow.yaml"
    if source_path != expected:
        raise AuthoringError("workflow source is outside the canonical project layout")
    expected_destinations = (
        project / ".lockstep" / "recipes" / f"{recipe.name}.recipe.yaml",
        project / ".lockstep" / "recipes" / f"{recipe.name}.dependencies.json",
        project / ".lockstep" / "recipes" / f"{recipe.name}.source-map.json",
    )
    if (recipe.recipe_path, recipe.dependency_path, recipe.source_map_path) != expected_destinations:
        raise AuthoringError("workflow destinations are outside the canonical project layout")
    return project, source_path


def _capture_source(
    role: str,
    path: Path,
    project: Path,
    directory_identities: dict[Path, _PathIdentity],
) -> SourceIdentity:
    first = path.lstat()
    if not stat.S_ISREG(first.st_mode):
        raise AuthoringError("workflow source must be a regular file")
    content = path.read_bytes()
    last = path.lstat()
    observed = (last.st_dev, last.st_ino, last.st_mode, last.st_size, last.st_mtime_ns)
    expected = (first.st_dev, first.st_ino, first.st_mode, first.st_size, first.st_mtime_ns)
    if observed != expected or last.st_size != len(content):
        raise AuthoringError("workflow source changed while it was captured")
    return SourceIdentity(
        role,
        path,
        content,
        _digest(content),
        _leaf_identity(path, last),
        tuple(
            _cached_directory_identity(directory_identities, ancestor)
            for ancestor in (project, project / ".lockstep", path.parent)
        ),
    )


def _leaf_destinations(
    recipe: AuthoredRecipe, compiled: CompilationResult
) -> dict[Path, bytes]:
    dependency_path = recipe.dependency_path
    source_map_path = recipe.source_map_path
    if dependency_path is None or source_map_path is None:
        raise AuthoringError("workflow destinations are incomplete")
    return {
        recipe.recipe_path: canonical_recipe_bytes_for_children(compiled.recipe_bytes, ()),
        dependency_path: compiled.dependency_manifest_bytes,
        source_map_path: compiled.source_map_bytes,
    }


def _absent_destination(
    role: str, path: Path, ancestors: tuple[_PathIdentity, ...]
) -> DestinationImage:
    try:
        path.lstat()
    except FileNotFoundError:
        return DestinationImage(role, path, None, None, None, None, ancestors)
    raise AuthoringError("existing compilation destinations are not yet supported")


def _destination_ancestors(
    project: Path,
    parent: Path,
    directory_identities: dict[Path, _PathIdentity],
) -> tuple[_PathIdentity, ...]:
    try:
        relative_parent = parent.relative_to(project)
    except ValueError as exc:
        raise AuthoringError("workflow destination is outside the project") from exc
    ancestors = [_cached_directory_identity(directory_identities, project)]
    current = project
    for part in relative_parent.parts:
        current /= part
        try:
            ancestors.append(_cached_directory_identity(directory_identities, current))
        except FileNotFoundError:
            break
    return tuple(ancestors)


def _directory_identity(path: Path) -> _PathIdentity:
    try:
        first = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise AuthoringError("destination ancestor cannot be captured") from exc
    if not stat.S_ISDIR(first.st_mode):
        raise AuthoringError("destination ancestor must be a canonical real directory")
    try:
        resolved = path.resolve(strict=True)
        last = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise AuthoringError("destination ancestor cannot be captured") from exc
    if (first.st_dev, first.st_ino, first.st_mode) != (
        last.st_dev,
        last.st_ino,
        last.st_mode,
    ):
        raise AuthoringError("destination ancestor changed while it was captured")
    if resolved != path:
        raise AuthoringError("destination ancestor must be a canonical real directory")
    return _PathIdentity(path, first.st_dev, first.st_ino)


def _cached_directory_identity(
    directory_identities: dict[Path, _PathIdentity], path: Path
) -> _PathIdentity:
    canonical = _absolute(path, "directory path")
    identity = directory_identities.get(canonical)
    if identity is None:
        identity = _directory_identity(canonical)
        directory_identities[canonical] = identity
    return identity


def _leaf_identity(path: Path, info: os.stat_result) -> _LeafIdentity:
    return _LeafIdentity(
        path,
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
    )
