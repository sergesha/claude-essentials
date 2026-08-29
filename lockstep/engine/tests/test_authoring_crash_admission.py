"""Direct crash-cut evidence for the private bounded per-file writer."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

import lockstep.authoring_publisher as publisher
from lockstep.authoring import project_paths
from lockstep.authoring_bundle import ProjectCompilationBundle, plan_project_compilation
from lockstep.authoring_project_tree import AuthoringProjectTree
from lockstep.errors import AuthoringError
from tests._authoring_crash_gate import NamespaceEntry, namespace_entry
from tests._authoring_gate import replace_marker, write_workflow
from tests._authoring_writer_gate import SimulatedProcessDeath


@dataclass(frozen=True, slots=True)
class _Scenario:
    project: Path
    bundle: ProjectCompilationBundle
    before: tuple[NamespaceEntry | None, ...]

    @property
    def targets(self) -> tuple[Path, ...]:
        return tuple(image.resolved_path for image in self.bundle.after_images)


def _scenario(tmp_path: Path, *, absent: bool) -> _Scenario:
    project = tmp_path / "project"
    source = write_workflow(project, "release", marker="old")
    source.chmod(0o640)
    if not absent:
        initial = plan_project_compilation(project_paths(project, "release"))
        publisher.AuthoringPublisher((tmp_path / "owner").resolve()).publish(initial)
        replace_marker(source, "old", "new")
        source.chmod(0o640)
    bundle = plan_project_compilation(project_paths(project, "release"))
    targets = tuple(image.resolved_path for image in bundle.after_images)
    assert len(targets) == 3
    assert all((image.content is None) is absent for image in bundle.before_images)
    return _Scenario(
        project,
        bundle,
        tuple(namespace_entry(path) if path.exists() else None for path in targets),
    )


def _raise_cut() -> None:
    raise SimulatedProcessDeath


def _install_cut(
    monkeypatch: pytest.MonkeyPatch,
    scenario: _Scenario,
    phase: str,
    cut_index: int,
) -> None:
    target = scenario.targets[cut_index]
    if phase == "before-preflight":
        monkeypatch.setattr(
            publisher,
            "validate_bundle_preconditions",
            lambda _bundle: _raise_cut(),
        )
        return
    if phase == "after-parent-creation":
        original = AuthoringProjectTree.ensure_target_parents

        def create_then_cut(tree: AuthoringProjectTree) -> None:
            original(tree)
            _raise_cut()

        monkeypatch.setattr(
            AuthoringProjectTree, "ensure_target_parents", create_then_cut
        )
        return
    if phase == "after-temporary-fsync":
        monkeypatch.setattr(
            publisher,
            "_validate_temporary_descriptor",
            lambda _descriptor, _after: _raise_cut(),
        )
        return
    if phase == "after-mutation":
        name = "link" if scenario.bundle.before_images[0].content is None else "replace"
        original = getattr(os, name)

        def mutate_then_cut(source, destination, *args, **kwargs):
            result = original(source, destination, *args, **kwargs)
            directory_fd = kwargs.get("dst_dir_fd")
            if directory_fd is not None and os.fsdecode(destination) == target.name:
                _raise_cut()
            return result

        monkeypatch.setattr(os, name, mutate_then_cut)
        return
    if phase == "after-target-fsync":
        def fsync_then_cut(directory_descriptor: int, leaf: str) -> None:
            descriptor = os.open(
                leaf,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if leaf == target.name:
                _raise_cut()

        monkeypatch.setattr(
            publisher, "_fsync_regular_at", fsync_then_cut, raising=False
        )
        return
    if phase == "after-parent-fsync":
        original_target_fsync = publisher._fsync_regular_at
        original_fsync = os.fsync
        target_synced = False

        def observe_target_fsync(directory_descriptor: int, leaf: str) -> None:
            nonlocal target_synced
            original_target_fsync(directory_descriptor, leaf)
            if leaf == target.name:
                target_synced = True

        def fsync_parent_then_cut(descriptor: int) -> None:
            original_fsync(descriptor)
            info = os.fstat(descriptor)
            expected = target.parent.stat()
            if target_synced and stat.S_ISDIR(info.st_mode) and (
                info.st_dev,
                info.st_ino,
            ) == (expected.st_dev, expected.st_ino):
                _raise_cut()

        monkeypatch.setattr(
            publisher,
            "_fsync_regular_at",
            observe_target_fsync,
        )
        monkeypatch.setattr(os, "fsync", fsync_parent_then_cut)
        return
    raise AssertionError(f"unknown cut: {phase}")


@pytest.mark.parametrize("absent", (True, False), ids=("link", "replace"))
@pytest.mark.parametrize(
    ("phase", "cut_index"),
    (
        ("before-preflight", 0),
        ("after-parent-creation", 0),
        ("after-temporary-fsync", 0),
        *((phase, index) for phase in (
            "after-mutation",
            "after-target-fsync",
            "after-parent-fsync",
        ) for index in range(3)),
    ),
)
def test_private_writer_cut_keeps_only_completed_per_file_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    absent: bool,
    phase: str,
    cut_index: int,
) -> None:
    """Removing rollback must leave prior files/directories and no later target."""

    scenario = _scenario(tmp_path, absent=absent)
    _install_cut(monkeypatch, scenario, phase, cut_index)

    with pytest.raises(SimulatedProcessDeath):
        publisher._publish_per_file(scenario.bundle)

    first_is_new = phase in {
        "after-mutation",
        "after-target-fsync",
        "after-parent-fsync",
    }
    for index, (path, before, after) in enumerate(
        zip(
            scenario.targets,
            scenario.before,
            scenario.bundle.after_images,
            strict=True,
        )
    ):
        observed = namespace_entry(path) if path.exists() else None
        if first_is_new and index <= cut_index:
            assert observed is not None
            assert (observed.kind, observed.content, observed.mode) == (
                "regular",
                after.content,
                after.mode,
            )
        else:
            assert observed == before
    if phase != "before-preflight":
        assert all(path.parent.is_dir() for path in scenario.targets)


def test_private_writer_publishes_each_target_as_an_exact_regular_after_image(
    tmp_path: Path,
) -> None:
    """A wrong mutation primitive, content, mode, or final verification must fail."""

    scenario = _scenario(tmp_path, absent=True)

    publisher._publish_per_file(scenario.bundle)

    for target, after in zip(
        scenario.targets, scenario.bundle.after_images, strict=True
    ):
        observed = namespace_entry(target)
        assert (observed.kind, observed.content, observed.mode) == (
            "regular",
            after.content,
            after.mode,
        )


def test_private_writer_final_verification_rejects_an_earlier_target_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting final whole-bundle verification would accept stale earlier bytes."""

    scenario = _scenario(tmp_path, absent=True)
    first, last = scenario.targets[0], scenario.targets[-1]
    original = publisher.capture_after_identity_at
    foreign = b"changed after immediate target verification\n"
    tampered = False

    def capture_then_tamper(directory_descriptor: int, after):
        nonlocal tampered
        observed = original(directory_descriptor, after)
        if after.resolved_path == last and not tampered:
            first.write_bytes(foreign)
            first.chmod(0o600)
            tampered = True
        return observed

    monkeypatch.setattr(
        publisher, "capture_after_identity_at", capture_then_tamper
    )

    with pytest.raises(AuthoringError):
        publisher._publish_per_file(scenario.bundle)

    assert tampered
    assert (namespace_entry(first).content, namespace_entry(first).mode) == (
        foreign,
        0o600,
    )
    for target, after in zip(
        scenario.targets[1:], scenario.bundle.after_images[1:], strict=True
    ):
        observed = namespace_entry(target)
        assert (observed.content, observed.mode) == (after.content, after.mode)
