"""Immutable values at the whole-DAG authoring boundary."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
            if before.content is not None and before.leaf is None:
                raise ValueError("a present before-image requires its captured leaf identity")
            if after.leaf is not None:
                raise ValueError("a planned after-image cannot contain a captured leaf identity")


def plan_project_compilation(recipe: AuthoredRecipe) -> ProjectCompilationBundle:
    """Plan one immutable authored closure without publishing it."""

    del recipe
    raise NotImplementedError("whole-DAG compilation planning is not implemented")
