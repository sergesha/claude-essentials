"""Canonical start admission must share the authoring recovery boundary."""

from __future__ import annotations

import fcntl
import os
import stat
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from lockstep.authoring import project_paths, publish_project_compilation
from lockstep.authoring_bundle import ProjectCompilationBundle, plan_project_compilation
from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.mcp import server
from lockstep.recipe.authority import (
    AuthorizedRecipe,
    RecipeCandidate,
    RecipeAuthorityError,
    StrictRecipeIngress,
)
from lockstep.runtime.engine import LockstepError
from lockstep.runtime.service import LockstepCommandService
from lockstep.runtime.start_service import AuthorizedStartPlan, AuthorizedStartService

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


@dataclass(frozen=True, slots=True)
class _ObservedInspection:
    result: RecipeCandidate
    started_locked: bool
    completed_locked: bool

    @property
    def locked(self) -> bool:
        return self.started_locked and self.completed_locked


@dataclass(frozen=True, slots=True)
class _ObservedAuthorization:
    receiver: RecipeCandidate
    result: AuthorizedRecipe
    started_locked: bool
    completed_locked: bool

    @property
    def locked(self) -> bool:
        return self.started_locked and self.completed_locked


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
    inspections: list[_ObservedInspection] = field(default_factory=list)
    authorizations: list[_ObservedAuthorization] = field(default_factory=list)
    lock_transitions: list[str] = field(default_factory=list)
    lock_started: bool = False
    lock_completed: bool = False
    post_lock_admissions: int = 0
    admission_depth: int = 0
    admission_unlocks: int = 0
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


def _prepare_stable_canonical_scenario(
    tmp_path: Path,
    *,
    owner_name: str = "owner-state",
) -> _MixedCanonicalScenario:
    project = tmp_path / "project"
    project.mkdir()
    owner_state = (tmp_path / owner_name).resolve()
    source = write_workflow(project, "leaf")
    publish_project_compilation(project, "leaf", state_dir=owner_state)
    bundle = plan_project_compilation(project_paths(project, "leaf"))
    before = {
        image.resolved_path: (image.content, image.mode)
        for image in bundle.before_images
        if image.content is not None and image.mode is not None
    }
    return _MixedCanonicalScenario(
        project,
        owner_state,
        source,
        bundle,
        before,
        _owner_lock_identity(namespace_image(owner_state)),
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
            if operation & fcntl.LOCK_UN:
                if observation.lock_started:
                    observation.lock_completed = True
            else:
                observation.lock_started = True
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
        if observation.lock_completed:
            observation.post_lock_admissions += 1
        observation.admission_depth += 1
        try:
            candidate = original(ingress, root)
            observation.inspections.append(
                _ObservedInspection(candidate, started_locked, observation.lock_held)
            )
            return candidate
        finally:
            observation.admission_depth -= 1

    return inspect


def _observed_authorization(
    observation: _CanonicalObservation,
    original: Callable[..., AuthorizedRecipe],
) -> Callable[..., AuthorizedRecipe]:
    def authorize(candidate: RecipeCandidate, policy: object) -> AuthorizedRecipe:
        started_locked = observation.lock_held
        if observation.lock_completed:
            observation.post_lock_admissions += 1
        observation.admission_depth += 1
        try:
            authorized = original(candidate, policy)
            observation.authorizations.append(
                _ObservedAuthorization(
                    candidate, authorized, started_locked, observation.lock_held
                )
            )
            return authorized
        finally:
            observation.admission_depth -= 1

    return authorize


def _stop_before_runtime(
    observation: _CanonicalObservation,
) -> Callable[..., dict[str, object]]:
    def stop(
        _service: AuthorizedStartService,
        _recipe: str,
        plan: AuthorizedStartPlan,
        _values,
        *,
        canonical_input: bytes,
    ) -> dict[str, object]:
        del canonical_input
        observation.authorized = plan.authorized
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
        AuthorizedStartService,
        "start",
        _stop_before_runtime(observation),
    )
    return observation


def _invoke_public_start(
    adapter: str,
    project: Path,
    owner_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str = "leaf",
) -> dict:
    if adapter == "service":
        command = LockstepCommandService(owner_state, project / ".lockstep" / "recipes")
        try:
            return command.start(name, {}, str(project))
        finally:
            command.close()
    if adapter == "mcp":
        monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))
        monkeypatch.delenv("LOCKSTEP_RECIPES", raising=False)
        server._reset_engine()
        try:
            return server.scenario_start(name, {}, ctx=mcp_context(project))
        finally:
            server._reset_engine()
    raise ValueError(f"unknown public start adapter: {adapter}")


