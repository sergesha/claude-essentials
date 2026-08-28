"""Crash recovery contract at the authoring transaction commit boundary."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from lockstep.authoring import project_paths
from lockstep.authoring_bundle import ProjectCompilationBundle, plan_project_compilation
from lockstep.authoring_publisher import AuthoringPublisher

from tests._authoring_gate import replace_marker, write_workflow
from tests._authoring_crash_gate import (
    NamespaceEntry,
    install_mutation_syscall_probe,
    namespace_entry,
    namespace_image,
    opaque_lock_identities,
)


class _SimulatedProcessDeath(BaseException):
    """Escape the publisher's in-process ``Exception`` rollback boundary."""


_DURABLE_SOURCE_EDIT = b"externally edited and durably synced source\n"


@dataclass(frozen=True, slots=True)
class _Scenario:
    project: Path
    source: Path
    owner_state: Path
    bundle: ProjectCompilationBundle
    destinations: tuple[Path, ...]
    project_before: dict[str, NamespaceEntry]
    owner_before: dict[str, NamespaceEntry]
    source_before: NamespaceEntry
    destination_before: dict[Path, NamespaceEntry]


@dataclass(frozen=True, slots=True)
class _EvidenceIdentity:
    path: Path
    device: int
    inode: int
    parent_identity: tuple[int, int]


@dataclass(slots=True)
class _DurableProgress:
    owner_directories: frozenset[tuple[int, int]]
    generation: int = 0
    durable_generation: int = 0
    parent_identity: tuple[int, int] | None = None
    injected: bool = False


def _prepare_scenario(tmp_path: Path) -> _Scenario:
    project = tmp_path / "project"
    source = write_workflow(project, "leaf", marker="old marker")
    source.chmod(0o640)
    project_sentinel = project / "notes" / "foreign-project.bin"
    project_sentinel.parent.mkdir()
    project_sentinel.write_bytes(b"foreign project bytes\n")
    project_sentinel.chmod(0o640)

    owner_state = (tmp_path / "owner-state").resolve()
    owner_state.mkdir(mode=0o700)
    owner_sentinel = owner_state / "foreign-owner.bin"
    owner_sentinel.write_bytes(b"foreign owner bytes\n")
    owner_sentinel.chmod(0o600)

    old_bundle = plan_project_compilation(project_paths(project, "leaf"))
    AuthoringPublisher(owner_state).publish(old_bundle)
    replace_marker(source, "old marker", "new marker")
    source.chmod(0o640)
    bundle = plan_project_compilation(project_paths(project, "leaf"))
    destinations = tuple(image.resolved_path for image in bundle.after_images)
    assert len(destinations) == 3
    assert all(image.content is not None for image in bundle.before_images)
    assert any(
        before.content != after.content
        for before, after in zip(
            bundle.before_images, bundle.after_images, strict=True
        )
    )
    destination_before = {path: namespace_entry(path) for path in destinations}
    for before, destination in zip(
        bundle.before_images, destinations, strict=True
    ):
        observed = destination_before[destination]
        assert (observed.content, observed.mode) == (before.content, before.mode)
    return _Scenario(
        project=project,
        source=source,
        owner_state=owner_state,
        bundle=bundle,
        destinations=destinations,
        project_before=namespace_image(project),
        owner_before=namespace_image(owner_state),
        source_before=namespace_entry(source),
        destination_before=destination_before,
    )


def _planned_after_is_published(scenario: _Scenario) -> bool:
    for destination, after in zip(
        scenario.destinations, scenario.bundle.after_images, strict=True
    ):
        try:
            observed = namespace_entry(destination)
        except FileNotFoundError:
            return False
        before = scenario.destination_before[destination]
        if (
            observed.kind != "regular"
            or (observed.content, observed.mode) != (after.content, after.mode)
            or (observed.device, observed.inode) == (before.device, before.inode)
        ):
            return False
    return True


