"""Incomplete-stage recovery and reserved-stage collision handling."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.errors import AuthoringError

from tests._authoring_gate import tree_image
from tests._authoring_publisher_faults import (
    _SimulatedProcessDeath,
    _install_first_destination_replacement_fault,
)
from tests._authoring_publisher_namespace import (
    _is_destination_namespace_call,
    _is_destination_parent_descriptor,
    _is_exclusive_create,
    _is_owner_state_destination,
    _namespace_file_image,
    _regular_file_identity,
    _regular_file_semantics,
    _regular_path_with_identity,
)
from tests._authoring_publisher_scenario import (
    _ExistingBundleScenario,
    _recovery_write_entry,
    _prepare_existing_bundle_scenario,
    _assert_existing_bundle_restored,
    _assert_unrelated_sentinels_unchanged,
    _assert_durable_owner_evidence,
    _assert_durable_crash_cut,
    _recover_twice_with_fresh_publishers,
    _assert_two_fresh_recoveries_are_noops,
)

@dataclass(slots=True)
class _IncompleteStageFault:
    cut: str
    injected: bool = False
    created_identity: tuple[int, int] | None = None
    attempted_bytes: int = 0
    prefix_bytes: int = 0



@dataclass(slots=True)
class _StageReservationProbe:
    stage_create_syscall_calls: int = 0
    stage_write_syscall_calls: int = 0
    destination_replace_calls: int = 0
    owner_state_replace_calls: int = 0
    stage_identities: tuple[tuple[int, int], ...] = ()



def _stage_reservation_probe_counts(
    probe: _StageReservationProbe,
) -> tuple[int, int, int, int]:
    return (
        probe.stage_create_syscall_calls,
        probe.stage_write_syscall_calls,
        probe.destination_replace_calls,
        probe.owner_state_replace_calls,
    )



def _install_incomplete_stage_fault(
    monkeypatch: pytest.MonkeyPatch,
    destinations: tuple[Path, ...],
    cut: str,
) -> _IncompleteStageFault:
    assert cut in {"after_create", "after_prefix"}
    original_open = os.open
    original_write = os.write
    fault = _IncompleteStageFault(cut)

    def open_then_maybe_crash(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        directory_fd = kwargs.get("dir_fd")
        if (
            fault.created_identity is not None
            or not _is_exclusive_create(flags)
            or not _is_destination_parent_descriptor(destinations, directory_fd)
            or _is_destination_namespace_call(destinations, path, directory_fd)
        ):
            return descriptor
        created = os.fstat(descriptor)
        assert stat.S_ISREG(created.st_mode)
        fault.created_identity = (created.st_dev, created.st_ino)
        if cut == "after_create":
            os.close(descriptor)
            fault.injected = True
            raise _SimulatedProcessDeath(
                "simulated process death after exclusive stage create"
            )
        return descriptor

    def write_prefix_then_crash(descriptor, data):
        if fault.created_identity is None or fault.injected:
            return original_write(descriptor, data)
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != fault.created_identity:
            return original_write(descriptor, data)
        view = memoryview(data)
        assert len(view) > 1
        prefix = view[: max(1, len(view) // 2)]
        written = original_write(descriptor, prefix)
        assert 0 < written < len(view)
        fault.attempted_bytes = len(view)
        fault.prefix_bytes = written
        fault.injected = True
        raise _SimulatedProcessDeath(
            "simulated process death after strict stage prefix"
        )

    monkeypatch.setattr(os, "open", open_then_maybe_crash)
    monkeypatch.setattr(os, "write", write_prefix_then_crash)
    return fault



def _install_stage_reservation_probe(
    monkeypatch: pytest.MonkeyPatch,
    destinations: tuple[Path, ...],
    owner_state: Path,
) -> _StageReservationProbe:
    original_open = os.open
    original_write = os.write
    original_replace = os.replace
    probe = _StageReservationProbe()

    def observe_open(path, flags, *args, **kwargs):
        is_stage_create = (
            _is_exclusive_create(flags)
            and _is_destination_parent_descriptor(
                destinations, kwargs.get("dir_fd")
            )
        )
        if is_stage_create:
            probe.stage_create_syscall_calls += 1
        descriptor = original_open(path, flags, *args, **kwargs)
        if is_stage_create:
            info = os.fstat(descriptor)
            probe.stage_identities += ((info.st_dev, info.st_ino),)
        return descriptor

    def observe_write(descriptor, data):
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) in probe.stage_identities:
            probe.stage_write_syscall_calls += 1
        return original_write(descriptor, data)

    def observe_replace(source_path, destination_path, *args, **kwargs):
        if _is_destination_namespace_call(
            destinations, destination_path, kwargs.get("dst_dir_fd")
        ):
            probe.destination_replace_calls += 1
        if _is_owner_state_destination(
            owner_state, destination_path, kwargs.get("dst_dir_fd")
        ):
            probe.owner_state_replace_calls += 1
        return original_replace(source_path, destination_path, *args, **kwargs)

    monkeypatch.setattr(os, "open", observe_open)
    monkeypatch.setattr(os, "write", observe_write)
    monkeypatch.setattr(os, "replace", observe_replace)
    return probe



def _assert_incomplete_stage_residue(
    scenario: _ExistingBundleScenario, fault: _IncompleteStageFault
) -> None:
    assert fault.injected
    assert fault.created_identity is not None
    residue = _regular_path_with_identity(scenario.project, fault.created_identity)
    assert residue.parent in {path.parent for path in scenario.destinations}
    assert residue not in scenario.destinations
    if fault.cut == "after_create":
        assert residue.stat().st_size == 0
    else:
        assert 0 < fault.prefix_bytes < fault.attempted_bytes
        assert residue.stat().st_size == fault.prefix_bytes



@pytest.mark.parametrize("cut", ("after_create", "after_prefix"))
def test_recover_cleans_incomplete_publication_stage_and_restores_existing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    scenario = _prepare_existing_bundle_scenario(tmp_path)
    project_before_crash = _namespace_file_image(scenario.project)
    owner_state_before_crash = tree_image(scenario.owner_state)

    with monkeypatch.context() as crash_patch:
        fault = _install_incomplete_stage_fault(
            crash_patch, scenario.destinations, cut
        )
        with pytest.raises(_SimulatedProcessDeath):
            scenario.publisher.publish(scenario.bundle)

    _assert_incomplete_stage_residue(scenario, fault)
    _assert_existing_bundle_restored(scenario)
    _assert_durable_owner_evidence(scenario, owner_state_before_crash)

    _recover_twice_with_fresh_publishers(scenario, project_before_crash)



@pytest.mark.parametrize("cut", ("after_create", "after_prefix"))
def test_recover_cleans_incomplete_restoration_stage_and_restores_existing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    scenario = _prepare_existing_bundle_scenario(tmp_path)
    project_before_crash = _namespace_file_image(scenario.project)
    owner_state_before_crash = tree_image(scenario.owner_state)

    with monkeypatch.context() as replacement_patch:
        replacement_fault = _install_first_destination_replacement_fault(
            replacement_patch, scenario.destinations
        )
        with pytest.raises(_SimulatedProcessDeath):
            scenario.publisher.publish(scenario.bundle)

    assert replacement_fault.injected
    _assert_durable_crash_cut(scenario, 0, owner_state_before_crash)
    project_before_stage_crash = _namespace_file_image(scenario.project)

    with monkeypatch.context() as crash_patch:
        stage_fault = _install_incomplete_stage_fault(
            crash_patch, scenario.destinations, cut
        )
        with pytest.raises(_SimulatedProcessDeath):
            AuthoringPublisher(scenario.owner_state).recover(scenario.project)

    _assert_incomplete_stage_residue(scenario, stage_fault)
    assert _regular_file_semantics(scenario.source) == scenario.source_before
    _assert_unrelated_sentinels_unchanged(scenario)
    assert (
        len(_namespace_file_image(scenario.project))
        == len(project_before_stage_crash) + 1
    )
    _assert_durable_crash_cut(scenario, 0, owner_state_before_crash)

    _recover_twice_with_fresh_publishers(scenario, project_before_crash)



@pytest.mark.parametrize("reservation", ("publication", "restoration"))
def test_publish_rejects_stage_reservation_collision_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reservation: str,
) -> None:
    import lockstep.authoring_transaction as authoring_transaction

    scenario = _prepare_existing_bundle_scenario(tmp_path)
    operation_id = "a" * 32
    entry = _recovery_write_entry(
        scenario.bundle, len(scenario.bundle.after_images) - 1
    )
    reserved_stage = (
        entry.publication_stage(operation_id)
        if reservation == "publication"
        else entry.restoration_stage(operation_id)
    )
    assert not reserved_stage.exists()
    reserved_stage.write_bytes(f"unrelated {reservation} reservation\n".encode())
    reserved_stage.chmod(0o640)
    reserved_stage_before = _regular_file_semantics(reserved_stage)
    reserved_stage_identity = _regular_file_identity(reserved_stage)
    project_before_publish = _namespace_file_image(scenario.project)
    owner_before_publish = _namespace_file_image(scenario.owner_state)

    monkeypatch.setattr(
        authoring_transaction.secrets,
        "token_hex",
        lambda _byte_count: operation_id,
    )
    probe = _install_stage_reservation_probe(
        monkeypatch,
        scenario.destinations,
        scenario.owner_state,
    )
    publication_error: Exception | None = None
    try:
        scenario.publisher.publish(scenario.bundle)
    except (OSError, AuthoringError) as exc:
        publication_error = exc

    assert (
        isinstance(publication_error, AuthoringError),
        None if publication_error is None else str(publication_error),
        _stage_reservation_probe_counts(probe),
    ) == (
        True,
        "authoring reserved stage path is occupied",
        (0, 0, 0, 0),
    )
    assert _regular_file_semantics(reserved_stage) == reserved_stage_before
    assert _regular_file_identity(reserved_stage) == reserved_stage_identity
    assert _namespace_file_image(scenario.project) == project_before_publish
    assert _namespace_file_image(scenario.owner_state) == owner_before_publish
    _assert_existing_bundle_restored(scenario)
    _assert_two_fresh_recoveries_are_noops(
        scenario,
        project_before_publish,
        owner_before_publish,
        reserved_stage,
        reserved_stage_before,
        reserved_stage_identity,
        probe,
    )
