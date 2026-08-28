"""Crash recovery contract for planned-absent authoring destinations."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from lockstep.authoring import project_paths
from lockstep.authoring_bundle import (
    ProjectCompilationBundle,
    plan_project_compilation,
)
from lockstep.authoring_publisher import AuthoringPublisher

from tests._authoring_gate import write_workflow


class _SimulatedProcessDeath(BaseException):
    """Escape the publisher's in-process ``Exception`` rollback boundary."""


@dataclass(frozen=True, slots=True)
class _NamespaceEntry:
    kind: str
    mode: int
    device: int
    inode: int
    content: bytes | None = None
    symlink_target: str | None = None


@dataclass(frozen=True, slots=True)
class _AbsentLeafScenario:
    project: Path
    source: Path
    owner_state: Path
    bundle: ProjectCompilationBundle
    destinations: tuple[Path, ...]
    parent_identities: dict[Path, tuple[int, int, int]]
    source_before: _NamespaceEntry
    project_before: dict[str, _NamespaceEntry]
    owner_before: dict[str, _NamespaceEntry]
    sentinels_before: dict[Path, _NamespaceEntry]


def _namespace_entry(path: Path) -> _NamespaceEntry:
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISREG(info.st_mode):
        return _NamespaceEntry(
            "regular", mode, info.st_dev, info.st_ino, content=path.read_bytes()
        )
    if stat.S_ISDIR(info.st_mode):
        return _NamespaceEntry("directory", mode, info.st_dev, info.st_ino)
    if stat.S_ISLNK(info.st_mode):
        return _NamespaceEntry(
            "symlink",
            mode,
            info.st_dev,
            info.st_ino,
            symlink_target=os.readlink(path),
        )
    return _NamespaceEntry("non-regular", mode, info.st_dev, info.st_ino)


def _namespace_image(root: Path) -> dict[str, _NamespaceEntry]:
    return {
        ".": _namespace_entry(root),
        **{
            path.relative_to(root).as_posix(): _namespace_entry(path)
            for path in sorted(root.rglob("*"))
        },
    }


def _directory_identity(path: Path) -> tuple[int, int, int]:
    info = path.lstat()
    assert stat.S_ISDIR(info.st_mode)
    return info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)


def _prepare_absent_leaf_scenario(tmp_path: Path) -> _AbsentLeafScenario:
    project = tmp_path / "project"
    source = write_workflow(project, "leaf")
    source.chmod(0o640)
    destination_parent = project / ".lockstep" / "recipes"
    destination_parent.mkdir(parents=True)
    destination_parent.chmod(0o750)

    project_sentinel = project / "notes" / "foreign-project.bin"
    project_sentinel.parent.mkdir()
    project_sentinel.write_bytes(b"foreign project bytes\n")
    project_sentinel.chmod(0o640)
    owner_state = (tmp_path / "owner-state").resolve()
    owner_state.mkdir(mode=0o700)
    owner_sentinel = owner_state / "foreign-owner.bin"
    owner_sentinel.write_bytes(b"foreign owner bytes\n")
    owner_sentinel.chmod(0o600)

    bundle = plan_project_compilation(project_paths(project, "leaf"))
    destinations = tuple(image.resolved_path for image in bundle.after_images)
    assert len(destinations) == 3
    assert all(image.content is None for image in bundle.before_images)
    assert all(not path.exists() and not path.is_symlink() for path in destinations)

    destination_parent_sentinels = tuple(
        parent / f"foreign-sibling-{index}.bin"
        for index, parent in enumerate(sorted({path.parent for path in destinations}))
    )
    for index, sentinel in enumerate(destination_parent_sentinels):
        sentinel.write_bytes(f"foreign sibling {index}\n".encode())
        sentinel.chmod(0o600 if index % 2 else 0o640)

    parents = {path.parent for path in destinations}
    parent_identities = {
        parent: _directory_identity(parent)
        for parent in parents
    }
    sentinels = (project_sentinel, owner_sentinel, *destination_parent_sentinels)
    return _AbsentLeafScenario(
        project=project,
        source=source,
        owner_state=owner_state,
        bundle=bundle,
        destinations=destinations,
        parent_identities=parent_identities,
        source_before=_namespace_entry(source),
        project_before=_namespace_image(project),
        owner_before=_namespace_image(owner_state),
        sentinels_before={path: _namespace_entry(path) for path in sentinels},
    )


