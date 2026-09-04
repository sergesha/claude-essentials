"""Descriptor-relative ownership, collision, and source-currentness controls."""
from __future__ import annotations

import os, stat
from dataclasses import dataclass
from pathlib import Path

import pytest

import lockstep.authoring_publisher as publisher
from lockstep import authoring
from lockstep.authoring_bundle import AuthoringPlan
from lockstep.authoring_compilation import plan_project_compilation
from lockstep.errors import AuthoringError
from tests._authoring_gate import replace_marker, tree_image, write_workflow


@dataclass(frozen=True)
class Scenario:
    project: Path; state: Path; source: Path; plan: AuthoringPlan
    @property
    def targets(self): return tuple(item.path for item in self.plan.targets)


def _scenario(tmp_path: Path, *, present: bool) -> Scenario:
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / "state").resolve()
    source = write_workflow(project, "release")
    if present:
        authoring.publish_project_compilation(project, "release", state_dir=state)
        replace_marker(source, "initial", "changed")
    return Scenario(project, state, source, plan_project_compilation(authoring.project_paths(project, "release")))


def _exact(path: Path, target, *, before: bool = False) -> bool:
    content = target.before if before else target.after
    mode = stat.S_IMODE(target.before_file.mode) if before else target.mode
    return path.is_file() and not path.is_symlink() and path.read_bytes() == content and stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize("fault", ("write", "fchmod", "fsync"))
def test_live_owned_temporary_is_cleaned_after_initialization_fault(tmp_path, monkeypatch, fault) -> None:
    scenario = _scenario(tmp_path, present=False); target = scenario.targets[0]; original = getattr(os, fault)
    def fail(*args, **kwargs): raise OSError("fault")
    monkeypatch.setattr(os, fault, fail)
    with pytest.raises(OSError, match="fault"): publisher._publish_per_file(scenario.plan)
    monkeypatch.setattr(os, fault, original)
    assert not tuple(target.parent.glob(".lockstep-authoring-*.tmp")) and not target.exists()


def test_temporary_creation_is_exclusive_nofollow_and_occupied_name_is_preserved(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, present=False); target = scenario.targets[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    scenario = Scenario(scenario.project, scenario.state, scenario.source, plan_project_compilation(authoring.project_paths(scenario.project, "release"))); target = scenario.targets[0]
    foreign = target.parent / (".lockstep-authoring-" + "a" * 32 + ".tmp")
    foreign.write_bytes(b"foreign\n"); calls = []; original = os.open
    monkeypatch.setattr(publisher.secrets, "token_hex", lambda _n: "a" * 32)
    def observe(path, flags, *args, **kwargs):
        if os.fsdecode(path) == foreign.name: calls.append(flags)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", observe)
    with pytest.raises(AuthoringError, match="temporary already exists"): publisher._publish_per_file(scenario.plan)
    assert calls and all(flags & os.O_CREAT and flags & os.O_EXCL and flags & getattr(os, "O_NOFOLLOW", 0) for flags in calls)
    assert foreign.read_bytes() == b"foreign\n"


def test_swapped_temporary_inode_is_never_deleted_as_owned(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, present=False); original = publisher._prove_owned_temporary; foreign = b"swapped\n"; observed = []
    def swap(parent, leaf, owned, after):
        os.unlink(leaf, dir_fd=parent); descriptor = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        os.write(descriptor, foreign); os.close(descriptor); observed.append(after.path.parent / leaf)
        return original(parent, leaf, owned, after)
    monkeypatch.setattr(publisher, "_prove_owned_temporary", swap)
    with pytest.raises(AuthoringError): publisher._publish_per_file(scenario.plan)
    assert len(observed) == 1 and observed[0].read_bytes() == foreign


def test_absent_target_no_clobber_preserves_foreign_destination(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, present=False); target = scenario.targets[0]; original = os.link; foreign = b"foreign\n"
    def race(source, destination, *args, **kwargs):
        target.write_bytes(foreign); return original(source, destination, *args, **kwargs)
    monkeypatch.setattr(os, "link", race)
    with pytest.raises(AuthoringError, match="created before publication"): publisher._publish_per_file(scenario.plan)
    assert target.read_bytes() == foreign


def test_exact_before_edit_rejects_without_restoring_earlier_target(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, present=True); first, second = scenario.targets[:2]; foreign = b"foreign\n"
    original = publisher.validate_target_at; calls = 0
    def validate(parent, before):
        nonlocal calls
        if calls == 1: second.write_bytes(foreign)
        calls += 1; return original(parent, before)
    monkeypatch.setattr(publisher, "validate_target_at", validate)
    with pytest.raises(AuthoringError): publisher._publish_per_file(scenario.plan)
    assert _exact(first, scenario.plan.targets[0]) and second.read_bytes() == foreign


def test_ordinary_link_error_preserves_completed_prefix_without_rollback(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, present=False); original = os.link; calls = 0
    def link(*args, **kwargs):
        nonlocal calls
        if calls == 1: raise OSError("ordinary link failure")
        calls += 1; return original(*args, **kwargs)
    monkeypatch.setattr(os, "link", link)
    with pytest.raises(OSError, match="ordinary link failure"): publisher._publish_per_file(scenario.plan)
    assert _exact(scenario.targets[0], scenario.plan.targets[0]) and not scenario.targets[1].exists()


@pytest.mark.parametrize("kind", ("symlink", "directory", "fifo"))
def test_nonregular_or_linked_destination_is_rejected_before_mutation(tmp_path, kind) -> None:
    scenario = _scenario(tmp_path, present=False); target = scenario.targets[-1]; target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink": target.symlink_to(scenario.source)
    elif kind == "directory": target.mkdir()
    else: os.mkfifo(target)
    before = tree_image(scenario.project)
    with pytest.raises(AuthoringError): publisher._publish_per_file(scenario.plan)
    assert tree_image(scenario.project) == before


def test_destination_parent_swap_cannot_escape_project(tmp_path) -> None:
    scenario = _scenario(tmp_path, present=False); parent = scenario.targets[0].parent; outside = tmp_path / "outside"; outside.mkdir()
    parent.mkdir(parents=True, exist_ok=True); parent.rmdir(); parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AuthoringError): publisher._publish_per_file(scenario.plan)
    assert not tuple(outside.iterdir())


