"""Crash recovery contract for one transaction-created destination parent."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable

import pytest

from lockstep.authoring import project_paths
from lockstep.authoring_bundle import (
    ProjectCompilationBundle,
    plan_project_compilation,
)
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.errors import AuthoringError

from tests._authoring_gate import write_workflow
from tests._authoring_crash_gate import (
    NamespaceEntry as _NamespaceEntry,
    directory_identity as _directory_identity,
    install_mutation_syscall_probe as _install_mutation_syscall_probe,
    namespace_entry as _namespace_entry,
    namespace_image as _namespace_image,
    opaque_lock_identities,
)


class _SimulatedProcessDeath(BaseException):
    """Escape the publisher's in-process rollback boundary."""


@dataclass(frozen=True, slots=True)
class _CreatedParentScenario:
    project: Path
    source: Path
    owner_state: Path
    bundle: ProjectCompilationBundle
    destinations: tuple[Path, ...]
    destination_parent: Path
    existing_parent: Path
    existing_parent_identity: tuple[int, int, int]
    project_before: dict[str, _NamespaceEntry]
    owner_before: dict[str, _NamespaceEntry]
    sentinels_before: dict[Path, _NamespaceEntry]


@dataclass(slots=True)
class _CreatedParentFault:
    cut: str
    injected: bool = False
    mkdir_completed: bool = False
    reopened_identity: tuple[int, int] | None = None
    reopened_mode: int | None = None
    parent_fsync_seen: bool = False
    destination_link_count: int = 0
    owner_directory_identities: frozenset[tuple[int, int]] = frozenset()
    owner_progress_parent: tuple[int, int] | None = None
    owner_progress_generation: int = 0
    owner_durable_generation: int = 0

    @property
    def identity_progress_durable(self) -> bool:
        return (
            self.owner_progress_generation > 0
            and self.owner_durable_generation == self.owner_progress_generation
        )


def _prepare_created_parent_scenario(tmp_path: Path) -> _CreatedParentScenario:
    project = tmp_path / "project"
    source = write_workflow(project, "leaf")
    source.chmod(0o640)
    existing_parent = project / ".lockstep"
    destination_parent = existing_parent / "recipes"
    assert existing_parent.is_dir()
    assert not destination_parent.exists() and not destination_parent.is_symlink()

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
    assert {path.parent for path in destinations} == {destination_parent}
    assert all(image.content is None for image in bundle.before_images)
    assert all(not path.exists() and not path.is_symlink() for path in destinations)
    existing_parent_identity = _directory_identity(existing_parent)
    expected_ancestor_paths = (project.resolve(), existing_parent.resolve())
    for image in (*bundle.before_images, *bundle.after_images):
        assert tuple(
            ancestor.resolved_path for ancestor in image.ancestors
        ) == expected_ancestor_paths
        captured_parent = image.ancestors[-1]
        assert (
            captured_parent.device,
            captured_parent.inode,
            existing_parent_identity[2],
        ) == existing_parent_identity
    sentinels = (source, project_sentinel, owner_sentinel)
    return _CreatedParentScenario(
        project=project,
        source=source,
        owner_state=owner_state,
        bundle=bundle,
        destinations=destinations,
        destination_parent=destination_parent,
        existing_parent=existing_parent,
        existing_parent_identity=existing_parent_identity,
        project_before=_namespace_image(project),
        owner_before=_namespace_image(owner_state),
        sentinels_before={path: _namespace_entry(path) for path in sentinels},
    )


def _is_existing_parent_descriptor(
    scenario: _CreatedParentScenario, descriptor: int | None
) -> bool:
    if descriptor is None:
        return False
    info = os.fstat(descriptor)
    return (info.st_dev, info.st_ino) == scenario.existing_parent_identity[:2]


def _is_target_mkdir(
    scenario: _CreatedParentScenario, path: object, descriptor: int | None
) -> bool:
    return (
        os.fsdecode(path) == scenario.destination_parent.name
        and _is_existing_parent_descriptor(scenario, descriptor)
    )


