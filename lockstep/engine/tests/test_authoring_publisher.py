"""Public authoring publisher contract."""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass, field
import fcntl
from pathlib import Path

import pytest

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
from lockstep.errors import AuthoringError

from tests._authoring_crash_gate import (
    install_mutation_syscall_probe,
    namespace_image,
    opaque_lock_identities,
)
from tests._authoring_gate import (
    TreeEntry,
    mcp_context,
    replace_marker,
    tree_image,
    write_workflow,
)


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
    destination_parent_sentinels: tuple[Path, ...]
    source_before: tuple[bytes, int]
    project_sentinel_before: tuple[bytes, int]
    destinations_before: dict[Path, DestinationState]
    destination_parent_sentinels_before: dict[Path, tuple[bytes, int]]
    owner_sentinel_before: tuple[bytes, int]


@dataclass(frozen=True, slots=True)
class _NamespaceEntry:
    kind: str
    mode: int
    content: bytes | None
    symlink_target: str | None


@dataclass(slots=True)
class _IncompleteStageFault:
    cut: str
    injected: bool = False
    created_identity: tuple[int, int] | None = None
    attempted_bytes: int = 0
    prefix_bytes: int = 0


@dataclass(slots=True)
class _DestinationReplacementFault:
    injected: bool = False


@dataclass(slots=True)
class _StageReservationProbe:
    stage_create_syscall_calls: int = 0
    stage_write_syscall_calls: int = 0
    destination_replace_calls: int = 0
    owner_state_replace_calls: int = 0
    stage_identities: tuple[tuple[int, int], ...] = ()


@dataclass(slots=True)
class _CooperatingWriterProbe:
    scenario: _ExistingBundleScenario
    attempted_lock: threading.Event = field(default_factory=threading.Event)
    mutated_destination: threading.Event = field(default_factory=threading.Event)
    mutation_before_observation_complete: threading.Event = field(
        default_factory=threading.Event
    )
    observation_complete: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    thread_id: int | None = None
    reader_thread_id: int = field(default_factory=threading.get_ident)
    reader_lock_identity: tuple[int, int] | None = None
    writer_lock_identity: tuple[int, int] | None = None
    reader_holds_lock: bool = False
    unexpected_writer_errors: list[BaseException] = field(default_factory=list)

    def start(self) -> None:
        assert self.thread is None

        def publish() -> None:
            self.thread_id = threading.get_ident()
            try:
                self.scenario.publisher.publish(self.scenario.bundle)
            except _SimulatedProcessDeath:
                pass
            except BaseException as exc:
                self.unexpected_writer_errors.append(exc)
            finally:
                self.finished.set()

        self.thread = threading.Thread(target=publish)
        self.thread.start()


def _namespace_file_image(root: Path) -> dict[str, _NamespaceEntry]:
    return {
        key: _NamespaceEntry(
            entry.kind,
            entry.mode,
            entry.content if entry.kind == "regular" else None,
            entry.symlink_target if entry.kind == "symlink" else None,
        )
        for key, entry in tree_image(root).items()
    }


def _regular_file_semantics(path: Path) -> tuple[bytes, int]:
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode)
    return path.read_bytes(), stat.S_IMODE(info.st_mode)


def _regular_file_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode)
    return info.st_dev, info.st_ino


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


def _is_destination_parent_descriptor(
    destinations: tuple[Path, ...], directory_fd: int | None
) -> bool:
    if directory_fd is None:
        return False
    directory_info = os.fstat(directory_fd)
    return any(
        (parent_info.st_dev, parent_info.st_ino)
        == (directory_info.st_dev, directory_info.st_ino)
        for parent_info in (path.parent.stat() for path in destinations)
    )


def _is_exclusive_create(flags: int) -> bool:
    return bool(flags & os.O_CREAT and flags & os.O_EXCL)


def _is_owner_state_destination(
    owner_state: Path, destination: object, directory_fd: int | None
) -> bool:
    path = Path(os.fsdecode(destination))
    if path.is_absolute():
        try:
            path.relative_to(owner_state)
        except ValueError:
            return False
        return True
    if directory_fd is None:
        return False
    directory_info = os.fstat(directory_fd)
    owner_directories = (owner_state,) + tuple(
        item for item in owner_state.rglob("*") if item.is_dir()
    )
    return any(
        (info.st_dev, info.st_ino)
        == (directory_info.st_dev, directory_info.st_ino)
        for info in (item.stat() for item in owner_directories)
    )


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


