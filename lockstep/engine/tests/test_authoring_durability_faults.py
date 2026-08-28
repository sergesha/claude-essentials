"""Durability cuts for one real three-destination authoring transaction."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from lockstep.authoring import project_paths
from lockstep.authoring_bundle import ProjectCompilationBundle, plan_project_compilation
from lockstep.authoring_journal import AuthoringJournal
import lockstep.authoring_journal as authoring_journal
from lockstep.authoring_publisher import AuthoringPublisher

from tests._authoring_crash_gate import (
    install_mutation_syscall_probe,
    namespace_entry,
    namespace_image,
    opaque_lock_identities,
)
from tests._authoring_gate import replace_marker, write_workflow


class _ProcessDeath(BaseException):
    """One-shot crash after a real durability primitive has returned."""


@dataclass(frozen=True, slots=True)
class _Scenario:
    project: Path
    owner: Path
    bundle: ProjectCompilationBundle
    destinations: tuple[Path, ...]
    before: tuple[tuple[bytes, int], ...]
    after: tuple[tuple[bytes, int], ...]
    project_before: dict[str, object]
    owner_before: dict[str, object]


def _scenario(tmp_path: Path) -> _Scenario:
    project = tmp_path / "project"
    source = write_workflow(project, "leaf", marker="old")
    source.chmod(0o640)
    owner = (tmp_path / "owner-state").resolve()
    owner.mkdir(mode=0o700)
    sentinel = project / "notes" / "sentinel.bin"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"sentinel\n")
    sentinel.chmod(0o640)
    initial = plan_project_compilation(project_paths(project, "leaf"))
    AuthoringPublisher(owner).publish(initial)
    replace_marker(source, "old", "new")
    source.chmod(0o640)
    bundle = plan_project_compilation(project_paths(project, "leaf"))
    destinations = tuple(image.resolved_path for image in bundle.after_images)
    assert len(destinations) == 3
    before = tuple((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in destinations)
    after = tuple((image.content, image.mode) for image in bundle.after_images)
    assert all(content is not None and mode is not None for content, mode in after)
    return _Scenario(project, owner, bundle, destinations, before, after, namespace_image(project), namespace_image(owner))


_CUTS = (
    "journal_begin.temp_fsync", "journal_begin.replace", "journal_begin.parent_fsync",
    "stage_file_fsync[0]", "stage_file_fsync[1]", "stage_file_fsync[2]",
    "destination_file_fsync[0]", "destination_file_fsync[1]", "destination_file_fsync[2]",
    "destination_parent_fsync[0]", "destination_parent_fsync[1]", "destination_parent_fsync[2]",
    "journal_progress[0].temp_fsync", "journal_progress[0].replace", "journal_progress[0].parent_fsync",
    "journal_progress[1].temp_fsync", "journal_progress[1].replace", "journal_progress[1].parent_fsync",
    "journal_progress[2].temp_fsync", "journal_progress[2].replace", "journal_progress[2].parent_fsync",
    "rollback_journal_cleanup.before_unlink", "rollback_journal_cleanup.after_unlink_before_parent_fsync",
    "committed_cleanup.after_unlink_before_owner_parent_fsync",
)

_EXPECTED_BASELINE_EVENTS = (
    "journal_begin.temp_fsync", "journal_begin.replace", "journal_begin.parent_fsync",
    "stage_file_fsync[0]", "stage_file_fsync[1]", "stage_file_fsync[2]",
    "destination_replace[0]", "destination_file_fsync[0]", "destination_parent_fsync[0]", "journal_progress[0].temp_fsync", "journal_progress[0].replace", "journal_progress[0].parent_fsync",
    "destination_replace[1]", "destination_file_fsync[1]", "destination_parent_fsync[1]", "journal_progress[1].temp_fsync", "journal_progress[1].replace", "journal_progress[1].parent_fsync",
    "destination_replace[2]", "destination_file_fsync[2]", "destination_parent_fsync[2]", "journal_progress[2].temp_fsync", "journal_progress[2].replace", "journal_progress[2].parent_fsync",
    "committed.temp_fsync", "committed.replace", "committed.parent_fsync", "journal_unlink", "committed_cleanup.after_unlink_before_owner_parent_fsync", "committed_cleanup.owner_parent_fsync",
)

_BASELINE_EVENT_FOR_CUT = {
    **{cut: cut for cut in _CUTS if not cut.startswith("rollback_")},
}


def _image(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _assert_project_side(
    scenario: _Scenario, expected: tuple[tuple[bytes, int], ...]
) -> None:
    """Keep the full project namespace exact outside transaction-owned leaves."""

    before = dict(scenario.project_before)
    after = namespace_image(scenario.project)
    for destination, wanted in zip(scenario.destinations, expected, strict=True):
        key = destination.relative_to(scenario.project).as_posix()
        before.pop(key, None)
        after.pop(key, None)
        assert _image(destination) == wanted
    assert after == before


def _assert_second_recovery_is_write_free(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_before = namespace_image(scenario.project)
    owner_before = namespace_image(scenario.owner)
    allowed = opaque_lock_identities({}, owner_before)
    with monkeypatch.context() as probe:
        calls = install_mutation_syscall_probe(
            probe, allowed_write_open_identities=allowed
        )
        AuthoringPublisher(scenario.owner).recover(scenario.project)
    assert calls == []
    assert namespace_image(scenario.project) == project_before
    assert namespace_image(scenario.owner) == owner_before


class _DurabilityProtocolProbe:
    """One real syscall trace; optionally dies at one named semantic cut."""

    def __init__(self, scenario: _Scenario, cut: str | None = None) -> None:
        self.scenario, self.cut = scenario, cut
        self.original_open, self.original_fsync = os.open, os.fsync
        self.original_replace, self.original_unlink = os.replace, os.unlink
        self.original_mkstemp = authoring_journal.tempfile.mkstemp
        self.original_create_journal = AuthoringJournal.create_for_bundle
        self.journal_directory: tuple[int, int] | None = None
        self.stage_descriptors: dict[int, int] = {}
        self.journal_temp_descriptors: dict[int, int] = {}
        self.stage_count = 0
        self.published: dict[tuple[int, int], int] = {}
        self.journal_parent: tuple[int, int] | None = None
        self.pending_destination_parent: int | None = None
        self.journal_cycle = 0
        self.pending_owner_fsync = False
        self.journal_was_unlinked = False
        self.rollback_triggered = False
        self.injected = False
        self.events: list[str] = []

    @property
    def rollback_requested(self) -> bool:
        return bool(self.cut and self.cut.startswith("rollback_journal_cleanup"))

    def index_at(self, value: object, directory_fd: int | None) -> int | None:
        if directory_fd is None:
            return None
        parent, leaf = os.fstat(directory_fd), os.fsdecode(value)
        return next((i for i, path in enumerate(self.scenario.destinations) if path.name == leaf and (path.parent.stat().st_dev, path.parent.stat().st_ino) == (parent.st_dev, parent.st_ino)), None)

    def open_(self, path, flags, mode=0o777, *, dir_fd=None):
        descriptor = self.original_open(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_CREAT and flags & os.O_EXCL and dir_fd is not None:
            if self.index_at(path, dir_fd) is None:
                self.stage_descriptors[descriptor] = self.stage_count
                self.stage_count += 1
        return descriptor

    def mkstemp(self, *args, **kwargs):
        descriptor, raw_path = self.original_mkstemp(*args, **kwargs)
        directory = Path(kwargs["dir"])
        created = Path(raw_path)
        expected = directory.stat()
        observed = created.parent.stat()
        assert self.journal_directory is not None
        assert (expected.st_dev, expected.st_ino) == self.journal_directory
        assert (observed.st_dev, observed.st_ino) == self.journal_directory
        self.journal_temp_descriptors[descriptor] = len(self.journal_temp_descriptors) + self.journal_cycle + 1
        return descriptor, raw_path

    def hit(self, label: str) -> None:
        self.events.append(label)
        if self.cut == label and not self.injected:
            self.injected = True
            raise _ProcessDeath(label)

    def journal_label(self, action: str, cycle: int) -> str:
        if cycle == 1:
            return f"journal_begin.{action}"
        if cycle == 5:
            return f"committed.{action}"
        return f"journal_progress[{cycle - 2}].{action}"

    def fsync(self, descriptor: int) -> None:
        self.original_fsync(descriptor)
        info = os.fstat(descriptor)
        identity = info.st_dev, info.st_ino
        if (cycle := self.journal_temp_descriptors.pop(descriptor, None)) is not None:
            self.hit(self.journal_label("temp_fsync", cycle))
        elif identity in self.published:
            self.hit(f"destination_file_fsync[{self.published[identity]}]")
        elif (stage := self.stage_descriptors.pop(descriptor, None)) is not None:
            self.hit(f"stage_file_fsync[{stage}]")
        elif self.journal_parent == identity and self.pending_owner_fsync:
            self.pending_owner_fsync = False
            if self.journal_was_unlinked:
                self.hit("committed_cleanup.owner_parent_fsync")
            else:
                self.hit(self.journal_label("parent_fsync", self.journal_cycle))
        elif self.pending_destination_parent is not None:
            parent = self.scenario.destinations[self.pending_destination_parent].parent.stat()
            if identity == (parent.st_dev, parent.st_ino):
                self.hit(f"destination_parent_fsync[{self.pending_destination_parent}]")
                self.pending_destination_parent = None

    def replace_(self, source, destination, *args, **kwargs):
        result = self.original_replace(source, destination, *args, **kwargs)
        index = self.index_at(destination, kwargs.get("dst_dir_fd"))
        if index is not None:
            info = self.scenario.destinations[index].stat()
            self.published[(info.st_dev, info.st_ino)] = index
            self.pending_destination_parent = index
            self.hit(f"destination_replace[{index}]")
            if self.rollback_requested and index == 0 and not self.rollback_triggered:
                self.rollback_triggered = True
                raise OSError("force caught rollback before journal cleanup")
        elif self.is_journal_path(destination):
            self.journal_parent = self.journal_directory
            self.journal_cycle += 1
            self.pending_owner_fsync = True
            self.hit(self.journal_label("replace", self.journal_cycle))
        return result

    def unlink(self, path, *args, **kwargs):
        if self.is_journal_path(path) and self.rollback_requested:
            self.hit("rollback_journal_cleanup.before_unlink")
        result = self.original_unlink(path, *args, **kwargs)
        if self.is_journal_path(path):
            self.events.append("journal_unlink")
            self.journal_was_unlinked = True
            self.pending_owner_fsync = True
            self.hit("rollback_journal_cleanup.after_unlink_before_parent_fsync" if self.rollback_requested else "committed_cleanup.after_unlink_before_owner_parent_fsync")
        return result

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def create_for_bundle(cls, state_dir, bundle):
            journal = self.original_create_journal(state_dir, bundle)
            info = journal.directory.stat()
            self.journal_directory = info.st_dev, info.st_ino
            return journal

        monkeypatch.setattr(os, "open", self.open_)
        monkeypatch.setattr(os, "fsync", self.fsync)
        monkeypatch.setattr(os, "replace", self.replace_)
        monkeypatch.setattr(os, "unlink", self.unlink)
        monkeypatch.setattr(authoring_journal.tempfile, "mkstemp", self.mkstemp)
        monkeypatch.setattr(
            AuthoringJournal, "create_for_bundle", classmethod(create_for_bundle)
        )

    def is_journal_path(self, value: object) -> bool:
        candidate = Path(os.fsdecode(value))
        if candidate.name != "transaction.json":
            return False
        if self.journal_directory is None:
            return False
        parent = candidate.parent.stat()
        return (parent.st_dev, parent.st_ino) == self.journal_directory

def _run_durable_cut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: str) -> None:
    scenario = _scenario(tmp_path)
    probe = _DurabilityProtocolProbe(scenario, cut)
    probe.install(monkeypatch)
    with pytest.raises(_ProcessDeath):
        AuthoringPublisher(scenario.owner).publish(scenario.bundle)
    assert probe.injected, f"{cut} was not reached; trace={probe.events}"
    monkeypatch.undo()
    AuthoringPublisher(scenario.owner).recover(scenario.project)
    _assert_project_side(scenario, scenario.after if cut.startswith("committed") else scenario.before)
    assert namespace_image(scenario.owner) == scenario.owner_before
    _assert_second_recovery_is_write_free(scenario, monkeypatch)


def test_unfaulted_three_destination_protocol_trace_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _scenario(tmp_path)
    probe = _DurabilityProtocolProbe(scenario)
    probe.install(monkeypatch)
    AuthoringPublisher(scenario.owner).publish(scenario.bundle)
    assert tuple(probe.events) == _EXPECTED_BASELINE_EVENTS
    for cut, event in _BASELINE_EVENT_FOR_CUT.items():
        assert _EXPECTED_BASELINE_EVENTS.count(event) == 1, cut


@pytest.mark.parametrize("cut", _CUTS)
def test_durable_authoring_cut_recovers_to_the_recorded_transaction_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    _run_durable_cut(tmp_path, monkeypatch, cut)