@pytest.mark.parametrize("race", ("empty-replacement", "foreign-child"))
def test_created_parent_race_refuses_foreign_replacement_or_child(tmp_path, monkeypatch, race) -> None:
    scenario = _scenario(tmp_path, present=False); parent = scenario.targets[0].parent; original_open, original_mkdir = os.open, os.mkdir; injected = []; foreign_directory = []
    def open_directory(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if os.fsdecode(path) == parent.name and not injected:
            injected.append(True)
            if race == "empty-replacement":
                os.rmdir(path, dir_fd=kwargs.get("dir_fd")); original_mkdir(path, dir_fd=kwargs.get("dir_fd"))
                info = parent.stat(); foreign_directory.append(((info.st_dev, info.st_ino), tree_image(parent)))
            else: (parent / "foreign.txt").write_bytes(b"foreign\n")
        return descriptor
    monkeypatch.setattr(os, "open", open_directory)
    with pytest.raises(AuthoringError, match="foreign|ownership|created"):
        publisher._publish_per_file(scenario.plan)
    assert injected == [True] and all(not path.exists() for path in scenario.targets)
    if race == "empty-replacement":
        info = parent.stat(); assert (info.st_dev, info.st_ino) == foreign_directory[0][0] and tree_image(parent) == foreign_directory[0][1]
    else: assert (parent / "foreign.txt").read_bytes() == b"foreign\n"


def test_each_target_fsync_precedes_its_parent_directory_fsync(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, present=False); events = []; original_target, original_fsync = publisher._fsync_regular_at, os.fsync
    def target(parent, leaf): original_target(parent, leaf); events.append(("target", leaf))
    def fsync(descriptor):
        result = original_fsync(descriptor)
        if stat.S_ISDIR(os.fstat(descriptor).st_mode): events.append(("parent", None))
        return result
    monkeypatch.setattr(publisher, "_fsync_regular_at", target); monkeypatch.setattr(os, "fsync", fsync)
    publisher._publish_per_file(scenario.plan)
    relevant = events[-2 * len(scenario.targets):]
    assert relevant == [item for target_path in scenario.targets for item in (("target", target_path.name), ("parent", None))]


@pytest.mark.parametrize("when", ("before", "between", "terminal"))
def test_source_currentness_rejects_without_rolling_back_completed_prefix(tmp_path, monkeypatch, when) -> None:
    scenario = _scenario(tmp_path, present=True); original = publisher._publish_target; calls = 0
    if when == "before": scenario.source.write_bytes(b"foreign source\n")
    def publish_target(*args, **kwargs):
        nonlocal calls; result = original(*args, **kwargs); calls += 1
        if (when == "between" and calls == 1) or (when == "terminal" and calls == len(scenario.targets)):
            scenario.source.write_bytes(b"foreign source\n")
        return result
    monkeypatch.setattr(publisher, "_publish_target", publish_target)
    with pytest.raises(AuthoringError): publisher._publish_per_file(scenario.plan)
    if when == "before": assert all(_exact(target.path, target, before=True) for target in scenario.plan.targets)
    else: assert _exact(scenario.targets[0], scenario.plan.targets[0])