def _is_destination_link(
    scenario: _AbsentLeafScenario, destination: object, directory_fd: int | None
) -> bool:
    if directory_fd is None:
        return False
    leaf = os.fsdecode(destination)
    directory_info = os.fstat(directory_fd)
    return any(
        path.name == leaf
        and scenario.parent_identities[path.parent][:2]
        == (directory_info.st_dev, directory_info.st_ino)
        for path in scenario.destinations
    )


def _assert_crash_namespace(
    scenario: _AbsentLeafScenario, ordinal: int
) -> None:
    for index, (before, after) in enumerate(
        zip(scenario.bundle.before_images, scenario.bundle.after_images, strict=True)
    ):
        assert before.content is None
        if index <= ordinal:
            assert after.content is not None
            assert after.mode is not None
            observed = _namespace_entry(after.resolved_path)
            assert observed.kind == "regular"
            assert (observed.content, observed.mode) == (after.content, after.mode)
        else:
            assert not after.resolved_path.exists()
            assert not after.resolved_path.is_symlink()


def _assert_sentinels_unchanged(scenario: _AbsentLeafScenario) -> None:
    assert _namespace_entry(scenario.source) == scenario.source_before
    assert {
        path: _namespace_entry(path) for path in scenario.sentinels_before
    } == scenario.sentinels_before


def _assert_opaque_durable_owner_evidence(scenario: _AbsentLeafScenario) -> None:
    owner_after = _namespace_image(scenario.owner_state)
    assert any(
        path not in scenario.owner_before
        and entry.kind == "regular"
        and entry.mode == 0o600
        for path, entry in owner_after.items()
    )


def _assert_destination_parents_unchanged(scenario: _AbsentLeafScenario) -> None:
    assert {
        parent: _directory_identity(parent)
        for parent in scenario.parent_identities
    } == scenario.parent_identities


def _open_can_mutate(flags: int) -> bool:
    access_mode = flags & getattr(os, "O_ACCMODE", os.O_WRONLY | os.O_RDWR)
    mutation_flags = os.O_CREAT | os.O_EXCL | os.O_TRUNC
    mutation_flags |= getattr(os, "O_TMPFILE", 0)
    return access_mode != os.O_RDONLY or bool(flags & mutation_flags)


def _opened_path_identity(
    path: object, directory_fd: int | None
) -> tuple[int, int] | None:
    try:
        info = os.stat(path, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return None
    return info.st_dev, info.st_ino


_MUTATION_SYSCALLS = (
    "chmod",
    "chown",
    "fchown",
    "fchmod",
    "ftruncate",
    "link",
    "lchown",
    "mkfifo",
    "mkdir",
    "mknod",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "symlink",
    "truncate",
    "unlink",
    "utime",
    "write",
    "writev",
    "chflags",
    "fchflags",
    "pwrite",
    "pwritev",
    "removexattr",
    "setxattr",
)


def _instrument_named_mutation(
    monkeypatch: pytest.MonkeyPatch, calls: list[str], name: str
) -> None:
    original: Callable[..., object] = getattr(os, name)

    def record(*args, **kwargs):
        calls.append(name)
        return original(*args, **kwargs)

    monkeypatch.setattr(os, name, record)


def _install_named_mutation_probes(
    monkeypatch: pytest.MonkeyPatch, calls: list[str]
) -> None:
    for name in _MUTATION_SYSCALLS:
        if hasattr(os, name):
            _instrument_named_mutation(monkeypatch, calls, name)


def _write_open_is_allowed(
    path: object,
    flags: int,
    directory_fd: int | None,
    allowed_identities: frozenset[tuple[int, int]],
) -> bool:
    destructive_flags = os.O_EXCL | os.O_TRUNC | getattr(os, "O_TMPFILE", 0)
    return (
        not flags & destructive_flags
        and _opened_path_identity(path, directory_fd) in allowed_identities
    )


def _install_open_mutation_probe(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    allowed_write_open_identities: frozenset[tuple[int, int]],
) -> None:
    original_open = os.open

    def record_open(path, flags, mode=0o777, *, dir_fd=None):
        if _open_can_mutate(flags) and not _write_open_is_allowed(
            path, flags, dir_fd, allowed_write_open_identities
        ):
            calls.append("open")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", record_open)


def _install_mutation_syscall_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    allowed_write_open_identities: frozenset[tuple[int, int]] = frozenset(),
) -> list[str]:
    calls: list[str] = []
    _install_named_mutation_probes(monkeypatch, calls)
    _install_open_mutation_probe(
        monkeypatch, calls, allowed_write_open_identities
    )
    return calls


