"""Shared exact publisher scenario construction and assertions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lockstep.authoring import project_paths
from lockstep.authoring_bundle import (
    ProjectCompilationBundle,
    plan_project_compilation,
)
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.authoring_recovery_model import (
    RecoveryAfterImage,
    RecoveryBeforeImage,
    RecoveryWriteEntry,
)
from tests._authoring_gate import (
    TreeEntry,
    replace_marker,
    tree_image,
    write_workflow,
)


DestinationState = tuple[bytes, int] | None
TreeImage = dict[str, TreeEntry]


from tests._authoring_publisher_namespace import (
    _NamespaceEntry,
    _namespace_file_image,
    _regular_file_semantics,
    _regular_file_identity,
    _destination_states,
)

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
    destination_parent_sentinels: tuple[Path, ...]
    source_before: tuple[bytes, int]
    project_sentinel_before: tuple[bytes, int]
    destinations_before: dict[Path, DestinationState]
    destination_parent_sentinels_before: dict[Path, tuple[bytes, int]]
    owner_sentinel_before: tuple[bytes, int]




def _recovery_write_entry(
    bundle: ProjectCompilationBundle, index: int
) -> RecoveryWriteEntry:
    before = bundle.before_images[index]
    after = bundle.after_images[index]
    assert after.content is not None
    assert after.sha256 is not None
    assert after.mode is not None
    return RecoveryWriteEntry(
        index=index,
        role=after.role,
        path=after.resolved_path,
        before=RecoveryBeforeImage(
            before.content,
            before.sha256,
            before.mode,
            before.leaf,
        ),
        after=RecoveryAfterImage(after.sha256, len(after.content), after.mode),
        ancestors=before.ancestors,
    )




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
    initial_bundle = plan_project_compilation(project_paths(project, "leaf"))
    publisher.publish(initial_bundle)
    destination_parent_sentinels = tuple(
        parent / f"unrelated-sibling-{index}.bin"
        for index, parent in enumerate(
            sorted({image.resolved_path.parent for image in initial_bundle.after_images})
        )
    )
    for index, sentinel in enumerate(destination_parent_sentinels):
        sentinel.write_bytes(f"unrelated destination sibling {index}\n".encode())
        sentinel.chmod(0o640 if index % 2 == 0 else 0o600)

    replace_marker(source, "initial", "edited-before-replan")
    source.write_bytes(b"\n" + source.read_bytes())
    bundle = plan_project_compilation(project_paths(project, "leaf"))
    destinations = tuple(image.resolved_path for image in bundle.after_images)
    assert len(destinations) == 3
    assert {path.parent for path in destinations} == {
        path.parent for path in destination_parent_sentinels
    }
    assert not set(destination_parent_sentinels) & set(destinations)
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
        destination_parent_sentinels=destination_parent_sentinels,
        source_before=_regular_file_semantics(source),
        project_sentinel_before=_regular_file_semantics(project_sentinel),
        destinations_before=_destination_states(bundle),
        destination_parent_sentinels_before={
            path: _regular_file_semantics(path)
            for path in destination_parent_sentinels
        },
        owner_sentinel_before=_regular_file_semantics(owner_sentinel),
    )




def _assert_existing_bundle_restored(scenario: _ExistingBundleScenario) -> None:
    assert _regular_file_semantics(scenario.source) == scenario.source_before
    assert _destination_states(scenario.bundle) == scenario.destinations_before
    _assert_unrelated_sentinels_unchanged(scenario)




def _assert_unrelated_sentinels_unchanged(
    scenario: _ExistingBundleScenario,
) -> None:
    assert (
        _regular_file_semantics(scenario.project_sentinel)
        == scenario.project_sentinel_before
    )
    assert {
        path: _regular_file_semantics(path)
        for path in scenario.destination_parent_sentinels
    } == scenario.destination_parent_sentinels_before
    assert (
        _regular_file_semantics(scenario.owner_sentinel)
        == scenario.owner_sentinel_before
    )




def _assert_durable_owner_evidence(
    scenario: _ExistingBundleScenario, owner_state_before_crash: TreeImage
) -> None:
    owner_state_after_crash = tree_image(scenario.owner_state)
    assert any(
        key not in owner_state_before_crash
        and entry.kind == "regular"
        and entry.mode == 0o600
        for key, entry in owner_state_after_crash.items()
    )
    _assert_unrelated_sentinels_unchanged(scenario)




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
    _assert_durable_owner_evidence(scenario, owner_state_before_crash)




def _recover_twice_with_fresh_publishers(
    scenario: _ExistingBundleScenario,
    expected_project: dict[str, _NamespaceEntry],
) -> None:
    AuthoringPublisher(scenario.owner_state).recover(scenario.project)
    assert _namespace_file_image(scenario.project) == expected_project
    _assert_existing_bundle_restored(scenario)
    project_before_second_recovery = _namespace_file_image(scenario.project)
    owner_before_second_recovery = _namespace_file_image(scenario.owner_state)

    AuthoringPublisher(scenario.owner_state).recover(scenario.project)

    assert _namespace_file_image(scenario.project) == project_before_second_recovery
    assert _namespace_file_image(scenario.owner_state) == owner_before_second_recovery




def _assert_two_fresh_recoveries_are_noops(
    scenario: _ExistingBundleScenario,
    expected_project: dict[str, _NamespaceEntry],
    expected_owner: dict[str, _NamespaceEntry],
    reserved_stage: Path,
    expected_reserved_semantics: tuple[bytes, int],
    expected_reserved_identity: tuple[int, int],
    probe,
) -> None:
    def probe_counts() -> tuple[int, int, int, int]:
        return (
            probe.stage_create_syscall_calls,
            probe.stage_write_syscall_calls,
            probe.destination_replace_calls,
            probe.owner_state_replace_calls,
        )

    expected_probe_counts = probe_counts()
    for _ in range(2):
        AuthoringPublisher(scenario.owner_state).recover(scenario.project)
        assert probe_counts() == expected_probe_counts
        assert _namespace_file_image(scenario.project) == expected_project
        assert _namespace_file_image(scenario.owner_state) == expected_owner
        assert _regular_file_semantics(reserved_stage) == expected_reserved_semantics
        assert _regular_file_identity(reserved_stage) == expected_reserved_identity
        _assert_existing_bundle_restored(scenario)