def _is_destination_link(
    scenario: _CreatedParentScenario,
    fault: _CreatedParentFault,
    destination: object,
    descriptor: int | None,
) -> bool:
    if descriptor is None or fault.reopened_identity is None:
        return False
    info = os.fstat(descriptor)
    return (
        os.fsdecode(destination) in {path.name for path in scenario.destinations}
        and (info.st_dev, info.st_ino) == fault.reopened_identity
    )


def _owner_directory_identities(owner_state: Path) -> frozenset[tuple[int, int]]:
    identities: set[tuple[int, int]] = set()
    for path in (owner_state, *owner_state.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            identities.add((info.st_dev, info.st_ino))
    return frozenset(identities)


def _mutation_parent_identity(
    destination: object, directory_fd: int | None
) -> tuple[int, int] | None:
    if directory_fd is not None:
        info = os.fstat(directory_fd)
        return info.st_dev, info.st_ino
    path = Path(os.fsdecode(destination))
    if not path.is_absolute():
        return None
    try:
        info = path.parent.lstat()
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _is_nonempty_owner_evidence(
    destination: object, directory_fd: int | None
) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags, dir_fd=directory_fd)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        return (
            stat.S_ISREG(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_size > 0
        )
    finally:
        os.close(descriptor)


def _mkdir_with_fault(
    original: Callable[..., object],
    scenario: _CreatedParentScenario,
    fault: _CreatedParentFault,
    path,
    mode=0o777,
    *,
    dir_fd=None,
):
    target = _is_target_mkdir(scenario, path, dir_fd)
    if target and fault.cut == "before_mkdir":
        fault.injected = True
        raise _SimulatedProcessDeath("before destination-parent mkdir")
    if target:
        fault.owner_directory_identities = _owner_directory_identities(
            scenario.owner_state
        )
    result = original(path, mode, dir_fd=dir_fd)
    if target:
        fault.mkdir_completed = True
        if fault.cut == "after_mkdir":
            fault.injected = True
            raise _SimulatedProcessDeath("after destination-parent mkdir")
    return result


def _open_with_fault(
    original: Callable[..., int],
    scenario: _CreatedParentScenario,
    fault: _CreatedParentFault,
    path,
    flags,
    mode=0o777,
    *,
    dir_fd=None,
):
    descriptor = original(path, flags, mode, dir_fd=dir_fd)
    if (
        fault.mkdir_completed
        and _is_target_mkdir(scenario, path, dir_fd)
        and flags & getattr(os, "O_DIRECTORY", 0)
    ):
        info = os.fstat(descriptor)
        fault.reopened_identity = (info.st_dev, info.st_ino)
        fault.reopened_mode = stat.S_IMODE(info.st_mode)
    return descriptor


def _fsync_with_fault(
    original: Callable[[int], None],
    scenario: _CreatedParentScenario,
    fault: _CreatedParentFault,
    descriptor: int,
) -> None:
    original(descriptor)
    info = os.fstat(descriptor)
    identity = (info.st_dev, info.st_ino)
    if (
        fault.owner_progress_generation > 0
        and identity == fault.owner_progress_parent
    ):
        fault.owner_durable_generation = fault.owner_progress_generation
    if (
        fault.reopened_identity is not None
        and _is_existing_parent_descriptor(scenario, descriptor)
    ):
        fault.parent_fsync_seen = True
        if fault.cut == "after_parent_fsync" and not fault.injected:
            fault.injected = True
            raise _SimulatedProcessDeath("after durable destination-parent mkdir")


def _link_with_fault(
    original: Callable[..., object],
    scenario: _CreatedParentScenario,
    fault: _CreatedParentFault,
    source,
    destination,
    *args,
    **kwargs,
):
    result = original(source, destination, *args, **kwargs)
    if _is_destination_link(
        scenario, fault, destination, kwargs.get("dst_dir_fd")
    ):
        fault.destination_link_count += 1
        if fault.cut == "after_first_link" and fault.destination_link_count == 1:
            fault.injected = True
            raise _SimulatedProcessDeath("after first destination link")
    return result


def _replace_with_observer(
    original: Callable[..., object],
    fault: _CreatedParentFault,
    source,
    destination,
    *args,
    **kwargs,
):
    result = original(source, destination, *args, **kwargs)
    parent_identity = _mutation_parent_identity(
        destination, kwargs.get("dst_dir_fd")
    )
    if (
        fault.mkdir_completed
        and fault.reopened_identity is not None
        and parent_identity in fault.owner_directory_identities
        and _is_nonempty_owner_evidence(
            destination, kwargs.get("dst_dir_fd")
        )
    ):
        fault.owner_progress_parent = parent_identity
        fault.owner_progress_generation += 1
    return result


def _install_mkdir_fault(
    monkeypatch: pytest.MonkeyPatch,
    scenario: _CreatedParentScenario,
    fault: _CreatedParentFault,
) -> None:
    monkeypatch.setattr(
        os, "mkdir", partial(_mkdir_with_fault, os.mkdir, scenario, fault)
    )


def _install_open_observer(
    monkeypatch: pytest.MonkeyPatch,
    scenario: _CreatedParentScenario,
    fault: _CreatedParentFault,
) -> None:
    monkeypatch.setattr(
        os, "open", partial(_open_with_fault, os.open, scenario, fault)
    )


def _install_fsync_fault(
    monkeypatch: pytest.MonkeyPatch,
    scenario: _CreatedParentScenario,
    fault: _CreatedParentFault,
) -> None:
    monkeypatch.setattr(
        os, "fsync", partial(_fsync_with_fault, os.fsync, scenario, fault)
    )


def _install_link_fault(
    monkeypatch: pytest.MonkeyPatch,
    scenario: _CreatedParentScenario,
    fault: _CreatedParentFault,
) -> None:
    monkeypatch.setattr(
        os, "link", partial(_link_with_fault, os.link, scenario, fault)
    )


def _install_owner_replace_observer(
    monkeypatch: pytest.MonkeyPatch, fault: _CreatedParentFault
) -> None:
    monkeypatch.setattr(
        os, "replace", partial(_replace_with_observer, os.replace, fault)
    )


def _install_created_parent_fault(
    monkeypatch: pytest.MonkeyPatch,
    scenario: _CreatedParentScenario,
    cut: str,
) -> _CreatedParentFault:
    assert cut in {
        "before_mkdir",
        "after_mkdir",
        "after_parent_fsync",
        "after_first_link",
    }
    fault = _CreatedParentFault(cut)
    _install_mkdir_fault(monkeypatch, scenario, fault)
    _install_open_observer(monkeypatch, scenario, fault)
    _install_fsync_fault(monkeypatch, scenario, fault)
    _install_link_fault(monkeypatch, scenario, fault)
    _install_owner_replace_observer(monkeypatch, fault)
    return fault


def _crash_publication(
    scenario: _CreatedParentScenario,
    monkeypatch: pytest.MonkeyPatch,
    cut: str,
) -> _CreatedParentFault:
    with monkeypatch.context() as crash_patch:
        fault = _install_created_parent_fault(crash_patch, scenario, cut)
        with pytest.raises(_SimulatedProcessDeath):
            AuthoringPublisher(scenario.owner_state).publish(scenario.bundle)
    assert fault.injected
    return fault


def _assert_sentinels_and_existing_parent(
    scenario: _CreatedParentScenario,
) -> None:
    assert _directory_identity(scenario.existing_parent) == (
        scenario.existing_parent_identity
    )
    assert {
        path: _namespace_entry(path) for path in scenario.sentinels_before
    } == scenario.sentinels_before


def _assert_active_owner_evidence(scenario: _CreatedParentScenario) -> None:
    owner_after = _namespace_image(scenario.owner_state)
    assert any(
        path not in scenario.owner_before
        and entry.kind == "regular"
        and entry.mode == 0o600
        and entry.content not in {None, b""}
        for path, entry in owner_after.items()
    )


def _assert_active_owner_evidence_gone(
    scenario: _CreatedParentScenario,
) -> None:
    owner_after = _namespace_image(scenario.owner_state)
    assert not any(
        path not in scenario.owner_before
        and entry.kind == "regular"
        and entry.mode == 0o600
        and entry.content not in {None, b""}
        for path, entry in owner_after.items()
    )


def _assert_common_crash_evidence(scenario: _CreatedParentScenario) -> None:
    _assert_sentinels_and_existing_parent(scenario)
    _assert_active_owner_evidence(scenario)


def _assert_only_project_additions(
    scenario: _CreatedParentScenario, additions: tuple[Path, ...]
) -> None:
    observed = _namespace_image(scenario.project)
    assert {
        path: observed[path] for path in scenario.project_before
    } == scenario.project_before
    assert set(observed) - set(scenario.project_before) == {
        path.relative_to(scenario.project).as_posix() for path in additions
    }


def _assert_empty_created_parent(
    scenario: _CreatedParentScenario, fault: _CreatedParentFault
) -> _NamespaceEntry:
    assert fault.mkdir_completed
    observed = _namespace_entry(scenario.destination_parent)
    assert observed.kind == "directory"
    if fault.reopened_identity is not None:
        assert (observed.device, observed.inode) == fault.reopened_identity
        assert observed.mode == fault.reopened_mode
    assert tuple(scenario.destination_parent.iterdir()) == ()
    assert all(
        not path.exists() and not path.is_symlink()
        for path in scenario.destinations
    )
    _assert_only_project_additions(scenario, (scenario.destination_parent,))
    return observed


def _assert_first_link_namespace(
    scenario: _CreatedParentScenario, fault: _CreatedParentFault
) -> None:
    assert fault.destination_link_count == 1
    assert fault.parent_fsync_seen
    assert fault.reopened_identity is not None
    parent = _namespace_entry(scenario.destination_parent)
    assert (parent.device, parent.inode, parent.mode) == (
        *fault.reopened_identity,
        fault.reopened_mode,
    )
    first = scenario.destinations[0]
    first_after = scenario.bundle.after_images[0]
    assert first_after.content is not None and first_after.mode is not None
    first_observed = _namespace_entry(first)
    assert (first_observed.kind, first_observed.content, first_observed.mode) == (
        "regular",
        first_after.content,
        first_after.mode,
    )
    assert all(
        not path.exists() and not path.is_symlink()
        for path in scenario.destinations[1:]
    )
    opaque_stages = tuple(
        path
        for path in scenario.destination_parent.iterdir()
        if path not in scenario.destinations
    )
    assert len(opaque_stages) == len(scenario.destinations)
    observed_stages = tuple(_namespace_entry(path) for path in opaque_stages)
    assert all(entry.kind == "regular" for entry in observed_stages)
    assert sorted((entry.content, entry.mode) for entry in observed_stages) == sorted(
        (image.content, image.mode) for image in scenario.bundle.after_images
    )
    assert sum(
        (entry.device, entry.inode)
        == (first_observed.device, first_observed.inode)
        for entry in observed_stages
    ) == 1
    _assert_only_project_additions(
        scenario,
        (scenario.destination_parent, first, *opaque_stages),
    )


def _recover_successfully_twice(
    scenario: _CreatedParentScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    AuthoringPublisher(scenario.owner_state).recover(scenario.project)
    assert _namespace_image(scenario.project) == scenario.project_before
    _assert_sentinels_and_existing_parent(scenario)
    _assert_active_owner_evidence_gone(scenario)
    project_after = _namespace_image(scenario.project)
    owner_after = _namespace_image(scenario.owner_state)
    allowed = opaque_lock_identities(scenario.owner_before, owner_after)
    assert allowed
    with monkeypatch.context() as mutation_patch:
        calls = _install_mutation_syscall_probe(
            mutation_patch, allowed_write_open_identities=allowed
        )
        AuthoringPublisher(scenario.owner_state).recover(scenario.project)
    assert calls == []
    assert _namespace_image(scenario.project) == project_after
    assert _namespace_image(scenario.owner_state) == owner_after


def _recover_fails_closed_twice(
    scenario: _CreatedParentScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_before = _namespace_image(scenario.project)
    owner_before = _namespace_image(scenario.owner_state)
    allowed = opaque_lock_identities(scenario.owner_before, owner_before)
    assert allowed
    with monkeypatch.context() as mutation_patch:
        calls = _install_mutation_syscall_probe(
            mutation_patch, allowed_write_open_identities=allowed
        )
        for _ in range(2):
            with pytest.raises(AuthoringError):
                AuthoringPublisher(scenario.owner_state).recover(scenario.project)
    assert calls == []
    assert _namespace_image(scenario.project) == project_before
    assert _namespace_image(scenario.owner_state) == owner_before
    _assert_common_crash_evidence(scenario)


def _crash_after_parent_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[_CreatedParentScenario, _CreatedParentFault]:
    scenario = _prepare_created_parent_scenario(tmp_path)
    fault = _crash_publication(scenario, monkeypatch, "after_parent_fsync")
    assert fault.parent_fsync_seen
    assert fault.mkdir_completed
    assert fault.reopened_identity is not None
    assert fault.reopened_mode is not None
    _assert_empty_created_parent(scenario, fault)
    _assert_common_crash_evidence(scenario)
    return scenario, fault


def test_recover_finishes_when_process_dies_before_created_parent_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _prepare_created_parent_scenario(tmp_path)
    fault = _crash_publication(scenario, monkeypatch, "before_mkdir")

    assert not fault.mkdir_completed
    assert fault.reopened_identity is None
    assert not scenario.destination_parent.exists()
    assert _namespace_image(scenario.project) == scenario.project_before
    _assert_common_crash_evidence(scenario)

    _recover_successfully_twice(scenario, monkeypatch)


def test_recover_fails_closed_after_mkdir_before_created_parent_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _prepare_created_parent_scenario(tmp_path)
    fault = _crash_publication(scenario, monkeypatch, "after_mkdir")

    assert fault.reopened_identity is None
    assert not fault.parent_fsync_seen
    _assert_empty_created_parent(scenario, fault)
    _assert_common_crash_evidence(scenario)

    _recover_fails_closed_twice(scenario, monkeypatch)
    _assert_empty_created_parent(scenario, fault)


def test_recover_removes_owned_empty_parent_after_parent_fsync_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario, fault = _crash_after_parent_fsync(tmp_path, monkeypatch)

    assert fault.identity_progress_durable
    _recover_successfully_twice(scenario, monkeypatch)


def test_recover_removes_first_published_leaf_before_owned_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _prepare_created_parent_scenario(tmp_path)
    fault = _crash_publication(scenario, monkeypatch, "after_first_link")

    assert fault.reopened_identity is not None
    assert fault.reopened_mode is not None
    assert fault.identity_progress_durable
    _assert_first_link_namespace(scenario, fault)
    _assert_common_crash_evidence(scenario)

    _recover_successfully_twice(scenario, monkeypatch)


def test_recover_preserves_foreign_empty_replacement_for_created_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario, fault = _crash_after_parent_fsync(tmp_path, monkeypatch)
    replacement = scenario.existing_parent / "foreign-empty-replacement"
    assert fault.reopened_identity is not None
    assert fault.reopened_mode is not None
    replacement.mkdir(mode=fault.reopened_mode)
    scenario.destination_parent.rmdir()
    replacement.rename(scenario.destination_parent)
    parent_descriptor = os.open(scenario.existing_parent, os.O_RDONLY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    foreign = _namespace_entry(scenario.destination_parent)
    assert foreign.kind == "directory"
    assert (foreign.device, foreign.inode) != fault.reopened_identity
    assert foreign.mode == fault.reopened_mode

    _recover_fails_closed_twice(scenario, monkeypatch)
    assert _namespace_entry(scenario.destination_parent) == foreign


def test_recover_preserves_foreign_child_in_owned_created_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario, fault = _crash_after_parent_fsync(tmp_path, monkeypatch)
    foreign_child = scenario.destination_parent / "foreign-user.bin"
    foreign_child.write_bytes(b"foreign user bytes\n")
    foreign_child.chmod(0o640)
    child_descriptor = os.open(foreign_child, os.O_RDONLY)
    directory_descriptor = os.open(scenario.destination_parent, os.O_RDONLY)
    try:
        os.fsync(child_descriptor)
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
        os.close(child_descriptor)
    foreign_before = _namespace_entry(foreign_child)
    assert (foreign_before.content, foreign_before.mode) == (
        b"foreign user bytes\n",
        0o640,
    )
    assert _directory_identity(scenario.destination_parent) == (
        *fault.reopened_identity,
        fault.reopened_mode,
    )

    _recover_fails_closed_twice(scenario, monkeypatch)
    assert _namespace_entry(foreign_child) == foreign_before