def _opaque_lock_identities(
    scenario: _AbsentLeafScenario,
    owner_after_recovery: dict[str, _NamespaceEntry],
) -> frozenset[tuple[int, int]]:
    return frozenset(
        (entry.device, entry.inode)
        for path, entry in owner_after_recovery.items()
        if path not in scenario.owner_before
        and entry.kind == "regular"
        and entry.mode == 0o600
        and entry.content == b""
    )


@pytest.mark.parametrize("ordinal", (0, 1, 2))
def test_recover_removes_each_crash_prefix_of_planned_absent_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ordinal: int
) -> None:
    scenario = _prepare_absent_leaf_scenario(tmp_path)
    original_link = os.link
    destination_link_count = 0
    crash_was_injected = False

    def link_then_crash(source, destination, *args, **kwargs):
        nonlocal crash_was_injected, destination_link_count
        result = original_link(source, destination, *args, **kwargs)
        if crash_was_injected or not _is_destination_link(
            scenario, destination, kwargs.get("dst_dir_fd")
        ):
            return result
        current_ordinal = destination_link_count
        destination_link_count += 1
        if current_ordinal == ordinal:
            crash_was_injected = True
            raise _SimulatedProcessDeath(
                "simulated process death after destination hard-link publication"
            )
        return result

    monkeypatch.setattr(os, "link", link_then_crash)
    with pytest.raises(_SimulatedProcessDeath):
        AuthoringPublisher(scenario.owner_state).publish(scenario.bundle)

    assert crash_was_injected
    assert destination_link_count == ordinal + 1
    _assert_crash_namespace(scenario, ordinal)
    _assert_sentinels_unchanged(scenario)
    _assert_opaque_durable_owner_evidence(scenario)
    monkeypatch.setattr(os, "link", original_link)

    AuthoringPublisher(scenario.owner_state).recover(scenario.project)

    assert _namespace_image(scenario.project) == scenario.project_before
    _assert_sentinels_unchanged(scenario)
    _assert_destination_parents_unchanged(scenario)
    project_after_first_recovery = _namespace_image(scenario.project)
    owner_after_first_recovery = _namespace_image(scenario.owner_state)
    allowed_write_open_identities = _opaque_lock_identities(
        scenario, owner_after_first_recovery
    )
    assert allowed_write_open_identities
    mutation_calls = _install_mutation_syscall_probe(
        monkeypatch,
        allowed_write_open_identities=allowed_write_open_identities,
    )

    AuthoringPublisher(scenario.owner_state).recover(scenario.project)

    assert mutation_calls == []
    assert _namespace_image(scenario.project) == project_after_first_recovery
    assert _namespace_image(scenario.owner_state) == owner_after_first_recovery
    _assert_sentinels_unchanged(scenario)
    _assert_destination_parents_unchanged(scenario)