def _write_denied_python_recipe(project: Path, sentinel: Path) -> str:
    name = "denied-python"
    module = "canonical_recovery_attacker"
    (project / f"{module}.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('imported')\n"
        "def run(state): return state\n",
        encoding="utf-8",
    )
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True)
    (recipes / f"{name}.recipe.yaml").write_text(
        f"name: {name}\n"
        "tools:\n"
        f"  code: {{type: python, module: {module}, function: run}}\n"
        "nodes: {code: {type: python, tool: code}}\n"
        "edges: [{from: START, to: code}, {from: code, to: END}]\n",
        encoding="utf-8",
    )
    return name


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
        result = _invoke_public_start(
            adapter, scenario.project, scenario.owner_state, monkeypatch
        )
    except BaseException as exc:
        raised = exc

    assert observation.reads, "public start never reached canonical project ingress"
    locked_images = tuple(
        image
        for image, locked in zip(
            observation.images, observation.lock_states, strict=True
        )
        if locked
    )
    assert locked_images, "public start never read canonical project bytes under lock"
    assert all(image == scenario.before for image in locked_images), (
        "locked canonical ingress observed project bytes before recovery"
    )
    locked_inspections = tuple(item for item in observation.inspections if item.locked)
    locked_authorizations = tuple(
        item for item in observation.authorizations if item.locked
    )
    assert len(locked_inspections) == len(observation.inspections)
    assert len(locked_authorizations) == len(observation.authorizations)
    assert locked_inspections, "no descriptor ingress completed under the authoring lock"
    assert len(locked_authorizations) == 1
    locked_authorization = locked_authorizations[0]
    assert any(
        locked_authorization.receiver is inspection.result
        for inspection in locked_inspections
    )
    assert observation.admission_unlocks == 0
    assert observation.post_lock_admissions == 0
    assert observation.authorized is not None
    assert observation.authorized.canonical_match_proof is not None
    assert observation.authorized.root == "leaf.recipe.yaml"
    assert (
        observation.authorized.source_bundle_sha256
        == observation.authorized.canonical_match_proof.source_bundle_sha256
    )
    assert (
        replace(observation.authorized, canonical_match_proof=None)
        == locked_authorization.result
    )
    assert observation.lock_transitions in (
        ["acquire"],
        ["acquire", "release"],
    ), "recovery and canonical capture used separate authoring lock intervals"
    assert raised is None
    assert observation.complete
    assert result is not None
    assert result["run_id"] == "canonical-recovery-probe"


@pytest.mark.parametrize("adapter", ("service", "mcp"))
def test_public_start_denies_python_before_owner_state_import_or_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    owner_state = tmp_path / "owner-state"
    sentinel = project / "ATTACKER-IMPORTED"
    module = "canonical_recovery_attacker"
    name = _write_denied_python_recipe(project, sentinel)
    monkeypatch.syspath_prepend(str(project))
    sys.modules.pop(module, None)
    runtime_effects: list[AuthorizedRecipe] = []

    def record_runtime(
        _service: LockstepCommandService,
        _recipe: str,
        authorized: AuthorizedRecipe,
        *_args,
        **_kwargs,
    ) -> dict:
        runtime_effects.append(authorized)
        raise AssertionError("denied recipe reached runtime admission")

    monkeypatch.setattr(LockstepCommandService, "start_authorized", record_runtime)

    with pytest.raises(LockstepError, match="executable authority denied"):
        _invoke_public_start(
            adapter, project, owner_state, monkeypatch, name=name
        )

    assert not owner_state.exists()
    assert module not in sys.modules
    assert not sentinel.exists()
    assert runtime_effects == []


@pytest.mark.parametrize("adapter", ("service", "mcp"))
def test_public_start_uses_ready_boundary_for_one_locked_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: str,
) -> None:
    scenario = _prepare_stable_canonical_scenario(tmp_path)
    observation = _install_canonical_observer(monkeypatch, scenario)

    result = _invoke_public_start(
        adapter, scenario.project, scenario.owner_state, monkeypatch
    )

    assert observation.inspections
    assert len(observation.authorizations) == 1
    assert observation.reads
    assert all(observation.lock_states)
    assert all(item.locked for item in observation.inspections)
    assert observation.authorizations[0].locked
    assert observation.admission_unlocks == 0
    assert observation.post_lock_admissions == 0
    assert observation.lock_transitions == ["acquire", "release"]
    assert observation.authorized is not None
    assert (
        replace(observation.authorized, canonical_match_proof=None)
        == observation.authorizations[0].result
    )
    assert result["run_id"] == "canonical-recovery-probe"


