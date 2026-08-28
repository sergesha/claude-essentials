"""Reachable foreign-destination freeze at the authoring mutation boundary."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

import lockstep.authoring_transaction as transaction
from lockstep.authoring import project_paths
from lockstep.authoring_bundle import ProjectCompilationBundle, plan_project_compilation
from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.errors import AuthoringError

from tests._authoring_crash_gate import (
    install_mutation_syscall_probe,
    namespace_entry,
    namespace_image,
    opaque_lock_identities,
)
from tests._authoring_gate import replace_marker, write_workflow


@dataclass(frozen=True, slots=True)
class _Scenario:
    project: Path
    owner: Path
    bundle: ProjectCompilationBundle
    destinations: tuple[Path, ...]
    before: tuple[object, ...]
    project_before: dict[str, object]
    owner_before: dict[str, object]
    sentinels: dict[Path, object]


def _scenario(tmp_path: Path, *, absent: bool) -> _Scenario:
    project = tmp_path / "project"
    source = write_workflow(project, "leaf", marker="old")
    source.chmod(0o640)
    owner = (tmp_path / "owner-state").resolve()
    owner.mkdir(mode=0o700)
    sentinel = project / "notes" / "sentinel.bin"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"sentinel\n")
    sentinel.chmod(0o640)
    if absent:
        (project / ".lockstep" / "recipes").mkdir(exist_ok=True)
        bundle = replace(plan_project_compilation(project_paths(project, "leaf")), sources=())
    else:
        initial = plan_project_compilation(project_paths(project, "leaf"))
        AuthoringPublisher(owner).publish(initial)
        replace_marker(source, "old", "new")
        source.chmod(0o640)
        bundle = plan_project_compilation(project_paths(project, "leaf"))
    destinations = tuple(image.resolved_path for image in bundle.after_images)
    assert len(destinations) == 3
    assert all((image.content is None) is absent for image in bundle.before_images)
    parent_sentinels: dict[Path, object] = {sentinel: namespace_entry(sentinel)}
    for index, parent in enumerate(sorted({path.parent for path in destinations})):
        sibling = parent / f"foreign-sentinel-{index}.bin"
        sibling.write_bytes(f"sibling-{index}\n".encode())
        sibling.chmod(0o600 if index % 2 else 0o640)
        parent_sentinels[sibling] = namespace_entry(sibling)
    return _Scenario(
        project, owner, bundle, destinations,
        tuple(namespace_entry(path) if path.exists() else None for path in destinations),
        namespace_image(project), namespace_image(owner), parent_sentinels,
    )


def _assert_prefix_restored(scenario: _Scenario, index: int) -> None:
    for candidate, expected in zip(scenario.destinations, scenario.before, strict=True):
        if candidate == scenario.destinations[index]:
            continue
        if expected is None:
            assert not candidate.exists()
            assert not candidate.is_symlink()
        else:
            observed = namespace_entry(candidate)
            assert (observed.kind, observed.content, observed.mode) == (
                expected.kind,
                expected.content,
                expected.mode,
            )
    assert {path: namespace_entry(path) for path in scenario.sentinels} == scenario.sentinels


def _assert_project_modulo_target(scenario: _Scenario, target: Path) -> None:
    """All non-transaction namespace facts remain exact; target is foreign."""

    before = dict(scenario.project_before)
    after = namespace_image(scenario.project)
    target_key = target.relative_to(scenario.project).as_posix()
    before.pop(target_key, None)
    after.pop(target_key, None)
    for destination in scenario.destinations:
        if destination == target:
            continue
        key = destination.relative_to(scenario.project).as_posix()
        before.pop(key, None)
        after.pop(key, None)
    assert after == before


@dataclass(slots=True)
class _RollbackDurabilityProbe:
    scenario: _Scenario
    active: bool = False
    target_index: int | None = None
    events: list[str] = field(default_factory=list)
    stage_paths: dict[tuple[tuple[int, int], str], tuple[int, tuple[int, int]]] = field(
        default_factory=dict
    )
    pending_files: dict[int, tuple[int, int]] = field(default_factory=dict)
    pending_parents: dict[str, tuple[int, int]] = field(default_factory=dict)
    journal_directory: tuple[int, int] | None = field(init=False, default=None)
    original_open: object = field(init=False)
    original_fsync: object = field(init=False)
    original_replace: object = field(init=False)
    original_unlink: object = field(init=False)
    original_create_journal: object = field(init=False)

    def activate(self, index: int) -> None:
        self.active, self.target_index = True, index

    def destination_index(self, value: object, directory_fd: int | None) -> int | None:
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

    def is_journal_path(self, value: object) -> bool:
        path = Path(os.fsdecode(value))
        if path.name != "transaction.json":
            return False
        if self.journal_directory is None:
            return False
        parent = path.parent.stat()
        return (parent.st_dev, parent.st_ino) == self.journal_directory

    def open_(self, path, flags, mode=0o777, *, dir_fd=None):
        descriptor = self.original_open(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_CREAT and flags & os.O_EXCL and dir_fd is not None:
            index = self.destination_index(path, dir_fd)
            if index is None:
                parent = os.fstat(dir_fd)
                info = os.fstat(descriptor)
                key = (parent.st_dev, parent.st_ino), os.fsdecode(path)
                self.stage_paths[key] = len(self.stage_paths), (info.st_dev, info.st_ino)
        return descriptor

    def fsync(self, descriptor: int) -> None:
        self.original_fsync(descriptor)
        if not self.active:
            return
        info = os.fstat(descriptor)
        identity = info.st_dev, info.st_ino
        for index, expected in tuple(self.pending_files.items()):
            if identity == expected:
                self.events.append(f"restore-file-fsync[{index}]")
                del self.pending_files[index]
        for label, parent in tuple(self.pending_parents.items()):
            if identity == parent:
                self.events.append(label)
                del self.pending_parents[label]

    def replace_(self, source, destination, *args, **kwargs):
        result = self.original_replace(source, destination, *args, **kwargs)
        index = self.destination_index(destination, kwargs.get("dst_dir_fd"))
        if self.active and index is not None and index < self.target_index:
            info = self.scenario.destinations[index].stat()
            self.events.append(f"restore-replace[{index}]")
            self.pending_files[index] = info.st_dev, info.st_ino
            parent = self.scenario.destinations[index].parent.stat()
            self.pending_parents[f"restore-parent-fsync[{index}]"] = (
                parent.st_dev,
                parent.st_ino,
            )
        return result

    def unlink(self, path, *args, **kwargs):
        directory_fd = kwargs.get("dir_fd")
        index = self.destination_index(path, directory_fd)
        parent_identity = None
        if directory_fd is not None:
            parent = os.fstat(directory_fd)
            parent_identity = parent.st_dev, parent.st_ino
        stage = (
            self.stage_paths.get((parent_identity, os.fsdecode(path)))
            if parent_identity is not None
            else None
        )
        if stage is not None:
            observed = os.stat(path, dir_fd=directory_fd, follow_symlinks=False)
            assert (observed.st_dev, observed.st_ino) == stage[1]
        journal = self.is_journal_path(path)
        result = self.original_unlink(path, *args, **kwargs)
        if not self.active:
            return result
        if index is not None and index < self.target_index:
            parent = self.scenario.destinations[index].parent.stat()
            self.events.append(f"remove-destination[{index}]")
            self.pending_parents[f"destination-parent-fsync[{index}]"] = (
                parent.st_dev,
                parent.st_ino,
            )
        elif stage is not None:
            ordinal, _identity = stage
            self.events.append(f"remove-stage[{ordinal}]")
            self.pending_parents[f"stage-parent-fsync[{ordinal}]"] = parent_identity
        if journal:
            self.events.append("journal-unlink")
            assert not self.pending_files and not self.pending_parents
        return result

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.original_open, self.original_fsync = os.open, os.fsync
        self.original_replace, self.original_unlink = os.replace, os.unlink
        self.original_create_journal = AuthoringJournal.create_for_bundle

        def create_for_bundle(cls, state_dir, bundle):
            journal = self.original_create_journal(state_dir, bundle)
            info = journal.directory.stat()
            self.journal_directory = info.st_dev, info.st_ino
            return journal

        monkeypatch.setattr(os, "open", self.open_)
        monkeypatch.setattr(os, "fsync", self.fsync)
        monkeypatch.setattr(os, "replace", self.replace_)
        monkeypatch.setattr(os, "unlink", self.unlink)
        monkeypatch.setattr(
            AuthoringJournal, "create_for_bundle", classmethod(create_for_bundle)
        )


def _install_rollback_durability_trace(
    monkeypatch: pytest.MonkeyPatch, scenario: _Scenario
) -> _RollbackDurabilityProbe:
    probe = _RollbackDurabilityProbe(scenario)
    probe.install(monkeypatch)
    return probe


def _assert_no_active_evidence(scenario: _Scenario) -> None:
    assert namespace_image(scenario.owner) == scenario.owner_before


def _assert_untouched_suffix(scenario: _Scenario, index: int) -> None:
    for suffix, expected in zip(
        scenario.destinations[index + 1:], scenario.before[index + 1:], strict=True
    ):
        if expected is None:
            assert not suffix.exists()
            assert not suffix.is_symlink()
        else:
            assert namespace_entry(suffix) == expected


def _assert_edit_rollback_durability(
    probe: _RollbackDurabilityProbe, index: int
) -> None:
    if not index:
        return
    journal = probe.events.index("journal-unlink")
    for ordinal in range(index):
        replace = f"restore-replace[{ordinal}]"
        file_fsync = f"restore-file-fsync[{ordinal}]"
        parent_fsync = f"restore-parent-fsync[{ordinal}]"
        assert probe.events.count(replace) == 1
        assert probe.events.count(file_fsync) == 1
        assert probe.events.count(parent_fsync) == 1
        assert probe.events.index(replace) < probe.events.index(file_fsync)
        assert probe.events.index(file_fsync) < probe.events.index(parent_fsync) < journal


def _assert_create_rollback_durability(
    probe: _RollbackDurabilityProbe, index: int
) -> None:
    journal = probe.events.index("journal-unlink")
    for ordinal in range(index):
        removal = f"remove-destination[{ordinal}]"
        parent_fsync = f"destination-parent-fsync[{ordinal}]"
        assert probe.events.count(removal) == 1
        assert probe.events.count(parent_fsync) == 1
        assert probe.events.index(removal) < probe.events.index(parent_fsync) < journal
    for removal in (event for event in probe.events if event.startswith("remove-stage[")):
        ordinal = removal.removeprefix("remove-stage[").removesuffix("]")
        parent_fsync = f"stage-parent-fsync[{ordinal}]"
        assert probe.events.count(removal) == 1
        assert probe.events.count(parent_fsync) == 1
        assert probe.events.index(removal) < probe.events.index(parent_fsync) < journal


@pytest.mark.parametrize("index", (0, 1, 2), ids=lambda index: f"edit[{index}]")
def test_foreign_edit_before_own_validation_preserves_leaf_and_restores_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    """A changed future existing leaf must never be claimed by this transaction."""

    scenario = _scenario(tmp_path, absent=False)
    target = scenario.destinations[index]
    original = transaction.validate_destination_before_at
    calls: list[tuple[int, str]] = []
    foreign = b"foreign existing leaf\n"
    injected_identity: list[object] = []
    trace = _install_rollback_durability_trace(monkeypatch, scenario)

    def mutate_then_validate(directory_fd: int, image) -> None:
        parent = os.fstat(directory_fd)
        if (
            image.resolved_path == target
            and not calls
            and (parent.st_dev, parent.st_ino)
            == (target.parent.stat().st_dev, target.parent.stat().st_ino)
        ):
            target.write_bytes(foreign)
            target.chmod(0o600)
            calls.append((index, image.resolved_path.name))
            injected_identity.append(namespace_entry(target))
            trace.activate(index)
        original(directory_fd, image)

    monkeypatch.setattr(transaction, "validate_destination_before_at", mutate_then_validate)
    with pytest.raises(AuthoringError):
        AuthoringPublisher(scenario.owner).publish(scenario.bundle)

    assert calls == [(index, target.name)]
    assert len(injected_identity) == 1
    foreign_identity = injected_identity[0]
    assert (foreign_identity.content, foreign_identity.mode) == (foreign, 0o600)
    _assert_prefix_restored(scenario, index)
    assert namespace_entry(target) == foreign_identity
    _assert_untouched_suffix(scenario, index)
    _assert_edit_rollback_durability(trace, index)
    _assert_project_modulo_target(scenario, target)
    _assert_no_active_evidence(scenario)
    snapshot = namespace_image(scenario.project), namespace_image(scenario.owner)
    AuthoringPublisher(scenario.owner).recover(scenario.project)
    assert (namespace_image(scenario.project), namespace_image(scenario.owner)) == snapshot


@pytest.mark.parametrize("index", (0, 1, 2), ids=lambda index: f"create[{index}]")
def test_foreign_create_at_real_no_clobber_edge_preserves_leaf_and_restores_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    """A foreign absent-before leaf created at ``link`` must win over publication."""

    scenario = _scenario(tmp_path, absent=True)
    target = scenario.destinations[index]
    original = os.link
    calls: list[tuple[int, str]] = []
    foreign = b"foreign created leaf\n"
    injected_identity: list[object] = []
    trace = _install_rollback_durability_trace(monkeypatch, scenario)

    def create_then_link(source, destination, *args, **kwargs):
        directory_fd = kwargs.get("dst_dir_fd")
        if (
            not calls
            and directory_fd is not None
            and os.fsdecode(destination) == target.name
            and (os.fstat(directory_fd).st_dev, os.fstat(directory_fd).st_ino)
            == (target.parent.stat().st_dev, target.parent.stat().st_ino)
        ):
            target.write_bytes(foreign)
            target.chmod(0o600)
            calls.append((index, target.name))
            injected_identity.append(namespace_entry(target))
            trace.activate(index)
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", create_then_link)
    with pytest.raises(AuthoringError):
        AuthoringPublisher(scenario.owner).publish(scenario.bundle)

    assert calls == [(index, target.name)]
    assert len(injected_identity) == 1
    foreign_identity = injected_identity[0]
    assert (foreign_identity.content, foreign_identity.mode) == (foreign, 0o600)
    _assert_prefix_restored(scenario, index)
    assert namespace_entry(target) == foreign_identity
    _assert_untouched_suffix(scenario, index)
    _assert_project_modulo_target(scenario, target)
    _assert_no_active_evidence(scenario)
    _assert_create_rollback_durability(trace, index)
