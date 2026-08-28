"""Canonical start admission must share the authoring recovery boundary."""

from __future__ import annotations

import fcntl
import os
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from lockstep.authoring import project_paths, publish_project_compilation
from lockstep.authoring_bundle import ProjectCompilationBundle, plan_project_compilation
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.mcp import server
from lockstep.recipe.authority import (
    AuthorizedRecipe,
    RecipeCandidate,
    StrictRecipeIngress,
)
from lockstep.runtime.service import LockstepCommandService

from tests._authoring_crash_gate import NamespaceEntry, namespace_image
from tests._authoring_gate import mcp_context, replace_marker, write_workflow


DestinationSemantics = dict[Path, tuple[bytes, int]]


class _SimulatedProcessDeath(BaseException):
    """Leave real publisher evidence while normal context cleanup releases FDs."""


@dataclass(frozen=True, slots=True)
class _MixedCanonicalScenario:
    project: Path
    owner_state: Path
    source: Path
    bundle: ProjectCompilationBundle
    before: DestinationSemantics
    lock_identity: tuple[int, int]


@dataclass(slots=True)
class _CanonicalObservation:
    expected_before: DestinationSemantics
    exact_paths: frozenset[Path]
    lock_identity: tuple[int, int]
    reader_thread: int = field(default_factory=threading.get_ident)
    lock_held: bool = False
    reads: list[Path] = field(default_factory=list)
    images: list[DestinationSemantics] = field(default_factory=list)
    lock_states: list[bool] = field(default_factory=list)
    ingress_lock_intervals: list[tuple[bool, bool]] = field(default_factory=list)
    authorization_lock_intervals: list[tuple[bool, bool]] = field(
        default_factory=list
    )
    lock_transitions: list[str] = field(default_factory=list)
    admission_depth: int = 0
    admission_unlocks: int = 0
    inspection_results: list[RecipeCandidate] = field(default_factory=list)
    authorization_receivers: list[RecipeCandidate] = field(default_factory=list)
    authorization_results: list[AuthorizedRecipe] = field(default_factory=list)
    authorized: AuthorizedRecipe | None = None
    complete: bool = False


def _destination_semantics(
    expected: DestinationSemantics,
    read_bytes: Callable[[Path], bytes] = Path.read_bytes,
) -> DestinationSemantics:
    observed: DestinationSemantics = {}
    for path in expected:
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode)
        observed[path] = (read_bytes(path), stat.S_IMODE(info.st_mode))
    return observed


def _is_exact_destination_call(
    destinations: tuple[Path, ...], destination: object, directory_fd: int | None
) -> bool:
    if directory_fd is None:
        return Path(os.fsdecode(destination)).resolve() in destinations
    leaf = os.fsdecode(destination)
    directory_info = os.fstat(directory_fd)
    return any(
        path.name == leaf
        and (path.parent.stat().st_dev, path.parent.stat().st_ino)
        == (directory_info.st_dev, directory_info.st_ino)
        for path in destinations
    )