def _owner_directory_for_descriptor(
    owner_state: Path, directory_fd: int
) -> Path | None:
    descriptor_info = os.fstat(directory_fd)
    identity = descriptor_info.st_dev, descriptor_info.st_ino
    for candidate in (owner_state, *owner_state.rglob("*")):
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == identity:
            return candidate
    return None


def _owner_relative_path(
    owner_state: Path, path: object, directory_fd: int | None
) -> tuple[Path, str] | None:
    candidate = Path(os.fsdecode(path))
    if directory_fd is not None:
        parent = _owner_directory_for_descriptor(owner_state, directory_fd)
        if parent is None or candidate.is_absolute():
            return None
        candidate = parent / candidate
    elif not candidate.is_absolute():
        return None
    try:
        relative = candidate.relative_to(owner_state)
    except ValueError:
        return None
    return candidate, relative.as_posix()


def _qualifying_owner_evidence(
    scenario: _Scenario, path: object, directory_fd: int | None = None
) -> _EvidenceIdentity | None:
    resolved = _owner_relative_path(scenario.owner_state, path, directory_fd)
    if resolved is None:
        return None
    candidate, relative = resolved
    if relative in scenario.owner_before:
        return None
    try:
        info = os.stat(path, dir_fd=directory_fd, follow_symlinks=False)
        parent = (
            os.fstat(directory_fd)
            if directory_fd is not None
            else candidate.parent.lstat()
        )
    except OSError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size == 0
    ):
        return None
    return _EvidenceIdentity(
        candidate, info.st_dev, info.st_ino, (parent.st_dev, parent.st_ino)
    )


def _install_terminal_unlink_fault(
    monkeypatch: pytest.MonkeyPatch,
    scenario: _Scenario,
    edited_source: bytes,
) -> list[_EvidenceIdentity]:
    captured: list[_EvidenceIdentity] = []
    original_open = os.open
    original_fsync = os.fsync

    def instrument(original: Callable[..., object]):
        def unlink(path, *args, **kwargs):
            evidence = _qualifying_owner_evidence(
                scenario, path, kwargs.get("dir_fd")
            )
            if evidence is not None and not captured:
                captured.append(evidence)
                _write_source_durably(
                    scenario,
                    edited_source,
                    open_call=original_open,
                    fsync_call=original_fsync,
                )
                raise _SimulatedProcessDeath("before terminal evidence unlink")
            return original(path, *args, **kwargs)

        return unlink

    monkeypatch.setattr(os, "unlink", instrument(os.unlink))
    monkeypatch.setattr(os, "remove", instrument(os.remove))
    return captured


def _assert_active_evidence(
    scenario: _Scenario, expected: _EvidenceIdentity | None = None
) -> _EvidenceIdentity:
    candidates: list[_EvidenceIdentity] = []
    for path in scenario.owner_state.rglob("*"):
        evidence = _qualifying_owner_evidence(scenario, path)
        if evidence is not None:
            candidates.append(evidence)
    assert len(candidates) == 1
    if expected is not None:
        assert candidates[0] == expected
    return candidates[0]


