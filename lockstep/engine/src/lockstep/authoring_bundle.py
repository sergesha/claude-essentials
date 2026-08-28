"""Immutable values at the whole-DAG authoring boundary."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from os import stat_result
from pathlib import Path
from typing import Literal

import yaml

from lockstep.authoring_capture import (
    capture_directory,
    capture_optional_regular_file,
    capture_regular_file,
    validate_directory,
)
from lockstep.authoring_compilation import (
    compile_captured_source,
    validate_logical_name,
    workflow_call_names,
)
from lockstep.authoring_limits import AuthoringBudget
from lockstep.errors import AuthoringError
from lockstep.workflow.canonical import canonical_yaml
from lockstep.workflow.compiler import CompilationResult
from lockstep.workflow.schema import load_workflow_bytes
from lockstep.workflow.semantics import ValidatedWorkflow

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


_CompiledWorkflow = tuple[ValidatedWorkflow, CompilationResult]
_ProjectedRole = tuple[str, dict[Path, bytes]]


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
    ctime_ns: int

    def __post_init__(self) -> None:
        _absolute(self.resolved_path, "leaf identity path")
        if any(
            type(value) is not int
            for value in (
                self.device,
                self.inode,
                self.mode,
                self.size,
                self.mtime_ns,
                self.ctime_ns,
            )
        ):
            raise TypeError("leaf identity values must be integers")
        if min(self.device, self.inode, self.mode, self.size) < 0:
            raise ValueError("leaf identity values must be non-negative")
        if not stat.S_ISREG(self.mode):
            raise ValueError("leaf identity must describe a regular file")


# Narrow typed contracts shared by the authoring planner and publisher.  They
# remain outside the public ``__all__`` surface.
PathIdentity = _PathIdentity
LeafIdentity = _LeafIdentity


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
    sources, dependency_edges, compiled_roles = _compile_closure(
        recipe, source_path, project, directory_identities
    )
    before_images, after_images = _destination_images(
        project, compiled_roles, directory_identities
    )
    return ProjectCompilationBundle(
        project,
        project_identity,
        sources,
        dependency_edges,
        before_images,
        after_images,
    )


def _compile_closure(
    recipe: AuthoredRecipe,
    source_path: Path,
    project: Path,
    directory_identities: dict[Path, _PathIdentity],
) -> tuple[
    tuple[SourceIdentity, ...],
    tuple[tuple[str, tuple[str, ...]], ...],
    tuple[_ProjectedRole, ...],
]:
    sources: list[SourceIdentity] = []
    dependency_edges: list[tuple[str, tuple[str, ...]]] = []
    projected_roles: list[_ProjectedRole] = []
    completed: dict[str, _CompiledWorkflow] = {}
    active: set[str] = set()
    source_budget = AuthoringBudget("authoring read set")
    destination_budget = AuthoringBudget("authoring after images")
    destination_paths: set[Path] = set()

    def visit(role_recipe: AuthoredRecipe, role_path: Path) -> None:
        role = role_recipe.name
        if role in completed:
            return
        if role in active:
            raise AuthoringError("workflow source dependency graph is recursive")
        active.add(role)
        try:
            source = _capture_source(
                role,
                role_path,
                project,
                directory_identities,
                max_bytes=source_budget.max_bytes_for_next,
            )
            source_budget.retain(source.content)
            document = load_workflow_bytes(source.resolved_path, source.content)
            child_names = workflow_call_names(document)
            for child_name in child_names:
                child_recipe = _workflow_recipe(project, child_name)
                child_path = child_recipe.workflow_path
                if child_path is None:
                    raise AuthoringError("workflow source is required")
                visit(child_recipe, child_path)
            children = {name: completed[name] for name in child_names}
            validated, _catalog, compiled = compile_captured_source(
                document, children=children
            )
            projected = _workflow_destinations(role_recipe, compiled, child_names)
            if any(path in destination_paths for path in projected):
                raise AuthoringError("compilation destinations must be unique")
            for content in projected.values():
                destination_budget.retain(content)
            destination_paths.update(projected)
            completed[role] = (validated, compiled)
            sources.append(source)
            dependency_edges.append((role, child_names))
            projected_roles.append((role, projected))
        finally:
            active.remove(role)

    visit(recipe, source_path)
    return tuple(sources), tuple(dependency_edges), tuple(projected_roles)


def _destination_images(
    project: Path,
    projected_roles: tuple[_ProjectedRole, ...],
    directory_identities: dict[Path, _PathIdentity],
) -> tuple[tuple[DestinationImage, ...], tuple[DestinationImage, ...]]:
    destinations: dict[Path, tuple[str, bytes]] = {}
    for role, projected in projected_roles:
        destinations.update(
            (path, (role, content)) for path, content in projected.items()
        )
    ancestors_by_parent = {
        parent: _destination_ancestors(project, parent, directory_identities)
        for parent in dict.fromkeys(path.parent for path in destinations)
    }
    before_images_list: list[DestinationImage] = []
    before_budget = AuthoringBudget("authoring before images")
    for path, (role, _content) in destinations.items():
        image = _capture_destination(
            role,
            path,
            ancestors_by_parent[path.parent],
            max_bytes=before_budget.max_bytes_for_next,
        )
        before_images_list.append(image)
        before_budget.retain(image.content)
    before_images = tuple(before_images_list)
    after_images = tuple(
        DestinationImage(
            role,
            path,
            content,
            _digest(content),
            0o644,
            None,
            ancestors_by_parent[path.parent],
        )
        for path, (role, content) in destinations.items()
    )
    return before_images, after_images


def _workflow_recipe(project: Path, name: str) -> AuthoredRecipe:
    validate_logical_name(name)
    workflow = project / ".lockstep" / "workflows" / f"{name}.workflow.yaml"
    recipe = project / ".lockstep" / "recipes" / f"{name}.recipe.yaml"
    return AuthoredRecipe(
        name,
        "workflow",
        workflow,
        recipe,
        recipe.with_name(f"{name}.dependencies.json"),
        recipe.with_name(f"{name}.source-map.json"),
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
    *,
    max_bytes: int,
) -> SourceIdentity:
    ancestors = tuple(
        _cached_directory_identity(directory_identities, ancestor)
        for ancestor in (project, project / ".lockstep", path.parent)
    )
    _validate_ancestor_identities(ancestors)
    content, info = capture_regular_file(
        path, max_bytes=max_bytes, label="workflow source"
    )
    _validate_ancestor_identities(ancestors)
    return SourceIdentity(
        role,
        path,
        content,
        _digest(content),
        _leaf_identity(path, info),
        ancestors,
    )


def _workflow_destinations(
    recipe: AuthoredRecipe,
    compiled: CompilationResult,
    children: tuple[str, ...],
) -> dict[Path, bytes]:
    dependency_path = recipe.dependency_path
    source_map_path = recipe.source_map_path
    if dependency_path is None or source_map_path is None:
        raise AuthoringError("workflow destinations are incomplete")
    destinations = {
        recipe.recipe_path: canonical_recipe_bytes_for_children(
            compiled.recipe_bytes, children
        ),
        dependency_path: compiled.dependency_manifest_bytes,
        source_map_path: compiled.source_map_bytes,
    }
    for item in compiled.generated_files:
        path = recipe.recipe_path.parent / item.relative_path
        if path in destinations:
            raise AuthoringError("compiled workflow contains a duplicate destination")
        destinations[path] = item.content
    return destinations


def _capture_destination(
    role: str,
    path: Path,
    ancestors: tuple[_PathIdentity, ...],
    *,
    max_bytes: int,
) -> DestinationImage:
    _validate_ancestor_identities(ancestors)
    captured = capture_optional_regular_file(
        path,
        max_bytes=max_bytes,
        label="compilation destination",
    )
    if captured is None:
        image = DestinationImage(role, path, None, None, None, None, ancestors)
    else:
        content, info = captured
        image = DestinationImage(
            role,
            path,
            content,
            _digest(content),
            stat.S_IMODE(info.st_mode),
            _leaf_identity(path, info),
            ancestors,
        )
    _validate_ancestor_identities(ancestors)
    return image


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
    info = capture_directory(path, label="destination ancestor")
    return _PathIdentity(path, info.st_dev, info.st_ino)


def _cached_directory_identity(
    directory_identities: dict[Path, _PathIdentity], path: Path
) -> _PathIdentity:
    canonical = _absolute(path, "directory path")
    identity = directory_identities.get(canonical)
    if identity is None:
        identity = _directory_identity(canonical)
        directory_identities[canonical] = identity
    return identity


def _validate_ancestor_identities(ancestors: tuple[_PathIdentity, ...]) -> None:
    for expected in ancestors:
        validate_directory(
            expected.resolved_path,
            device=expected.device,
            inode=expected.inode,
            label="destination ancestor",
        )


def _leaf_identity(path: Path, info: stat_result) -> _LeafIdentity:
    return _LeafIdentity(
        path,
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
