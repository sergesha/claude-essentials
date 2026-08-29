"""Read-command recovery and authoring-lock serialization."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
import fcntl
from pathlib import Path

import pytest

from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.errors import AuthoringError

from tests._authoring_crash_gate import (
    install_mutation_syscall_probe,
    namespace_image,
    opaque_lock_identities,
)
from tests._authoring_gate import (
    TreeEntry,
    mcp_context,
    tree_image,
)


DestinationState = tuple[bytes, int] | None
TreeImage = dict[str, TreeEntry]



from tests._authoring_publisher_faults import _SimulatedProcessDeath
from tests._authoring_publisher_namespace import (
    _NamespaceEntry,
    _is_destination_namespace_call,
    _namespace_file_image,
)
from tests._authoring_publisher_scenario import (
    _ExistingBundleScenario,
    _prepare_existing_bundle_scenario,
    _assert_existing_bundle_restored,
    _assert_durable_crash_cut,
)

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