def _install_retirement_observer(
    monkeypatch: pytest.MonkeyPatch, evidence: _EvidenceIdentity
) -> tuple[list[str], list[bool]]:
    events: list[str] = []
    durable = [False]
    original_unlink = os.unlink
    original_remove = os.remove
    original_fsync = os.fsync

    def instrument(original: Callable[..., object]):
        def unlink(path, *args, **kwargs):
            directory_fd = kwargs.get("dir_fd")
            target = Path(os.fsdecode(path))
            if directory_fd is None:
                path_matches = target == evidence.path
            else:
                parent = os.fstat(directory_fd)
                path_matches = (
                    not target.is_absolute()
                    and target == Path(evidence.path.name)
                    and (parent.st_dev, parent.st_ino)
                    == evidence.parent_identity
                )
            removes_captured_evidence = False
            if path_matches:
                try:
                    info = os.stat(
                        path,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    pass
                else:
                    removes_captured_evidence = (info.st_dev, info.st_ino) == (
                        evidence.device,
                        evidence.inode,
                    )
            result = original(path, *args, **kwargs)
            if removes_captured_evidence:
                events.append("unlink")
            return result

        return unlink

    def fsync(descriptor: int) -> None:
        original_fsync(descriptor)
        info = os.fstat(descriptor)
        if events and (info.st_dev, info.st_ino) == evidence.parent_identity:
            events.append("parent-fsync")
            durable[0] = True

    monkeypatch.setattr(os, "unlink", instrument(original_unlink))
    monkeypatch.setattr(os, "remove", instrument(original_remove))
    monkeypatch.setattr(os, "fsync", fsync)
    return events, durable


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


def _is_current_owner_evidence(
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
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_size > 0
        )
    finally:
        os.close(descriptor)


def _write_source_durably(
    scenario: _Scenario,
    content: bytes,
    *,
    open_call: Callable[..., int],
    fsync_call: Callable[[int], None],
) -> None:
    descriptor = open_call(scenario.source, os.O_WRONLY | os.O_TRUNC)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            assert written > 0
            view = view[written:]
        fsync_call(descriptor)
    finally:
        os.close(descriptor)
    parent = open_call(
        scenario.source.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        fsync_call(parent)
    finally:
        os.close(parent)


def _install_source_drift_fault(
    monkeypatch: pytest.MonkeyPatch,
    scenario: _Scenario,
    edited_source: bytes,
) -> _DurableProgress:
    progress = _DurableProgress(_owner_directory_identities(scenario.owner_state))
    original_replace = os.replace
    original_fsync = os.fsync
    original_open = os.open

    def replace(source, destination, *args, **kwargs):
        result = original_replace(source, destination, *args, **kwargs)
        parent = _mutation_parent_identity(
            destination, kwargs.get("dst_dir_fd")
        )
        if (
            parent in progress.owner_directories
            and _is_current_owner_evidence(
                destination, kwargs.get("dst_dir_fd")
            )
        ):
            progress.parent_identity = parent
            progress.generation += 1
        return result

    def fsync(descriptor: int) -> None:
        original_fsync(descriptor)
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        if progress.generation and identity == progress.parent_identity:
            progress.durable_generation = progress.generation
            if not progress.injected and _planned_after_is_published(scenario):
                progress.injected = True
                _write_source_durably(
                    scenario,
                    edited_source,
                    open_call=original_open,
                    fsync_call=original_fsync,
                )
                raise _SimulatedProcessDeath(
                    "after terminal replacement progress, before source validation"
                )

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", fsync)
    return progress


def _assert_destinations_match_before(scenario: _Scenario) -> None:
    for destination, before in scenario.destination_before.items():
        observed = namespace_entry(destination)
        assert (observed.kind, observed.content, observed.mode) == (
            before.kind,
            before.content,
            before.mode,
        )


def _namespace_outside(
    image: dict[str, NamespaceEntry], root: Path, paths: tuple[Path, ...]
) -> dict[str, NamespaceEntry]:
    excluded = {path.relative_to(root).as_posix() for path in paths}
    return {path: entry for path, entry in image.items() if path not in excluded}


def _owner_without_evidence(
    scenario: _Scenario,
    image: dict[str, NamespaceEntry],
    evidence: _EvidenceIdentity,
) -> dict[str, NamespaceEntry]:
    relative = evidence.path.relative_to(scenario.owner_state).as_posix()
    return {path: entry for path, entry in image.items() if path != relative}


def _assert_second_recovery_is_mutation_free(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_before = namespace_image(scenario.project)
    owner_before = namespace_image(scenario.owner_state)
    allowed = opaque_lock_identities({}, owner_before)
    assert allowed
    with monkeypatch.context() as probe:
        calls = install_mutation_syscall_probe(
            probe, allowed_write_open_identities=allowed
        )
        AuthoringPublisher(scenario.owner_state).recover(scenario.project)
    assert calls == []
    assert namespace_image(scenario.project) == project_before
    assert namespace_image(scenario.owner_state) == owner_before


def test_recovery_preserves_committed_outputs_after_terminal_evidence_unlink_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _prepare_scenario(tmp_path)
    captured = _install_terminal_unlink_fault(
        monkeypatch, scenario, _DURABLE_SOURCE_EDIT
    )
    with pytest.raises(_SimulatedProcessDeath):
        AuthoringPublisher(scenario.owner_state).publish(scenario.bundle)

    assert len(captured) == 1
    evidence = captured[0]
    assert _planned_after_is_published(scenario)
    edited_source_image = namespace_entry(scenario.source)
    assert edited_source_image.content == _DURABLE_SOURCE_EDIT
    _assert_active_evidence(scenario, evidence)
    project_at_crash = namespace_image(scenario.project)
    transaction_paths = (scenario.source, *scenario.destinations)
    assert _namespace_outside(
        project_at_crash,
        scenario.project,
        transaction_paths,
    ) == _namespace_outside(
        scenario.project_before,
        scenario.project,
        transaction_paths,
    )
    owner_at_crash = namespace_image(scenario.owner_state)
    assert _owner_without_evidence(
        scenario, owner_at_crash, evidence
    ) == scenario.owner_before

    with monkeypatch.context() as observer:
        events, durable = _install_retirement_observer(observer, evidence)
        AuthoringPublisher(scenario.owner_state).recover(scenario.project)

    assert namespace_image(scenario.project) == project_at_crash
    assert namespace_entry(scenario.source) == edited_source_image
    assert events[-2:] == ["unlink", "parent-fsync"]
    assert durable == [True]
    assert namespace_image(scenario.owner_state) == scenario.owner_before
    _assert_second_recovery_is_mutation_free(scenario, monkeypatch)


def test_source_drift_before_terminal_validation_rolls_back_all_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _prepare_scenario(tmp_path)
    edited_source = _DURABLE_SOURCE_EDIT
    progress = _install_source_drift_fault(monkeypatch, scenario, edited_source)
    with pytest.raises(_SimulatedProcessDeath):
        AuthoringPublisher(scenario.owner_state).publish(scenario.bundle)

    assert progress.injected
    assert progress.generation == progress.durable_generation
    assert _planned_after_is_published(scenario)
    edited_source_image = namespace_entry(scenario.source)
    assert edited_source_image.content == edited_source
    evidence = _assert_active_evidence(scenario)
    project_at_crash = namespace_image(scenario.project)
    transaction_paths = (scenario.source, *scenario.destinations)
    assert _namespace_outside(
        project_at_crash, scenario.project, transaction_paths
    ) == _namespace_outside(
        scenario.project_before, scenario.project, transaction_paths
    )
    owner_at_crash = namespace_image(scenario.owner_state)
    assert _owner_without_evidence(
        scenario, owner_at_crash, evidence
    ) == scenario.owner_before

    with monkeypatch.context() as observer:
        events, durable = _install_retirement_observer(observer, evidence)
        AuthoringPublisher(scenario.owner_state).recover(scenario.project)

    _assert_destinations_match_before(scenario)
    assert namespace_entry(scenario.source) == edited_source_image
    assert events[-2:] == ["unlink", "parent-fsync"]
    assert durable == [True]
    assert _namespace_outside(
        namespace_image(scenario.project), scenario.project, transaction_paths
    ) == _namespace_outside(
        scenario.project_before, scenario.project, transaction_paths
    )
    assert namespace_image(scenario.owner_state) == scenario.owner_before
    _assert_second_recovery_is_mutation_free(scenario, monkeypatch)