@pytest.mark.parametrize("adapter", ("service", "mcp"))
@pytest.mark.parametrize("optimistic_outcome", ("success", "failure"))
def test_public_start_discards_optimistic_result_when_boundary_appears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: str,
    optimistic_outcome: str,
) -> None:
    scenario = _prepare_stable_canonical_scenario(
        tmp_path, owner_name="publisher-state"
    )
    owner_state = (tmp_path / "start-state").resolve()
    optimistic = replace(scenario, owner_state=owner_state, lock_identity=(-1, -1))
    observation = _install_canonical_observer(monkeypatch, optimistic)
    observed_authorize = RecipeCandidate.authorize
    publication_count = 0

    def publish_after_first_authorization(
        candidate: RecipeCandidate, policy: object
    ) -> AuthorizedRecipe:
        nonlocal publication_count
        authorized = observed_authorize(candidate, policy)
        if publication_count == 0:
            publication_count += 1
            replace_marker(
                scenario.source, "initial", "changed-during-start-admission"
            )
            changed = plan_project_compilation(
                project_paths(scenario.project, "leaf")
            )
            AuthoringPublisher(owner_state).publish(changed)
            observation.lock_identity = _owner_lock_identity(
                namespace_image(owner_state)
            )
            if optimistic_outcome == "failure":
                raise RecipeAuthorityError(
                    "canonical image changed during optimistic admission"
                )
        return authorized

    monkeypatch.setattr(
        RecipeCandidate, "authorize", publish_after_first_authorization
    )

    result = _invoke_public_start(adapter, scenario.project, owner_state, monkeypatch)

    assert publication_count == 1
    assert len(observation.authorizations) == 2
    optimistic_authorization, locked_authorization = observation.authorizations
    assert not optimistic_authorization.locked
    assert locked_authorization.locked
    assert optimistic_authorization.result != locked_authorization.result
    locked_inspections = tuple(
        item for item in observation.inspections if item.locked
    )
    assert locked_inspections
    assert any(
        locked_authorization.receiver is inspection.result
        for inspection in locked_inspections
    )
    assert any(observation.lock_states)
    first_locked_read = observation.lock_states.index(True)
    assert all(observation.lock_states[first_locked_read:])
    assert observation.admission_unlocks == 0
    assert observation.post_lock_admissions == 0
    assert observation.authorized is not None
    assert (
        replace(observation.authorized, canonical_match_proof=None)
        == locked_authorization.result
    )
    assert observation.lock_transitions == ["acquire", "release"]
    assert result["run_id"] == "canonical-recovery-probe"


@pytest.mark.parametrize("adapter", ("service", "mcp"))
def test_public_start_uses_one_optimistic_plan_while_boundary_remains_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: str,
) -> None:
    scenario = _prepare_stable_canonical_scenario(
        tmp_path, owner_name="publisher-state"
    )
    owner_state = (tmp_path / "start-state").resolve()
    optimistic = replace(scenario, owner_state=owner_state, lock_identity=(-1, -1))
    observation = _install_canonical_observer(monkeypatch, optimistic)

    result = _invoke_public_start(adapter, scenario.project, owner_state, monkeypatch)

    assert observation.inspections
    assert len(observation.authorizations) == 1
    assert all(not item.locked for item in observation.inspections)
    assert not observation.authorizations[0].locked
    assert observation.authorized is not None
    assert (
        replace(observation.authorized, canonical_match_proof=None)
        == observation.authorizations[0].result
    )
    assert not (owner_state / "authoring").exists()
    assert result["run_id"] == "canonical-recovery-probe"


@pytest.mark.parametrize("adapter", ("service", "mcp"))
def test_public_start_rejects_unready_boundary_without_reader_side_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: str,
) -> None:
    scenario = _prepare_stable_canonical_scenario(
        tmp_path, owner_name="publisher-state"
    )
    owner_state = (tmp_path / "start-state").resolve()
    journal, _identity = AuthoringJournal.create_for_project(
        owner_state, scenario.project
    )
    assert not (journal.directory / "transaction.lock").exists()
    before = namespace_image(owner_state)
    unready = replace(scenario, owner_state=owner_state, lock_identity=(-1, -1))
    observation = _install_canonical_observer(monkeypatch, unready)
    raised: BaseException | None = None

    try:
        _invoke_public_start(adapter, scenario.project, owner_state, monkeypatch)
    except BaseException as exc:
        raised = exc

    assert isinstance(raised, LockstepError)
    assert namespace_image(owner_state) == before
    assert observation.reads == []
    assert observation.inspections == []
    assert observation.authorizations == []
    assert observation.authorized is None