def _install_first_destination_replacement_fault(
    monkeypatch: pytest.MonkeyPatch, destinations: tuple[Path, ...]
) -> _DestinationReplacementFault:
    original_replace = os.replace
    fault = _DestinationReplacementFault()

    def replace_then_crash(source_path, destination_path, *args, **kwargs):
        result = original_replace(source_path, destination_path, *args, **kwargs)
        if fault.injected or not _is_destination_namespace_call(
            destinations, destination_path, kwargs.get("dst_dir_fd")
        ):
            return result
        fault.injected = True
        raise _SimulatedProcessDeath(
            "simulated process death after destination rename"
        )

    monkeypatch.setattr(os, "replace", replace_then_crash)
    return fault


def _regular_path_with_identity(
    project: Path, identity: tuple[int, int]
) -> Path:
    matches: list[Path] = []
    for path in project.rglob("*"):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity:
            matches.append(path)
    assert len(matches) == 1
    return matches[0]


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
    probe: _StageReservationProbe,
) -> None:
    expected_probe_counts = _stage_reservation_probe_counts(probe)
    for _ in range(2):
        AuthoringPublisher(scenario.owner_state).recover(scenario.project)
        assert _stage_reservation_probe_counts(probe) == expected_probe_counts
        assert _namespace_file_image(scenario.project) == expected_project
        assert _namespace_file_image(scenario.owner_state) == expected_owner
        assert _regular_file_semantics(reserved_stage) == expected_reserved_semantics
        assert _regular_file_identity(reserved_stage) == expected_reserved_identity
        _assert_existing_bundle_restored(scenario)


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


def _crash_after_first_destination(
    scenario: _ExistingBundleScenario, monkeypatch: pytest.MonkeyPatch
) -> dict[str, _NamespaceEntry]:
    original_replace = os.replace
    replacement_count = 0
    owner_namespace_before_crash = _namespace_file_image(scenario.owner_state)

    def replace_then_crash(source_path, destination_path, *args, **kwargs):
        nonlocal replacement_count
        result = original_replace(source_path, destination_path, *args, **kwargs)
        if not _is_destination_namespace_call(
            scenario.destinations, destination_path, kwargs.get("dst_dir_fd")
        ):
            return result
        replacement_count += 1
        if replacement_count == 1:
            raise _SimulatedProcessDeath("mixed authoring transaction")
        return result

    monkeypatch.setattr(os, "replace", replace_then_crash)
    owner_state_before_crash = tree_image(scenario.owner_state)
    with pytest.raises(_SimulatedProcessDeath):
        scenario.publisher.publish(scenario.bundle)
    assert replacement_count == 1
    _assert_durable_crash_cut(scenario, 0, owner_state_before_crash)
    monkeypatch.setattr(os, "replace", original_replace)
    return owner_namespace_before_crash


