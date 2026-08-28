"""Public authoring publisher contract."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from lockstep.authoring import project_paths
from lockstep.authoring_bundle import (
    ProjectCompilationBundle,
    plan_project_compilation,
)
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.errors import AuthoringError

from tests._authoring_gate import TreeEntry, replace_marker, tree_image, write_workflow


DestinationState = tuple[bytes, int] | None
TreeImage = dict[str, TreeEntry]


class _SimulatedProcessDeath(BaseException):
    """Bypass in-process rollback while Python still releases test resources."""


@dataclass(frozen=True, slots=True)
class _ExistingBundleScenario:
    project: Path
    source: Path
    project_sentinel: Path
    owner_state: Path
    owner_sentinel: Path
    publisher: AuthoringPublisher
    bundle: ProjectCompilationBundle
    destinations: tuple[Path, ...]
    source_before: TreeImage
    project_sentinel_before: TreeImage
    destinations_before: dict[Path, DestinationState]
    owner_sentinel_before: TreeImage


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


def _prepare_existing_bundle_scenario(tmp_path: Path) -> _ExistingBundleScenario:
    project = tmp_path / "project"
    source = write_workflow(project, "leaf")
    project_sentinel = project / "notes" / "owner.txt"
    project_sentinel.parent.mkdir()
    project_sentinel.write_bytes(b"owner bytes\n")
    project_sentinel.chmod(0o640)
    owner_state = (tmp_path / "owner-state").resolve()
    owner_state.mkdir(mode=0o700)
    owner_sentinel = owner_state / "unrelated.bin"
    owner_sentinel.write_bytes(b"unrelated owner state\n")
    owner_sentinel.chmod(0o600)
    publisher = AuthoringPublisher(owner_state)
    publisher.publish(plan_project_compilation(project_paths(project, "leaf")))

    replace_marker(source, "initial", "edited-before-replan")
    source.write_bytes(b"\n" + source.read_bytes())
    bundle = plan_project_compilation(project_paths(project, "leaf"))
    destinations = tuple(image.resolved_path for image in bundle.after_images)
    assert len(destinations) == 3
    assert all(image.content is not None for image in bundle.before_images)
    assert all(
        before.content != after.content
        for before, after in zip(
            bundle.before_images, bundle.after_images, strict=True
        )
    )
    return _ExistingBundleScenario(
        project=project,
        source=source,
        project_sentinel=project_sentinel,
        owner_state=owner_state,
        owner_sentinel=owner_sentinel,
        publisher=publisher,
        bundle=bundle,
        destinations=destinations,
        source_before=tree_image(source),
        project_sentinel_before=tree_image(project_sentinel),
        destinations_before=_destination_states(bundle),
        owner_sentinel_before=tree_image(owner_sentinel),
    )


def _assert_existing_bundle_restored(scenario: _ExistingBundleScenario) -> None:
    assert tree_image(scenario.source) == scenario.source_before
    assert tree_image(scenario.project_sentinel) == scenario.project_sentinel_before
    assert _destination_states(scenario.bundle) == scenario.destinations_before
    assert tree_image(scenario.owner_sentinel) == scenario.owner_sentinel_before


def _assert_durable_crash_cut(
    scenario: _ExistingBundleScenario,
    ordinal: int,
    owner_state_before_crash: TreeImage,
) -> None:
    mixed_destinations = _destination_states(scenario.bundle)
    for index, (before, after) in enumerate(
        zip(
            scenario.bundle.before_images,
            scenario.bundle.after_images,
            strict=True,
        )
    ):
        if index <= ordinal:
            assert after.content is not None
            assert after.mode is not None
            expected = (after.content, after.mode)
        else:
            expected = scenario.destinations_before[before.resolved_path]
        assert mixed_destinations[after.resolved_path] == expected
    owner_state_after_crash = tree_image(scenario.owner_state)
    assert any(
        key not in owner_state_before_crash
        and entry.kind == "regular"
        and entry.mode == 0o600
        for key, entry in owner_state_after_crash.items()
    )
    assert tree_image(scenario.owner_sentinel) == scenario.owner_sentinel_before


def test_successful_publish_materializes_only_exact_bundle_destinations(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = write_workflow(project, "leaf")
    sentinel = project / "notes" / "owner.txt"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"owner bytes\n")
    sentinel.chmod(0o640)
    sentinel_before = tree_image(sentinel)
    bundle = plan_project_compilation(project_paths(project, "leaf"))
    source_before = tree_image(source)
    project_files_before = {
        path.resolve()
        for path in project.rglob("*")
        if not path.is_dir() or path.is_symlink()
    }
    expected_destinations = {
        image.resolved_path for image in bundle.after_images
    }
    assert sentinel.resolve() not in expected_destinations
    owner_state = (tmp_path / "owner-state").resolve()

    AuthoringPublisher(owner_state).publish(bundle)

    for image in bundle.after_images:
        destination_stat = image.resolved_path.lstat()
        assert stat.S_ISREG(destination_stat.st_mode)
        assert image.resolved_path.read_bytes() == image.content
        assert stat.S_IMODE(destination_stat.st_mode) == image.mode
    assert tree_image(source) == source_before
    assert tree_image(sentinel) == sentinel_before
    assert {
        path.resolve()
        for path in project.rglob("*")
        if not path.is_dir() or path.is_symlink()
    } == project_files_before | expected_destinations


def test_source_change_after_plan_is_not_overwritten_or_published(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    child = write_workflow(project, "child")
    write_workflow(project, "parent", children=("child",))
    sentinel = project / "notes" / "owner.txt"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"owner bytes\n")
    sentinel.chmod(0o640)
    sentinel_before = tree_image(sentinel)
    bundle = plan_project_compilation(project_paths(project, "parent"))
    destinations_before = _destination_states(bundle)
    replace_marker(child, "initial", "edited-after-plan")
    edited_source = tree_image(child)
    publisher = AuthoringPublisher((tmp_path / "owner-state").resolve())

    with pytest.raises(AuthoringError):
        publisher.publish(bundle)

    assert tree_image(child) == edited_source
    assert _destination_states(bundle) == destinations_before
    assert tree_image(sentinel) == sentinel_before
    publisher.recover(project)


@pytest.mark.parametrize("ordinal", range(3))
def test_source_change_mid_publish_rolls_outputs_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ordinal: int
) -> None:
    project = tmp_path / "project"
    source = write_workflow(project, "leaf")
    sentinel = project / "notes" / "owner.txt"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"owner bytes\n")
    sentinel.chmod(0o640)
    sentinel_before = tree_image(sentinel)
    bundle = plan_project_compilation(project_paths(project, "leaf"))
    destinations = tuple(image.resolved_path for image in bundle.after_images)
    destinations_before = _destination_states(bundle)
    source_before = source.read_bytes()
    source_mode_before = stat.S_IMODE(source.lstat().st_mode)
    edited_bytes = source_before.replace(b"initial", b"edited-mid-publish")
    assert edited_bytes != source_before
    publisher = AuthoringPublisher((tmp_path / "owner-state").resolve())
    original_link = os.link
    original_replace = os.replace
    mutation_count = 0
    source_was_edited = False

    def edit_after_selected_destination(
        destination: object, directory_fd: int | None
    ) -> None:
        nonlocal mutation_count, source_was_edited
        if source_was_edited or not _is_destination_namespace_call(
            destinations, destination, directory_fd
        ):
            return
        if mutation_count == ordinal:
            source.write_bytes(edited_bytes)
            source_was_edited = True
        mutation_count += 1

    def link_then_edit(source_path, destination_path, *args, **kwargs):
        result = original_link(source_path, destination_path, *args, **kwargs)
        edit_after_selected_destination(destination_path, kwargs.get("dst_dir_fd"))
        return result

    def replace_then_edit(source_path, destination_path, *args, **kwargs):
        result = original_replace(source_path, destination_path, *args, **kwargs)
        edit_after_selected_destination(destination_path, kwargs.get("dst_dir_fd"))
        return result

    monkeypatch.setattr(os, "link", link_then_edit)
    monkeypatch.setattr(os, "replace", replace_then_edit)

    with pytest.raises(AuthoringError):
        publisher.publish(bundle)

    assert len(bundle.after_images) == 3
    assert source_was_edited
    assert mutation_count == ordinal + 1
    assert source.read_bytes() == edited_bytes
    assert stat.S_IMODE(source.lstat().st_mode) == source_mode_before
    assert _destination_states(bundle) == destinations_before
    assert tree_image(sentinel) == sentinel_before
    publisher.recover(project)


@pytest.mark.parametrize("ordinal", (0, 1, 2))
def test_publish_fault_restores_existing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ordinal: int
) -> None:
    scenario = _prepare_existing_bundle_scenario(tmp_path)
    assert len(scenario.destinations) == 3
    original_replace = os.replace
    replacement_count = 0
    fault_was_injected = False

    def replace_then_fail(source_path, destination_path, *args, **kwargs):
        nonlocal fault_was_injected, replacement_count
        result = original_replace(source_path, destination_path, *args, **kwargs)
        if fault_was_injected or not _is_destination_namespace_call(
            scenario.destinations, destination_path, kwargs.get("dst_dir_fd")
        ):
            return result
        current_ordinal = replacement_count
        replacement_count += 1
        if current_ordinal == ordinal:
            fault_was_injected = True
            raise OSError("injected destination replacement fault")
        return result

    monkeypatch.setattr(os, "replace", replace_then_fail)

    with pytest.raises((OSError, AuthoringError)):
        scenario.publisher.publish(scenario.bundle)

    assert fault_was_injected
    assert replacement_count == ordinal + 1
    _assert_existing_bundle_restored(scenario)
    scenario.publisher.recover(scenario.project)


@pytest.mark.parametrize("ordinal", (0, 1, 2))
def test_recover_restores_existing_bundle_after_each_destination_rename_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ordinal: int
) -> None:
    scenario = _prepare_existing_bundle_scenario(tmp_path)
    assert len(scenario.destinations) == 3
    original_replace = os.replace
    replacement_count = 0
    crash_was_injected = False

    def replace_then_crash(source_path, destination_path, *args, **kwargs):
        nonlocal crash_was_injected, replacement_count
        result = original_replace(source_path, destination_path, *args, **kwargs)
        if crash_was_injected or not _is_destination_namespace_call(
            scenario.destinations, destination_path, kwargs.get("dst_dir_fd")
        ):
            return result
        current_ordinal = replacement_count
        replacement_count += 1
        if current_ordinal == ordinal:
            crash_was_injected = True
            raise _SimulatedProcessDeath(
                "simulated process death after destination rename"
            )
        return result

    monkeypatch.setattr(os, "replace", replace_then_crash)
    owner_state_before_crash = tree_image(scenario.owner_state)
    with pytest.raises(_SimulatedProcessDeath):
        scenario.publisher.publish(scenario.bundle)

    assert crash_was_injected
    assert replacement_count == ordinal + 1
    _assert_durable_crash_cut(scenario, ordinal, owner_state_before_crash)
    monkeypatch.setattr(os, "replace", original_replace)

    fresh_publisher = AuthoringPublisher(scenario.owner_state)
    fresh_publisher.recover(scenario.project)

    _assert_existing_bundle_restored(scenario)
    project_before_second_recovery = tree_image(scenario.project)
    owner_before_second_recovery = tree_image(scenario.owner_state)
    fresh_publisher.recover(scenario.project)
    assert tree_image(scenario.project) == project_before_second_recovery
    assert tree_image(scenario.owner_state) == owner_before_second_recovery
