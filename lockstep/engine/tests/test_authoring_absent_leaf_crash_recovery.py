"""Crash recovery contract for planned-absent authoring destinations."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from lockstep.authoring import project_paths
from lockstep.authoring_bundle import (
    ProjectCompilationBundle,
    plan_project_compilation,
)
from lockstep.authoring_publisher import AuthoringPublisher

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
    """Escape the publisher's in-process ``Exception`` rollback boundary."""


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
    allowed_write_open_identities = opaque_lock_identities(
        scenario.owner_before, owner_after_first_recovery
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


def test_recover_accepts_real_destination_only_bundle_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _prepare_absent_leaf_scenario(tmp_path)
    destination_only_bundle = replace(scenario.bundle, sources=())
    original_link = os.link
    destination_link_count = 0

    def link_then_crash(source, destination, *args, **kwargs):
        nonlocal destination_link_count
        result = original_link(source, destination, *args, **kwargs)
        if not _is_destination_link(
            scenario, destination, kwargs.get("dst_dir_fd")
        ):
            return result
        destination_link_count += 1
        raise _SimulatedProcessDeath(
            "simulated process death after destination-only publication"
        )

    monkeypatch.setattr(os, "link", link_then_crash)
    with pytest.raises(_SimulatedProcessDeath):
        AuthoringPublisher(scenario.owner_state).publish(destination_only_bundle)

    assert destination_only_bundle.sources == ()
    assert destination_link_count == 1
    _assert_crash_namespace(scenario, 0)
    _assert_opaque_durable_owner_evidence(scenario)
    monkeypatch.setattr(os, "link", original_link)

    publisher = AuthoringPublisher(scenario.owner_state)
    publisher.recover(scenario.project)

    assert _namespace_image(scenario.project) == scenario.project_before
    _assert_sentinels_unchanged(scenario)
    _assert_destination_parents_unchanged(scenario)