def _observe_recovered_recipe_lookup(
    scenario: _ExistingBundleScenario,
    owner_namespace_before_crash: dict[str, _NamespaceEntry],
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    import lockstep.authoring as authoring

    original_project_paths = authoring.project_paths
    observed: list[str] = []

    def project_paths_after_recovery(project: Path, name: str):
        _assert_existing_bundle_restored(scenario)
        assert _namespace_file_image(scenario.owner_state) == owner_namespace_before_crash
        observed.append(name)
        return original_project_paths(project, name)

    monkeypatch.setattr(authoring, "project_paths", project_paths_after_recovery)
    return observed


def _observe_recovered_recipe_enumeration(
    scenario: _ExistingBundleScenario,
    owner_namespace_before_crash: dict[str, _NamespaceEntry],
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    original_glob = Path.glob
    recipes = scenario.project / ".lockstep" / "recipes"
    observed: list[str] = []

    def glob_after_recovery(path: Path, pattern: str):
        if path == recipes:
            _assert_existing_bundle_restored(scenario)
            assert _namespace_file_image(scenario.owner_state) == owner_namespace_before_crash
            observed.append(pattern)
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", glob_after_recovery)
    return observed


def _invoke_read_command(
    scenario: _ExistingBundleScenario,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adapter: str,
    action: str,
) -> None:
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(scenario.owner_state))
    if adapter == "cli":
        from lockstep import cli

        monkeypatch.chdir(scenario.project)
        assert cli.main(["recipe", action, "leaf"]) == (2 if action == "check" else 0)
        observed = capsys.readouterr()
        assert (observed.err if action == "check" else observed.out)
    else:
        from lockstep.mcp import server

        if action == "check":
            with pytest.raises(ValueError, match="canonical|byte-for-byte"):
                server.recipe_check("leaf", ctx=mcp_context(scenario.project))
        else:
            assert server.recipe_diff("leaf", ctx=mcp_context(scenario.project))


def _assert_recovery_evidence_is_retired(
    scenario: _ExistingBundleScenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_existing_bundle_restored(scenario)
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


def _install_cooperating_writer_probe(
    scenario: _ExistingBundleScenario, monkeypatch: pytest.MonkeyPatch
) -> _CooperatingWriterProbe:
    probe = _CooperatingWriterProbe(scenario)
    original_flock = fcntl.flock
    original_link = os.link
    original_replace = os.replace

    def flock_with_attempt(file_descriptor: int, operation: int) -> None:
        caller = threading.get_ident()
        info = os.fstat(file_descriptor)
        identity = (info.st_dev, info.st_ino)
        if caller == probe.thread_id and operation & fcntl.LOCK_EX:
            if probe.writer_lock_identity is None:
                probe.writer_lock_identity = identity
            else:
                assert probe.writer_lock_identity == identity
            probe.attempted_lock.set()
        original_flock(file_descriptor, operation)
        if caller != probe.reader_thread_id:
            return
        if operation & fcntl.LOCK_EX:
            probe.reader_lock_identity = identity
            probe.reader_holds_lock = True
        elif operation & fcntl.LOCK_UN:
            if identity == probe.reader_lock_identity:
                probe.reader_holds_lock = False

    def crash_after_writer_mutation(destination: object, directory_fd: int | None) -> None:
        if (
            threading.get_ident() == probe.thread_id
            and _is_destination_namespace_call(
                scenario.destinations, destination, directory_fd
            )
        ):
            if not probe.observation_complete.is_set():
                probe.mutation_before_observation_complete.set()
            probe.mutated_destination.set()
            raise _SimulatedProcessDeath("cooperating writer crash")

    def link_then_crash(source_path, destination_path, *args, **kwargs):
        result = original_link(source_path, destination_path, *args, **kwargs)
        crash_after_writer_mutation(destination_path, kwargs.get("dst_dir_fd"))
        return result

    def replace_then_crash(source_path, destination_path, *args, **kwargs):
        result = original_replace(source_path, destination_path, *args, **kwargs)
        crash_after_writer_mutation(destination_path, kwargs.get("dst_dir_fd"))
        return result

    monkeypatch.setattr(fcntl, "flock", flock_with_attempt)
    monkeypatch.setattr(os, "link", link_then_crash)
    monkeypatch.setattr(os, "replace", replace_then_crash)
    return probe


def _require_same_lock_contention(probe: _CooperatingWriterProbe) -> None:
    probe.start()
    assert probe.attempted_lock.wait(5), "cooperating writer did not reach project lock"
    assert probe.reader_holds_lock, "read command did not hold the authoring lock"
    assert probe.reader_lock_identity == probe.writer_lock_identity
    assert not probe.mutated_destination.is_set()


def _require_lock_through_observation(probe: _CooperatingWriterProbe) -> None:
    assert probe.reader_holds_lock, (
        "read command released the authoring lock before observation completed"
    )
    assert probe.reader_lock_identity == probe.writer_lock_identity
    assert not probe.mutation_before_observation_complete.is_set()
    probe.observation_complete.set()


@pytest.mark.parametrize("adapter", ("cli", "mcp"))
@pytest.mark.parametrize("action", ("check", "diff"))
def test_read_command_recovers_mixed_authoring_transaction_before_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adapter: str,
    action: str,
) -> None:
    scenario = _prepare_existing_bundle_scenario(tmp_path)
    owner_namespace_before_crash = _crash_after_first_destination(
        scenario, monkeypatch
    )
    observed = _observe_recovered_recipe_lookup(
        scenario, owner_namespace_before_crash, monkeypatch
    )

    _invoke_read_command(scenario, monkeypatch, capsys, adapter, action)

    assert observed
    _assert_recovery_evidence_is_retired(scenario, monkeypatch)


def test_cli_check_all_recovers_before_recipe_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from lockstep import cli

    scenario = _prepare_existing_bundle_scenario(tmp_path)
    owner_namespace_before_crash = _crash_after_first_destination(
        scenario, monkeypatch
    )
    observed = _observe_recovered_recipe_enumeration(
        scenario, owner_namespace_before_crash, monkeypatch
    )
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(scenario.owner_state))
    monkeypatch.chdir(scenario.project)

    assert cli.main(["recipe", "check", "--all"]) == 2
    assert capsys.readouterr().err
    assert observed == ["*.recipe.yaml"]
    _assert_recovery_evidence_is_retired(scenario, monkeypatch)


