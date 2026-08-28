"""Fail-closed recovery when a crashed destination is later replaced externally."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import pytest

from lockstep.authoring import project_paths
from lockstep.authoring_bundle import ProjectCompilationBundle, plan_project_compilation
from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.errors import AuthoringError

from tests._authoring_crash_gate import (
    NamespaceEntry,
    install_mutation_syscall_probe,
    namespace_entry,
    namespace_image,
    opaque_lock_identities,
)
from tests._authoring_gate import replace_marker, write_workflow


class _ProcessDeath(BaseException):
    """Escape the publisher's caught rollback path after a durable event."""


Role = Literal["existing", "absent"]


@dataclass(frozen=True, slots=True)
class _Scenario:
    role: Role
    project: Path
    owner: Path
    bundle: ProjectCompilationBundle
    destinations: tuple[Path, ...]


@dataclass(slots=True)
class _DestinationParentFsyncCut:
    scenario: _Scenario
    ordinal: int
    primitive_name: str
    mutation_ordinals: list[int]
    awaiting_parent_fsync: bool = False
    injected: bool = False

    def destination_ordinal(self, value: object, directory_fd: int | None) -> int | None:
        if directory_fd is None:
            return None
        parent = os.fstat(directory_fd)
        leaf = os.fsdecode(value)
        return next(
            (
                index
                for index, path in enumerate(self.scenario.destinations)
                if path.name == leaf
                and (path.parent.stat().st_dev, path.parent.stat().st_ino)
                == (parent.st_dev, parent.st_ino)
            ),
            None,
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original_mutation = getattr(os, self.primitive_name)
        original_fsync = os.fsync

        def mutation(source, destination, *args, **kwargs):
            result = original_mutation(source, destination, *args, **kwargs)
            ordinal = self.destination_ordinal(destination, kwargs.get("dst_dir_fd"))
            if ordinal is not None:
                self.mutation_ordinals.append(ordinal)
                self.awaiting_parent_fsync = ordinal == self.ordinal
            return result

        def fsync(descriptor: int) -> None:
            original_fsync(descriptor)
            if not self.awaiting_parent_fsync or self.injected:
                return
            expected = self.scenario.destinations[self.ordinal].parent.stat()
            observed = os.fstat(descriptor)
            if (observed.st_dev, observed.st_ino) == (
                expected.st_dev,
                expected.st_ino,
            ):
                self.injected = True
                raise _ProcessDeath(
                    f"after durable {self.primitive_name} destination ordinal "
                    f"{self.ordinal}"
                )

        monkeypatch.setattr(os, self.primitive_name, mutation)
        monkeypatch.setattr(os, "fsync", fsync)


def _scenario(tmp_path: Path, role: Role) -> _Scenario:
    project = tmp_path / "project"
    source = write_workflow(project, "leaf", marker="old")
    source.chmod(0o640)
    owner = (tmp_path / "owner-state").resolve()
    owner.mkdir(mode=0o700)
    (project / "notes").mkdir()
    (project / "notes" / "sentinel.bin").write_bytes(b"project sentinel\n")
    if role == "existing":
        AuthoringPublisher(owner).publish(
            plan_project_compilation(project_paths(project, "leaf"))
        )
        replace_marker(source, "old", "new")
        source.chmod(0o640)
    bundle = plan_project_compilation(project_paths(project, "leaf"))
    destinations = tuple(image.resolved_path for image in bundle.after_images)
    assert len(destinations) == 3
    assert all(
        image.content is not None and image.mode is not None
        for image in bundle.after_images
    )
    assert all(
        (image.content is not None) is (role == "existing")
        for image in bundle.before_images
    )
    return _Scenario(role, project, owner, bundle, destinations)


def _crash_after_destination_parent_fsync(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch, ordinal: int
) -> None:
    primitive_name = "replace" if scenario.role == "existing" else "link"
    cut = _DestinationParentFsyncCut(scenario, ordinal, primitive_name, [])
    with monkeypatch.context() as crash_patch:
        cut.install(crash_patch)
        with pytest.raises(_ProcessDeath, match=rf"ordinal {ordinal}"):
            AuthoringPublisher(scenario.owner).publish(scenario.bundle)
    assert cut.injected
    assert cut.mutation_ordinals == list(range(ordinal + 1))


def _crash_after_record_committed(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = AuthoringJournal.record_committed
    called = False

    def record_then_die(journal: AuthoringJournal) -> None:
        nonlocal called
        original(journal)
        called = True
        raise _ProcessDeath("after durable committed journal generation")

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(AuthoringJournal, "record_committed", record_then_die)
        with pytest.raises(_ProcessDeath, match="durable committed"):
            AuthoringPublisher(scenario.owner).publish(scenario.bundle)
    assert called


def _assert_crash_journal_phase(
    scenario: _Scenario, *, committed: bool, replacement_progress: tuple[int, ...]
) -> None:
    """Read trusted active evidence under its existing persistent lock only."""

    project_before = namespace_image(scenario.project)
    owner_before = namespace_image(scenario.owner)
    journal, project_identity = AuthoringJournal.locate_for_project(
        scenario.owner, scenario.project
    )
    assert journal is not None
    with journal.locked_existing():
        model = journal.read_recovery_model(expected_project=project_identity)
    assert model.committed is committed
    assert model.replacement_progress == replacement_progress
    assert namespace_image(scenario.project) == project_before
    assert namespace_image(scenario.owner) == owner_before


def _install_durable_foreign(target: Path, *, label: str) -> NamespaceEntry:
    temporary = target.with_name(f".{target.name}.{label}.foreign")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, f"foreign {label}\n".encode())
        os.fchmod(descriptor, 0o601)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, target)
    parent = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return namespace_entry(target)


