"""One portable path language and tree budget for snapshots and workspaces."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from lockstep.runtime.owner_state import StorageLimitExceeded, take_bounded

ProjectPathKind = Literal["file", "directory", "prefix"]


class PortablePathError(ValueError):
    pass


class PortablePathCollision(PortablePathError):
    pass


@dataclass(frozen=True)
class ProjectTreeLimits:
    max_entries: int = 10_000
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_depth: int = 256

    def __post_init__(self) -> None:
        if (
            min(
                self.max_entries,
                self.max_file_bytes,
                self.max_total_bytes,
                self.max_depth,
            )
            <= 0
        ):
            raise ValueError("project tree limits must be positive")


@dataclass(frozen=True)
class PortableProjectPath:
    value: str
    relative: PurePosixPath
    kind: ProjectPathKind

    @property
    def is_prefix(self) -> bool:
        return self.kind == "prefix"

    @classmethod
    def parse(cls, raw: str, kind: ProjectPathKind) -> PortableProjectPath:
        if kind not in {"file", "directory", "prefix"}:
            raise TypeError("invalid portable project path kind")
        if (
            not isinstance(raw, str)
            or not raw
            or "\\" in raw
            or any(character in raw for character in '<>:"|?*')
            or any(ord(character) < 32 for character in raw)
        ):
            raise PortablePathError(f"unsafe portable project path {raw!r}")
        has_slash = raw.endswith("/")
        if has_slash != (kind == "prefix"):
            raise PortablePathError(f"non-canonical portable project path {raw!r}")
        body = raw[:-1] if has_slash else raw
        path = PurePosixPath(body)
        if (
            not body
            or body in {".", ".."}
            or body.startswith("/")
            or path.as_posix() != body
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise PortablePathError(f"unsafe portable project path {raw!r}")
        for index, part in enumerate(path.parts):
            if _windows_alias(part):
                raise PortablePathError(f"platform path alias is not allowed: {raw!r}")
            if index == 0 and portable_collision_key(part) == portable_collision_key(
                ".git"
            ):
                raise PortablePathError(".git is never a portable project path")
        return cls(raw, path, kind)


def portable_collision_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _windows_alias(value: str) -> bool:
    stripped = value.rstrip(". ")
    if stripped != value or not stripped:
        return True
    base = stripped.split(".", 1)[0].upper()
    return base in {"CON", "PRN", "AUX", "NUL", "CLOCK$"} or (
        len(base) == 4 and base[:3] in {"COM", "LPT"} and base[3] in "123456789"
    )


def validate_portable_project_paths(
    entries: Iterable[tuple[str, ProjectPathKind]],
    *,
    limits: ProjectTreeLimits,
    label: str,
) -> tuple[PortableProjectPath, ...]:
    raw_entries = take_bounded(entries, limits.max_entries, label)
    parsed = tuple(PortableProjectPath.parse(raw, kind) for raw, kind in raw_entries)
    explicit: dict[str, PortableProjectPath] = {}
    siblings: dict[tuple[str, ...], dict[str, str]] = {}
    tree_nodes: set[str] = set()
    for item in parsed:
        normalized = item.relative.as_posix()
        if normalized in explicit:
            raise PortablePathCollision(
                f"duplicate portable project path {item.value!r}"
            )
        explicit[normalized] = item
        for index, part in enumerate(item.relative.parts):
            parent = tuple(item.relative.parts[:index])
            seen = siblings.setdefault(parent, {})
            key = portable_collision_key(part)
            previous = seen.get(key)
            if previous is not None and previous != part:
                raise PortablePathCollision(
                    f"portable project path collision: {previous!r} and {part!r}"
                )
            seen[key] = part
            tree_nodes.add(PurePosixPath(*item.relative.parts[: index + 1]).as_posix())
            if len(tree_nodes) > limits.max_entries:
                raise StorageLimitExceeded(
                    f"{label} entries exceed {limits.max_entries} admission limit"
                )
    for item in parsed:
        for parent in item.relative.parents:
            if parent.as_posix() == ".":
                continue
            ancestor = explicit.get(parent.as_posix())
            if ancestor is not None and ancestor.kind == "file":
                raise PortablePathCollision(
                    f"portable project file collides with descendant {item.value!r}"
                )
    return parsed
