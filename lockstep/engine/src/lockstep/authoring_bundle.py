"""Immutable values and canonical layout at the authoring boundary."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Literal

import yaml

from lockstep.errors import AuthoringError
from lockstep.workflow.canonical import canonical_yaml
from lockstep.workflow.compiler import CompilationResult
from lockstep.workflow.semantics import ResolvedCatalog, ValidatedWorkflow

__all__ = ["AuthoredRecipe", "AuthoringPlan", "DirectoryIdentity", "FileIdentity",
           "PlannedTarget", "ProjectCompilation", "SourceSnapshot"]


def _absolute(path: Path, label: str) -> Path:
    if not isinstance(path, Path):
        raise TypeError(f"{label} must be a Path")
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be absolute and lexically canonical")
    return path


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
@dataclass(frozen=True, slots=True)
class AuthoredRecipe:
    name: str
    kind: Literal["workflow", "manual"]
    workflow_path: Path | None
    recipe_path: Path
    dependency_path: Path | None
    source_map_path: Path | None
@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    path: Path
    device: int
    inode: int

    def __post_init__(self) -> None:
        _absolute(self.path, "directory identity path")
        if any(type(value) is not int for value in (self.device, self.inode)):
            raise TypeError("directory identity values must be integers")
        if min(self.device, self.inode) < 0:
            raise ValueError("directory identity values must be non-negative")
@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    def __post_init__(self) -> None:
        values = (self.device, self.inode, self.mode, self.size, self.mtime_ns, self.ctime_ns)
        if any(type(value) is not int for value in values):
            raise TypeError("file identity values must be integers")
        if min(self.device, self.inode, self.mode, self.size) < 0:
            raise ValueError("file identity values must be non-negative")
        if not stat.S_ISREG(self.mode):
            raise ValueError("file identity must describe a regular file")
def _parents(value: object, label: str) -> tuple[DirectoryIdentity, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{label} parents must be a tuple")
    if any(not isinstance(item, DirectoryIdentity) for item in value):
        raise TypeError(f"{label} parent identity is invalid")
    paths = tuple(item.path for item in value)
    if len(paths) != len(set(paths)):
        raise ValueError(f"{label} parent identities are invalid")
    return value
@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    role: str
    path: Path
    content: bytes
    sha256: str
    file: FileIdentity
    parents: tuple[DirectoryIdentity, ...]

    def __post_init__(self) -> None:
        _absolute(self.path, "source path")
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("source role must be non-empty")
        if not isinstance(self.content, bytes) or self.sha256 != _digest(self.content):
            raise ValueError("source digest does not match its exact bytes")
        if not isinstance(self.file, FileIdentity) or self.file.size != len(self.content):
            raise ValueError("source file identity does not match its bytes")
        _parents(self.parents, "source")
@dataclass(frozen=True, slots=True)
class PlannedTarget:
    role: str
    path: Path
    before: bytes | None
    before_sha256: str | None
    before_file: FileIdentity | None
    after: bytes
    after_sha256: str
    mode: int
    parents: tuple[DirectoryIdentity, ...]

    def __post_init__(self) -> None:
        _absolute(self.path, "target path")
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("target role must be non-empty")
        if not isinstance(self.after, bytes) or self.after_sha256 != _digest(self.after):
            raise ValueError("target after digest does not match its exact bytes")
        if type(self.mode) is not int or not 0 <= self.mode <= 0o7777:
            raise ValueError("target mode is invalid")
        _parents(self.parents, "target")
        if self.before is None:
            if self.before_sha256 is not None or self.before_file is not None:
                raise ValueError("target absence must have one exact absence image")
        elif (not isinstance(self.before, bytes)
              or self.before_sha256 != _digest(self.before)
              or not isinstance(self.before_file, FileIdentity)
              or self.before_file.size != len(self.before)):
            raise ValueError("target before image is inconsistent")
def _topology(edges: tuple[tuple[str, tuple[str, ...]], ...]) -> tuple[str, ...]:
    if not isinstance(edges, tuple):
        raise TypeError("plan dependency edges must be a tuple")
    roles: list[str] = []
    for edge in edges:
        if not isinstance(edge, tuple) or len(edge) != 2:
            raise TypeError("plan dependency edge is invalid")
        role, children = edge
        if not isinstance(role, str) or not role or role in roles:
            raise ValueError("plan dependency roles must be non-empty and unique")
        if not isinstance(children, tuple) or len(children) != len(set(children)):
            raise ValueError("plan dependencies must reference earlier child roles")
        if any(not isinstance(child, str) or child not in roles for child in children):
            raise ValueError("plan dependencies must reference earlier child roles")
        roles.append(role)
    if not roles:
        raise ValueError("plan dependency roles must be non-empty and unique")
    return tuple(roles)
def _bound_chain(project: Path, parents: tuple[DirectoryIdentity, ...], path: Path) -> None:
    if not parents or parents[0].path != project:
        raise ValueError("plan path is not project-bound")
    previous = project.parent
    for parent in parents:
        if parent.path.parent != previous:
            raise ValueError("plan parent chain is incomplete")
        previous = parent.path
    try:
        path.parent.relative_to(previous)
    except ValueError as exc:
        raise ValueError("plan parent chain is invalid") from exc
def _project_identity_matches(project: Path, identity: object) -> bool:
    return isinstance(identity, DirectoryIdentity) and identity.path == project
@dataclass(frozen=True, slots=True)
class AuthoringPlan:
    project: Path
    project_identity: DirectoryIdentity
    sources: tuple[SourceSnapshot, ...]
    dependency_edges: tuple[tuple[str, tuple[str, ...]], ...]
    targets: tuple[PlannedTarget, ...]

    def __post_init__(self) -> None:
        project = _absolute(self.project, "project path")
        if not _project_identity_matches(project, self.project_identity):
            raise ValueError("project identity does not match its path")
        if not isinstance(self.sources, tuple) or not isinstance(self.targets, tuple):
            raise TypeError("plan sources and targets must be tuples")
        roles = _topology(self.dependency_edges)
        if any(not isinstance(item, SourceSnapshot) for item in self.sources):
            raise TypeError("plan source snapshot is invalid")
        source_roles = tuple(
            role for role, _items in groupby(item.role for item in self.sources)
        )
        if source_roles not in ((), roles):
            raise ValueError("plan source roles must be complete or empty")
        source_paths = tuple(item.path for item in self.sources)
        targets = tuple(item.path for item in self.targets)
        if len(source_paths) != len(set(source_paths)):
            raise ValueError("plan source paths must be unique")
        if any(not isinstance(item, PlannedTarget) or item.role not in roles
               for item in self.targets):
            raise ValueError("plan target roles must match dependency roles")
        if len(targets) != len(set(targets)):
            raise ValueError("plan target paths must be unique")
        if set(source_paths).intersection(targets) or any(
            first in second.parents for first in targets for second in targets
        ):
            raise ValueError("plan source and target paths overlap")
        identities: dict[Path, DirectoryIdentity] = {}
        for item in (*self.sources, *self.targets):
            _bound_chain(project, item.parents, item.path)
            for identity in item.parents:
                if identities.setdefault(identity.path, identity) != identity:
                    raise ValueError("plan contains conflicting directory identities")
@dataclass(frozen=True, slots=True)
class ProjectCompilation:
    plan: AuthoringPlan
    root_validated: ValidatedWorkflow
    root_catalog: ResolvedCatalog
    root_result: CompilationResult

    def __post_init__(self) -> None:
        if not isinstance(self.plan, AuthoringPlan):
            raise TypeError("project compilation plan is invalid")
def canonical_recipe_bytes_for_children(recipe_bytes: bytes, children: tuple[str, ...]) -> bytes:
    if not children:
        return recipe_bytes
    document = yaml.safe_load(recipe_bytes)
    nodes = document.get("nodes") if isinstance(document, dict) else None
    if not isinstance(nodes, dict):
        raise AuthoringError("compiled template recipe has no node catalog")
    for index, child in enumerate(children):
        nodes[f"template-dependency-{index}"] = {"type": "subgraph", "graph": f"{child}.recipe.yaml", "mode": "invoke"}
    return canonical_yaml(document)
def _workflow_project_and_source(recipe: AuthoredRecipe) -> tuple[Path, Path]:
    if recipe.workflow_path is None:
        raise AuthoringError("workflow source is required")
    source = recipe.workflow_path.resolve()
    project = source.parent.parent.parent
    expected = project / ".lockstep" / "workflows" / f"{recipe.name}.workflow.yaml"
    destinations = tuple(project / ".lockstep" / "recipes" / f"{recipe.name}{suffix}"
                         for suffix in (".recipe.yaml", ".dependencies.json", ".source-map.json"))
    if source != expected or (recipe.recipe_path, recipe.dependency_path, recipe.source_map_path) != destinations:
        raise AuthoringError("workflow source or destinations are outside the canonical project layout")
    return project, source
