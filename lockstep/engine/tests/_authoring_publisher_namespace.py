"""Exact namespace observations for authoring publisher tests."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from lockstep.authoring_bundle import ProjectCompilationBundle
from tests._authoring_gate import tree_image

DestinationState = tuple[bytes, int] | None


@dataclass(frozen=True, slots=True)
class _NamespaceEntry:
    kind: str
    mode: int
    content: bytes | None
    symlink_target: str | None



def _namespace_file_image(root: Path) -> dict[str, _NamespaceEntry]:
    return {
        key: _NamespaceEntry(
            entry.kind,
            entry.mode,
            entry.content if entry.kind == "regular" else None,
            entry.symlink_target if entry.kind == "symlink" else None,
        )
        for key, entry in tree_image(root).items()
    }




def _regular_file_semantics(path: Path) -> tuple[bytes, int]:
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode)
    return path.read_bytes(), stat.S_IMODE(info.st_mode)




def _regular_file_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode)
    return info.st_dev, info.st_ino




def _destination_states(
    bundle: ProjectCompilationBundle,
) -> dict[Path, DestinationState]:
    states: dict[Path, DestinationState] = {}
    for image in bundle.before_images:
        path = image.resolved_path
        try:
            info = path.lstat()
        except FileNotFoundError:
            states[path] = None
            continue
        assert stat.S_ISREG(info.st_mode)
        states[path] = (path.read_bytes(), stat.S_IMODE(info.st_mode))
    return states




def _is_destination_namespace_call(
    destinations: tuple[Path, ...], destination: object, directory_fd: int | None
) -> bool:
    if directory_fd is None:
        return False
    leaf = os.fsdecode(destination)
    directory_info = os.fstat(directory_fd)
    for path in destinations:
        if path.name != leaf:
            continue
        parent_info = path.parent.stat()
        if (parent_info.st_dev, parent_info.st_ino) == (
            directory_info.st_dev,
            directory_info.st_ino,
        ):
            return True
    return False




def _is_destination_parent_descriptor(
    destinations: tuple[Path, ...], directory_fd: int | None
) -> bool:
    if directory_fd is None:
        return False
    directory_info = os.fstat(directory_fd)
    return any(
        (parent_info.st_dev, parent_info.st_ino)
        == (directory_info.st_dev, directory_info.st_ino)
        for parent_info in (path.parent.stat() for path in destinations)
    )




def _is_exclusive_create(flags: int) -> bool:
    return bool(flags & os.O_CREAT and flags & os.O_EXCL)




def _is_owner_state_destination(
    owner_state: Path, destination: object, directory_fd: int | None
) -> bool:
    path = Path(os.fsdecode(destination))
    if path.is_absolute():
        try:
            path.relative_to(owner_state)
        except ValueError:
            return False
        return True
    if directory_fd is None:
        return False
    directory_info = os.fstat(directory_fd)
    owner_directories = (owner_state,) + tuple(
        item for item in owner_state.rglob("*") if item.is_dir()
    )
    return any(
        (info.st_dev, info.st_ino)
        == (directory_info.st_dev, directory_info.st_ino)
        for info in (item.stat() for item in owner_directories)
    )



def _regular_path_with_identity(
    project: Path, identity: tuple[int, int]
) -> Path:
    matches: list[Path] = []
    for path in project.rglob("*"):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity:
            matches.append(path)
    assert len(matches) == 1
    return matches[0]