@pytest.mark.parametrize("adapter", ("cli", "mcp"))
@pytest.mark.parametrize("action", ("check", "diff"))
@pytest.mark.parametrize("invalid_name", ("../../../escape", ""))
def test_invalid_read_command_does_not_recover_or_mutate_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adapter: str,
    action: str,
    invalid_name: str,
) -> None:
    scenario = _prepare_existing_bundle_scenario(tmp_path)
    _crash_after_first_destination(scenario, monkeypatch)
    project_before = namespace_image(scenario.project)
    owner_before = namespace_image(scenario.owner_state)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(scenario.owner_state))

    if adapter == "cli":
        from lockstep import cli

        monkeypatch.chdir(scenario.project)
        assert cli.main(["recipe", action, invalid_name]) == 2
        assert "invalid workflow name" in capsys.readouterr().err
    else:
        from lockstep.mcp import server

        command = server.recipe_check if action == "check" else server.recipe_diff
        with pytest.raises(AuthoringError, match="invalid workflow name"):
            command(invalid_name, ctx=mcp_context(scenario.project))

    assert namespace_image(scenario.project) == project_before
    assert namespace_image(scenario.owner_state) == owner_before


@pytest.mark.parametrize("surface", ("named", "all"))
def test_read_command_holds_authoring_lock_through_complete_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    from lockstep import cli
    import lockstep.authoring as authoring

    scenario = _prepare_existing_bundle_scenario(tmp_path)
    probe = _install_cooperating_writer_probe(scenario, monkeypatch)
    observed: list[str] = []
    remaining_checks = 1
    original_check_recipe = authoring.check_recipe

    def check_recipe_under_lock(project: Path, name: str):
        nonlocal remaining_checks
        try:
            return original_check_recipe(project, name)
        finally:
            remaining_checks -= 1
            if remaining_checks == 0:
                _require_lock_through_observation(probe)

    monkeypatch.setattr(authoring, "check_recipe", check_recipe_under_lock)
    if surface == "named":
        original_project_paths = authoring.project_paths

        def project_paths_under_lock(project: Path, name: str):
            _require_same_lock_contention(probe)
            observed.append(name)
            return original_project_paths(project, name)

        monkeypatch.setattr(authoring, "project_paths", project_paths_under_lock)
        args = ["recipe", "check", "leaf"]
    else:
        original_glob = Path.glob
        recipes = scenario.project / ".lockstep" / "recipes"

        def glob_under_lock(path: Path, pattern: str):
            nonlocal remaining_checks
            if path == recipes:
                matches = tuple(original_glob(path, pattern))
                remaining_checks = len(matches)
                _require_same_lock_contention(probe)
                observed.append(pattern)
                return iter(matches)
            return original_glob(path, pattern)

        monkeypatch.setattr(Path, "glob", glob_under_lock)
        args = ["recipe", "check", "--all"]

    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(scenario.owner_state))
    monkeypatch.chdir(scenario.project)
    try:
        assert cli.main(args) == 2
        assert capsys.readouterr().err
        assert observed
        assert probe.observation_complete.is_set()
    finally:
        if probe.thread is not None:
            probe.thread.join(5)
            assert not probe.thread.is_alive()
    assert probe.finished.is_set()
    assert probe.unexpected_writer_errors == []
    assert probe.mutated_destination.is_set()
    assert not probe.mutation_before_observation_complete.is_set()


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