def _active_journal(owner: Path) -> tuple[Path, NamespaceEntry]:
    journals = tuple(owner.rglob("transaction.json"))
    assert len(journals) == 1
    journal = journals[0]
    observed = namespace_entry(journal)
    metadata = journal.lstat()
    assert observed.kind == "regular"
    assert observed.mode == 0o600
    assert observed.content
    assert metadata.st_uid == os.getuid()
    return journal, observed


def _assert_fail_closed_twice(
    scenario: _Scenario,
    target: Path,
    foreign: NamespaceEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_before = namespace_image(scenario.project)
    owner_before = namespace_image(scenario.owner)
    journal, journal_before = _active_journal(scenario.owner)
    allowed_locks = opaque_lock_identities({}, owner_before)
    assert len(allowed_locks) == 1
    with monkeypatch.context() as probe:
        calls = install_mutation_syscall_probe(
            probe,
            allowed_write_open_identities=allowed_locks,
            record_fsync=True,
        )
        for _attempt in range(2):
            with pytest.raises(AuthoringError, match=re.escape(str(target))):
                AuthoringPublisher(scenario.owner).recover(scenario.project)
    assert calls == []
    assert namespace_entry(target) == foreign
    assert namespace_entry(journal) == journal_before
    assert namespace_image(scenario.project) == project_before
    assert namespace_image(scenario.owner) == owner_before


@pytest.mark.parametrize(
    ("role", "crash_ordinal", "target_ordinal"),
    (
        ("existing", 0, 2),
        ("existing", 1, 1),
        ("existing", 2, 0),
        ("absent", 0, 1),
        ("absent", 1, 0),
        ("absent", 2, 2),
    ),
    ids=(
        "replace-crash0-target2",
        "replace-crash1-target1",
        "replace-crash2-target0",
        "link-crash0-target1",
        "link-crash1-target0",
        "link-crash2-target2",
    ),
)
def test_uncommitted_foreign_destination_recovery_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: Role,
    crash_ordinal: int,
    target_ordinal: int,
) -> None:
    """A foreign leaf must fail before reverse/forward recovery can mutate."""

    scenario = _scenario(tmp_path, role)
    _crash_after_destination_parent_fsync(scenario, monkeypatch, crash_ordinal)
    _assert_crash_journal_phase(
        scenario,
        committed=False,
        replacement_progress=tuple(range(crash_ordinal)),
    )
    target = scenario.destinations[target_ordinal]
    foreign = _install_durable_foreign(
        target, label=f"{role}-{crash_ordinal}-{target_ordinal}"
    )
    _assert_fail_closed_twice(scenario, target, foreign, monkeypatch)


@pytest.mark.parametrize("target_ordinal", (0, 1, 2))
def test_committed_existing_foreign_destination_recovery_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_ordinal: int
) -> None:
    """A committed journal cannot retire evidence after its after-image drifts."""

    scenario = _scenario(tmp_path, "existing")
    _crash_after_record_committed(scenario, monkeypatch)
    _assert_crash_journal_phase(
        scenario, committed=True, replacement_progress=(0, 1, 2)
    )
    target = scenario.destinations[target_ordinal]
    foreign = _install_durable_foreign(target, label=f"committed-{target_ordinal}")
    _assert_fail_closed_twice(scenario, target, foreign, monkeypatch)