def _durably_restore_source(source: Path, content: bytes) -> None:
    source.write_bytes(content)
    descriptor = os.open(source, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent = os.open(source.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _owner_lock_identity(owner_image: dict[str, NamespaceEntry]) -> tuple[int, int]:
    candidates = {
        (entry.device, entry.inode)
        for entry in owner_image.values()
        if entry.kind == "regular" and entry.mode == 0o600 and entry.content == b""
    }
    assert len(candidates) == 1
    return next(iter(candidates))


def _crash_after_first_destination(
    monkeypatch: pytest.MonkeyPatch,
    publisher: AuthoringPublisher,
    bundle: ProjectCompilationBundle,
) -> None:
    destinations = tuple(image.resolved_path for image in bundle.after_images)
    original_replace = os.replace
    replacements = 0

    def replace_then_die(source_path, destination_path, *args, **kwargs):
        nonlocal replacements
        result = original_replace(source_path, destination_path, *args, **kwargs)
        if _is_exact_destination_call(
            destinations, destination_path, kwargs.get("dst_dir_fd")
        ):
            replacements += 1
            raise _SimulatedProcessDeath("canonical transaction died after rename")
        return result

    with monkeypatch.context() as crash:
        crash.setattr(os, "replace", replace_then_die)
        with pytest.raises(_SimulatedProcessDeath):
            publisher.publish(bundle)
    assert replacements == 1


def _assert_one_mixed_destination(
    bundle: ProjectCompilationBundle, before: DestinationSemantics
) -> None:
    after = _destination_semantics(before)
    changed = {
        image.resolved_path
        for image in bundle.after_images
        if image.content is not None
        and image.mode is not None
        and after[image.resolved_path] == (image.content, image.mode)
        and after[image.resolved_path] != before[image.resolved_path]
    }
    assert len(changed) == 1
    assert all(path in changed or after[path] == before[path] for path in before)


def _prepare_mixed_canonical_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _MixedCanonicalScenario:
    project = tmp_path / "project"
    project.mkdir()
    owner_state = (tmp_path / "owner-state").resolve()
    source = write_workflow(project, "leaf")
    publish_project_compilation(project, "leaf", state_dir=owner_state)
    original_source = source.read_bytes()
    replace_marker(source, "initial", "changed-for-mixed-publication")
    bundle = plan_project_compilation(project_paths(project, "leaf"))
    destinations = tuple(image.resolved_path for image in bundle.after_images)
    before = {
        image.resolved_path: (image.content, image.mode)
        for image in bundle.before_images
        if image.content is not None and image.mode is not None
    }
    assert set(before) == set(destinations)
    owner_before = namespace_image(owner_state)
    _crash_after_first_destination(
        monkeypatch, AuthoringPublisher(owner_state), bundle
    )
    _assert_one_mixed_destination(bundle, before)
    owner_after = namespace_image(owner_state)
    assert any(
        path not in owner_before
        and entry.kind == "regular"
        and entry.mode == 0o600
        and bool(entry.content)
        for path, entry in owner_after.items()
    )
    _durably_restore_source(source, original_source)
    return _MixedCanonicalScenario(
        project,
        owner_state,
        source,
        bundle,
        before,
        _owner_lock_identity(owner_after),
    )


def _tracked_flock(
    observation: _CanonicalObservation,
    original: Callable[[int, int], None],
) -> Callable[[int, int], None]:
    def track(descriptor: int, operation: int) -> None:
        info = os.fstat(descriptor)
        result = original(descriptor, operation)
        if (
            threading.get_ident() == observation.reader_thread
            and (info.st_dev, info.st_ino) == observation.lock_identity
        ):
            transition = "release" if operation & fcntl.LOCK_UN else "acquire"
            if not observation.complete:
                observation.lock_transitions.append(transition)
            if operation & fcntl.LOCK_UN and observation.admission_depth:
                observation.admission_unlocks += 1
            observation.lock_held = not bool(operation & fcntl.LOCK_UN)
        return result

    return track


def _observed_read_bytes(
    observation: _CanonicalObservation,
    original: Callable[[Path], bytes],
) -> Callable[[Path], bytes]:
    def read(path: Path) -> bytes:
        resolved = path.resolve()
        if not observation.complete and resolved in observation.exact_paths:
            observation.reads.append(resolved)
            observation.images.append(
                _destination_semantics(observation.expected_before, original)
            )
            observation.lock_states.append(observation.lock_held)
        return original(path)

    return read


def _observed_ingress(
    observation: _CanonicalObservation,
    original: Callable[..., RecipeCandidate],
) -> Callable[..., RecipeCandidate]:
    def inspect(ingress: StrictRecipeIngress, root: str) -> RecipeCandidate:
        started_locked = observation.lock_held
        observation.admission_depth += 1
        try:
            candidate = original(ingress, root)
            observation.inspection_results.append(candidate)
            return candidate
        finally:
            observation.admission_depth -= 1
            observation.ingress_lock_intervals.append(
                (started_locked, observation.lock_held)
            )

    return inspect


def _observed_authorization(
    observation: _CanonicalObservation,
    original: Callable[..., AuthorizedRecipe],
) -> Callable[..., AuthorizedRecipe]:
    def authorize(candidate: RecipeCandidate, policy: object) -> AuthorizedRecipe:
        started_locked = observation.lock_held
        observation.admission_depth += 1
        observation.authorization_receivers.append(candidate)
        try:
            authorized = original(candidate, policy)
            observation.authorization_results.append(authorized)
            return authorized
        finally:
            observation.admission_depth -= 1
            observation.authorization_lock_intervals.append(
                (started_locked, observation.lock_held)
            )

    return authorize


def _stop_before_runtime(
    observation: _CanonicalObservation,
) -> Callable[..., dict[str, object]]:
    def stop(
        _service: LockstepCommandService,
        _recipe: str,
        authorized,
        _input,
        _project: str,
        *,
        compiler_provenance=None,
    ) -> dict[str, object]:
        del compiler_provenance
        observation.authorized = authorized
        observation.complete = True
        return {
            "status": "canonical-admission-observed",
            "run_id": "canonical-recovery-probe",
        }

    return stop


def _install_canonical_observer(
    monkeypatch: pytest.MonkeyPatch, scenario: _MixedCanonicalScenario
) -> _CanonicalObservation:
    observation = _CanonicalObservation(
        scenario.before,
        frozenset(
            (scenario.source.resolve(),)
            + tuple(image.resolved_path for image in scenario.bundle.after_images)
        ),
        scenario.lock_identity,
    )
    original_flock = fcntl.flock
    original_read_bytes = Path.read_bytes
    original_inspect = StrictRecipeIngress.inspect
    original_authorize = RecipeCandidate.authorize
    monkeypatch.setattr(fcntl, "flock", _tracked_flock(observation, original_flock))
    monkeypatch.setattr(
        Path, "read_bytes", _observed_read_bytes(observation, original_read_bytes)
    )
    monkeypatch.setattr(
        StrictRecipeIngress,
        "inspect",
        _observed_ingress(observation, original_inspect),
    )
    monkeypatch.setattr(
        RecipeCandidate,
        "authorize",
        _observed_authorization(observation, original_authorize),
    )
    monkeypatch.setattr(
        LockstepCommandService,
        "start_authorized",
        _stop_before_runtime(observation),
    )
    return observation


def _invoke_public_start(
    adapter: str,
    scenario: _MixedCanonicalScenario,
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    if adapter == "service":
        command = LockstepCommandService(
            scenario.owner_state, scenario.project / ".lockstep" / "recipes"
        )
        try:
            return command.start("leaf", {}, str(scenario.project))
        finally:
            command.close()
    if adapter == "mcp":
        monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(scenario.owner_state))
        monkeypatch.delenv("LOCKSTEP_RECIPES", raising=False)
        server._reset_engine()
        try:
            return server.scenario_start("leaf", {}, ctx=mcp_context(scenario.project))
        finally:
            server._reset_engine()
    raise ValueError(f"unknown public start adapter: {adapter}")


@pytest.mark.parametrize("adapter", ("service", "mcp"))
def test_public_start_recovers_before_locked_canonical_ingress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: str,
) -> None:
    scenario = _prepare_mixed_canonical_scenario(tmp_path, monkeypatch)
    observation = _install_canonical_observer(monkeypatch, scenario)
    raised: BaseException | None = None
    result: dict | None = None

    try:
        result = _invoke_public_start(adapter, scenario, monkeypatch)
    except BaseException as exc:
        raised = exc

    assert observation.reads, "public start never reached canonical project ingress"
    assert all(image == scenario.before for image in observation.images), (
        "public start observed canonical project bytes before authoring recovery"
    )
    assert all(observation.lock_states), (
        "public start observed canonical project bytes without the authoring lock"
    )
    assert observation.ingress_lock_intervals
    assert all(
        started and completed
        for started, completed in observation.ingress_lock_intervals
    ), "descriptor-based recipe ingress escaped the authoring lock"
    assert observation.authorization_lock_intervals
    assert all(
        started and completed
        for started, completed in observation.authorization_lock_intervals
    ), "AuthorizedRecipe construction escaped the authoring lock"
    assert observation.admission_unlocks == 0
    assert len(observation.inspection_results) == 1
    assert len(observation.authorization_receivers) == 1
    assert len(observation.authorization_results) == 1
    assert observation.authorization_receivers[0] is observation.inspection_results[0]
    assert observation.authorized is not None
    assert observation.authorized.canonical_match_proof is not None
    assert (
        replace(observation.authorized, canonical_match_proof=None)
        == observation.authorization_results[0]
    )
    assert observation.lock_transitions in (
        ["acquire"],
        ["acquire", "release"],
    ), "recovery and canonical capture used separate authoring lock intervals"
    assert raised is None
    assert observation.complete
    assert result is not None
    assert result["run_id"] == "canonical-recovery-probe"
