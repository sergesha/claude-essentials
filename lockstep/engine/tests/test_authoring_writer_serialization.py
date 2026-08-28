"""Public cooperating authoring writers serialize on one project-bound flock."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lockstep import authoring, templates
from lockstep.authoring import project_paths
from lockstep.authoring_bundle import ProjectCompilationBundle, plan_project_compilation
from lockstep.authoring_journal import AuthoringJournal, AuthoringRecoveryRequired
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.errors import AuthoringError
from tests._authoring_crash_gate import (
    NamespaceEntry,
    install_mutation_syscall_probe,
    namespace_image,
)
from tests._authoring_gate import replace_marker, write_workflow
from tests._authoring_writer_gate import (
    DurableMutationGate,
    FlockTrace,
    PlanningGate,
    SimulatedProcessDeath,
    ThreadMutationTrace,
    ThreadResults,
    wait,
)


@dataclass(frozen=True, slots=True)
class _CompilationScenario:
    project: Path
    owner: Path
    bundle: ProjectCompilationBundle
    project_before: dict[str, NamespaceEntry]
    owner_clean: dict[str, NamespaceEntry]
    lock_identity: tuple[int, int]

    @property
    def destinations(self) -> tuple[Path, ...]:
        return tuple(image.resolved_path for image in self.bundle.after_images)


def _lock_entry(owner: dict[str, NamespaceEntry]) -> NamespaceEntry:
    entries = [entry for path, entry in owner.items() if path.endswith("transaction.lock")]
    assert len(entries) == 1
    entry = entries[0]
    assert (entry.kind, entry.mode, entry.content) == ("regular", 0o600, b"")
    return entry


def _assert_expected_lock_path(
    owner: Path, project: Path, image: dict[str, NamespaceEntry]
) -> NamespaceEntry:
    journal, _identity = AuthoringJournal.locate_for_project(owner, project)
    assert journal is not None
    relative = (journal.directory / "transaction.lock").relative_to(owner).as_posix()
    assert {path for path in image if path.endswith("transaction.lock")} == {relative}
    return _lock_entry(image)


def _compilation_scenario(tmp_path: Path) -> _CompilationScenario:
    project = tmp_path / "project"
    source = write_workflow(project, "release", marker="old")
    source.chmod(0o640)
    owner = (tmp_path / "owner-state").resolve()
    owner.mkdir(mode=0o700)
    sentinel = project / "notes" / "sentinel.bin"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"sentinel\n")
    sentinel.chmod(0o640)
    authoring.publish_project_compilation(project, "release", state_dir=owner)
    replace_marker(source, "old", "new")
    source.chmod(0o640)
    bundle = plan_project_compilation(project_paths(project, "release"))
    owner_clean = namespace_image(owner)
    lock = _assert_expected_lock_path(owner, project, owner_clean)
    return _CompilationScenario(
        project, owner, bundle, namespace_image(project), owner_clean,
        (lock.device, lock.inode),
    )


def _preinitialize_owner(owner: Path, project: Path) -> dict[str, NamespaceEntry]:
    journal, _identity = AuthoringJournal.create_for_project(owner, project)
    with journal.locked():
        journal.require_inactive()
    observed = namespace_image(owner)
    _assert_expected_lock_path(owner, project, observed)
    return observed


def _precreate_public_template_directories(
    tmp_path: Path, project: Path, name: str
) -> None:
    """Mirror only directory shape learned from disposable public installs."""

    for template_name in ("reviewed-change", "parallel-review"):
        scratch = tmp_path / f"shape-{template_name}"
        scratch.mkdir()
        scratch_owner = (tmp_path / f"shape-owner-{template_name}").resolve()
        scratch_owner.mkdir(mode=0o700)
        templates.install_template(
            template_name, name, scratch, state_dir=scratch_owner
        )
        for directory in sorted(path for path in scratch.rglob("*") if path.is_dir()):
            (project / directory.relative_to(scratch)).mkdir(parents=True, exist_ok=True)


def _assert_bundle_projection(
    root: Path,
    before: dict[str, NamespaceEntry],
    *bundles: ProjectCompilationBundle,
) -> None:
    expected = dict(before)
    observed = namespace_image(root)
    for bundle in bundles:
        for image in bundle.after_images:
            relative = image.resolved_path.relative_to(root).as_posix()
            expected.pop(relative, None)
            entry = observed.pop(relative)
            assert image.content is not None and image.mode is not None
            assert (entry.kind, entry.content, entry.mode) == (
                "regular", image.content, image.mode
            )
    assert observed == expected


def _assert_all_old(scenario: _CompilationScenario) -> None:
    expected = dict(scenario.project_before)
    observed = namespace_image(scenario.project)
    for image in scenario.bundle.before_images:
        relative = image.resolved_path.relative_to(scenario.project).as_posix()
        old = expected.pop(relative)
        entry = observed.pop(relative)
        assert (entry.kind, entry.content, entry.mode) == (
            "regular", old.content, old.mode
        )
    assert observed == expected


def _assert_uncommitted_model(model: Any, scenario: _CompilationScenario) -> None:
    assert model.project.resolved_path == scenario.project.resolve()
    assert (model.project.device, model.project.inode) == (
        scenario.project.stat().st_dev,
        scenario.project.stat().st_ino,
    )
    assert model.committed is False and model.replacement_progress == ()
    assert tuple(entry.path for entry in model.write_set) == scenario.destinations
    assert model.reservation.operation_id == model.operation_id
    assert len(model.reservation.stages) == len(scenario.destinations)


def _assert_first_destination_crash(
    scenario: _CompilationScenario,
    model: Any,
    observed: dict[str, NamespaceEntry],
) -> None:
    expected = dict(scenario.project_before)
    actual = dict(observed)
    first = scenario.bundle.after_images[0]
    relative = first.resolved_path.relative_to(scenario.project).as_posix()
    expected.pop(relative)
    entry = actual.pop(relative)
    assert first.content is not None and first.mode is not None
    old_first = scenario.project_before[relative]
    assert (entry.kind, entry.content, entry.mode) == (
        "regular",
        first.content,
        first.mode,
    )
    assert entry.inode != old_first.inode
    for index in range(1, len(scenario.destinations)):
        stage = model.reservation.stages[index].publication
        stage_relative = stage.relative_to(scenario.project).as_posix()
        stage_entry = actual.pop(stage_relative)
        after = scenario.bundle.after_images[index]
        assert (stage_entry.kind, stage_entry.content, stage_entry.mode) == (
            "regular",
            after.content,
            after.mode,
        )
    assert actual == expected


def _assert_active_crash_owner(
    scenario: _CompilationScenario,
    observed: dict[str, NamespaceEntry],
) -> None:
    expected = dict(scenario.owner_clean)
    actual = dict(observed)
    journal, _identity = AuthoringJournal.locate_for_project(
        scenario.owner, scenario.project
    )
    assert journal is not None
    relative = journal.journal_path.relative_to(scenario.owner).as_posix()
    assert {path for path in actual if path.endswith("transaction.json")} == {
        relative
    }
    entry = actual.pop(relative)
    assert (entry.kind, entry.mode) == ("regular", 0o600)
    assert entry.content
    assert actual == expected


def _install_thread_mutation_trace(
    monkeypatch: pytest.MonkeyPatch,
    trace: FlockTrace,
    lock_identity: tuple[int, int],
) -> tuple[ThreadMutationTrace, dict[tuple[str, int], int]]:
    mutations = ThreadMutationTrace(frozenset({lock_identity}))
    acquisition_counts: dict[tuple[str, int], int] = {}

    def capture_acquisition(writer: str, ordinal: int) -> None:
        acquisition_counts[(writer, ordinal)] = len(mutations.events)

    trace.on_acquired = capture_acquisition
    mutations.install(monkeypatch)
    return mutations, acquisition_counts


def _assert_no_mutation_before_acquire(
    mutations: ThreadMutationTrace,
    acquisition_counts: dict[tuple[str, int], int],
    writer: str,
    ordinal: int,
    boundary: int,
) -> None:
    acquired = acquisition_counts[(writer, ordinal)]
    assert [
        event for event in mutations.events[boundary:acquired] if event[0] == writer
    ] == []


def _assert_no_mutation_since(
    mutations: ThreadMutationTrace,
    writer: str,
    boundary: int,
) -> None:
    assert [
        event for event in mutations.events[boundary:] if event[0] == writer
    ] == []


def _assert_noop_recovery_twice(
    project: Path,
    owner: Path,
    expected_project: dict[str, NamespaceEntry],
    expected_owner: dict[str, NamespaceEntry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _lock_entry(expected_owner)
    with monkeypatch.context() as probe:
        calls = install_mutation_syscall_probe(
            probe,
            allowed_write_open_identities=frozenset({(lock.device, lock.inode)}),
        )
        AuthoringPublisher(owner).recover(project)
        AuthoringPublisher(owner).recover(project)
    assert calls == []
    assert namespace_image(project) == expected_project
    assert namespace_image(owner) == expected_owner


def _assert_lock_schedule(
    trace: FlockTrace,
    expected: tuple[int, int],
    *,
    holder: str = "writer-a",
    holder_ordinal: int = 2,
    waiter: str = "writer-b",
    waiter_ordinal: int = 2,
) -> None:
    for writer in ("writer-a", "writer-b"):
        for ordinal in (1, 2):
            attempt = trace.event(writer, "attempt", ordinal)
            acquired = trace.event(writer, "acquired", ordinal)
            released = trace.event(writer, "released", ordinal)
            assert trace.before(attempt, acquired) and trace.before(acquired, released)
    assert len(trace.events) == 12
    assert all(
        (event.identity, event.mode, event.uid) == (expected, 0o600, os.getuid())
        for event in trace.events
    )
    holder_acquired = trace.event(holder, "acquired", holder_ordinal)
    waiter_attempt = trace.event(waiter, "attempt", waiter_ordinal)
    holder_released = trace.event(holder, "released", holder_ordinal)
    waiter_acquired = trace.event(waiter, "acquired", waiter_ordinal)
    assert trace.before(holder_acquired, waiter_attempt)
    assert trace.before(waiter_attempt, holder_released)
    assert trace.before(holder_released, waiter_acquired)


def _install_planner_gate(
    monkeypatch: pytest.MonkeyPatch,
    gate: PlanningGate,
    *, compilation: bool = False,
    template: bool = False,
) -> None:
    if compilation:
        monkeypatch.setattr(
            authoring, "_plan_project_compilation",
            gate.wrap(authoring._plan_project_compilation),
        )
    if template:
        monkeypatch.setattr(
            templates, "plan_template_installation",
            gate.wrap(templates.plan_template_installation),
        )


def _finish(results: ThreadResults, trace: FlockTrace, *events: threading.Event) -> None:
    for event in events:
        event.set()
    results.join_all(trace.events)


def test_overlapping_replacement_writers_commit_one_complete_preplanned_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _compilation_scenario(tmp_path)
    planning = PlanningGate(("writer-a", "writer-b"))
    trace = FlockTrace("writer-b", 2)
    mutation = DurableMutationGate("writer-a", scenario.destinations)
    _install_planner_gate(monkeypatch, planning, compilation=True)
    trace.install(monkeypatch)
    mutations = ThreadMutationTrace(frozenset({scenario.lock_identity}))
    mutations.install(monkeypatch)
    mutation.install(monkeypatch)
    results = ThreadResults()
    operation = lambda: authoring.publish_project_compilation(
        scenario.project, "release", state_dir=scenario.owner
    )
    b_mutation_boundary = 0
    try:
        results.start("writer-a", operation)
        results.start("writer-b", operation)
        planning.wait_for("writer-a", trace.events)
        planning.wait_for("writer-b", trace.events)
        planned_a = planning.values["writer-a"]
        planned_b = planning.values["writer-b"]
        assert planned_a is not planned_b
        assert planned_a == planned_b
        assert planned_a.bundle == scenario.bundle  # type: ignore[attr-defined]
        assert planned_b.bundle == scenario.bundle  # type: ignore[attr-defined]
        b_mutation_boundary = len(mutations.events)
        planning.release_writer("writer-a")
        wait(mutation.inside, trace.events)
        assert trace.holds("writer-a")
        planning.release_writer("writer-b")
        wait(trace.before_kernel, trace.events)
        trace.enter_kernel.set()
        trace.wait_attempt("writer-b", 2)
        assert not trace.paused_acquired.is_set()
        mutation.release.set()
    finally:
        planning.release_all()
        _finish(results, trace, trace.enter_kernel, mutation.release)
    assert not isinstance(results.values["writer-a"], BaseException)
    loser = results.values["writer-b"]
    assert type(loser) is AuthoringError
    assert str(loser) == "authoring destination identity changed after planning"
    _assert_no_mutation_since(mutations, "writer-b", b_mutation_boundary)
    _assert_lock_schedule(trace, scenario.lock_identity)
    _assert_bundle_projection(scenario.project, scenario.project_before, scenario.bundle)
    assert namespace_image(scenario.owner) == scenario.owner_clean
    _assert_noop_recovery_twice(
        scenario.project, scenario.owner, namespace_image(scenario.project),
        scenario.owner_clean, monkeypatch,
    )


def test_overlapping_distinguishable_template_installs_leave_one_complete_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    owner = (tmp_path / "owner").resolve()
    project.mkdir()
    owner.mkdir(mode=0o700)
    sentinel = project / "sentinel.bin"
    sentinel.write_bytes(b"keep\n")
    sentinel.chmod(0o640)
    _precreate_public_template_directories(tmp_path, project, "release")
    owner_clean = _preinitialize_owner(owner, project)
    project_before = namespace_image(project)
    lock = _assert_expected_lock_path(owner, project, owner_clean)
    planning = PlanningGate(("writer-a", "writer-b"))
    trace = FlockTrace("writer-b", 2)
    mutation = DurableMutationGate("writer-a", ())
    _install_planner_gate(monkeypatch, planning, template=True)
    trace.install(monkeypatch)
    lock_identity = (lock.device, lock.inode)
    mutations = ThreadMutationTrace(frozenset({lock_identity}))
    mutations.install(monkeypatch)
    mutation.install(monkeypatch)
    results = ThreadResults()
    b_mutation_boundary = 0
    try:
        results.start(
            "writer-a", lambda: templates.install_template(
                "reviewed-change", "release", project, state_dir=owner
            )
        )
        results.start(
            "writer-b", lambda: templates.install_template(
                "parallel-review", "release", project, state_dir=owner
            )
        )
        planning.wait_for("writer-a", trace.events)
        planning.wait_for("writer-b", trace.events)
        reviewed = planning.values["writer-a"].bundle  # type: ignore[attr-defined]
        parallel = planning.values["writer-b"].bundle  # type: ignore[attr-defined]
        assert all(
            image.content is None
            for bundle in (reviewed, parallel)
            for image in bundle.before_images
        )
        mutation.destinations = tuple(
            image.resolved_path
            for bundle in (reviewed, parallel)
            for image in bundle.after_images
        )
        b_mutation_boundary = len(mutations.events)
        planning.release_writer("writer-a")
        wait(mutation.inside, trace.events)
        planning.release_writer("writer-b")
        wait(trace.before_kernel, trace.events)
        trace.enter_kernel.set()
        trace.wait_attempt("writer-b", 2)
        assert not trace.paused_acquired.is_set()
        mutation.release.set()
    finally:
        planning.release_all()
        _finish(results, trace, trace.enter_kernel, mutation.release)
    assert not isinstance(results.values["writer-a"], BaseException)
    loser = results.values["writer-b"]
    assert type(loser) is AuthoringError
    assert str(loser) == "authoring destination was created after planning"
    _assert_no_mutation_since(mutations, "writer-b", b_mutation_boundary)
    _assert_lock_schedule(trace, lock_identity)
    _assert_bundle_projection(project, project_before, reviewed)
    losing_only = {
        image.resolved_path.relative_to(project).as_posix()
        for image in parallel.after_images
    } - {
        image.resolved_path.relative_to(project).as_posix()
        for image in reviewed.after_images
    }
    assert losing_only and losing_only.isdisjoint(namespace_image(project))
    assert namespace_image(owner) == owner_clean
    _assert_noop_recovery_twice(
        project, owner, namespace_image(project), owner_clean, monkeypatch
    )


def test_disjoint_replacement_and_template_writers_both_commit_under_one_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _compilation_scenario(tmp_path)
    _precreate_public_template_directories(tmp_path, scenario.project, "review")
    project_before = namespace_image(scenario.project)
    planning = PlanningGate(("writer-a", "writer-b"))
    trace = FlockTrace("writer-b", 2)

    def observe_destination(writer: str, primitive: str, ordinal: int) -> None:
        assert trace.holds(writer)
        trace.timeline.append((writer, f"destination-{primitive}", ordinal))

    mutation = DurableMutationGate(
        "writer-a",
        scenario.destinations,
        on_mutation=observe_destination,
    )
    _install_planner_gate(monkeypatch, planning, compilation=True, template=True)
    trace.install(monkeypatch)
    mutations, acquisition_counts = _install_thread_mutation_trace(
        monkeypatch, trace, scenario.lock_identity
    )
    mutation.install(monkeypatch)
    results = ThreadResults()
    b_mutation_boundary = 0
    try:
        results.start(
            "writer-a", lambda: authoring.write_compilation(
                project_paths(scenario.project, "release"), state_dir=scenario.owner
            )
        )
        results.start(
            "writer-b", lambda: templates.install_template(
                "parallel-review", "review", scenario.project,
                state_dir=scenario.owner,
            )
        )
        planning.wait_for("writer-a", trace.events)
        planning.wait_for("writer-b", trace.events)
        compilation = planning.values["writer-a"].bundle  # type: ignore[attr-defined]
        template = planning.values["writer-b"].bundle  # type: ignore[attr-defined]
        assert all(image.content is None for image in template.before_images)
        mutation.destinations = tuple(
            image.resolved_path
            for bundle in (compilation, template)
            for image in bundle.after_images
        )
        b_mutation_boundary = len(mutations.events)
        planning.release_writer("writer-a")
        wait(mutation.inside, trace.events)
        planning.release_writer("writer-b")
        wait(trace.before_kernel, trace.events)
        trace.enter_kernel.set()
        trace.wait_attempt("writer-b", 2)
        assert not trace.paused_acquired.is_set()
        mutation.release.set()
    finally:
        planning.release_all()
        _finish(results, trace, trace.enter_kernel, mutation.release)
    assert all(not isinstance(value, BaseException) for value in results.values.values())
    _assert_no_mutation_before_acquire(
        mutations, acquisition_counts, "writer-b", 2, b_mutation_boundary
    )
    _assert_lock_schedule(trace, scenario.lock_identity)
    b_mutations = [
        event
        for event in trace.timeline
        if event[0] == "writer-b" and event[1] == "destination-link"
    ]
    first_template_ordinal = len(compilation.after_images)
    assert b_mutations == [
        ("writer-b", "destination-link", ordinal)
        for ordinal in range(
            first_template_ordinal,
            first_template_ordinal + len(template.after_images),
        )
    ]
    b_acquired = trace.timeline.index(("writer-b", "acquired", 2))
    b_released = trace.timeline.index(("writer-b", "released", 2))
    assert all(
        b_acquired < trace.timeline.index(event) < b_released
        for event in b_mutations
    )
    _assert_bundle_projection(scenario.project, project_before, compilation, template)
    assert namespace_image(scenario.owner) == scenario.owner_clean
    _assert_noop_recovery_twice(
        scenario.project, scenario.owner, namespace_image(scenario.project),
        scenario.owner_clean, monkeypatch,
    )


def test_queued_recovery_replans_only_after_crashed_writer_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _compilation_scenario(tmp_path)
    trace = FlockTrace()
    recovered: dict[str, Any] = {}
    crash_evidence: dict[str, object] = {}

    def capture_crash_evidence() -> None:
        crash_evidence["events"] = tuple(mutation.events)
        crash_evidence["project"] = namespace_image(scenario.project)
        crash_evidence["owner"] = namespace_image(scenario.owner)

    mutation = DurableMutationGate(
        "writer-a",
        scenario.destinations,
        crash=True,
        on_crash=capture_crash_evidence,
    )
    original_read = AuthoringJournal.read_recovery_model
    original_plan = authoring._plan_project_compilation

    def read_model(journal: AuthoringJournal, **kwargs):
        model = original_read(journal, **kwargs)
        if threading.current_thread().name == "writer-b":
            recovered["model"] = model
            recovered["crash_project"] = namespace_image(scenario.project)
            recovered["crash_owner"] = namespace_image(scenario.owner)
            trace.timeline.append(("writer-b", "recovery-read", 1))
        return model

    def plan_after_recovery(*args, **kwargs):
        if threading.current_thread().name == "writer-b":
            _assert_all_old(scenario)
            assert namespace_image(scenario.owner) == scenario.owner_clean
            trace.timeline.append(("writer-b", "planner-read", 1))
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(AuthoringJournal, "read_recovery_model", read_model)
    monkeypatch.setattr(authoring, "_plan_project_compilation", plan_after_recovery)
    trace.install(monkeypatch)
    mutations, acquisition_counts = _install_thread_mutation_trace(
        monkeypatch, trace, scenario.lock_identity
    )
    mutation.install(monkeypatch)
    results = ThreadResults()
    operation = lambda: authoring.publish_project_compilation(
        scenario.project, "release", state_dir=scenario.owner
    )
    b_mutation_boundary = 0
    try:
        results.start("writer-a", operation)
        wait(mutation.inside, trace.events)
        b_mutation_boundary = len(mutations.events)
        results.start("writer-b", operation)
        trace.wait_attempt("writer-b", 1)
        acquired = trace.acquired.get(("writer-b", 1), threading.Event())
        assert not acquired.is_set() and trace.holds("writer-a")
        mutation.release.set()
    finally:
        _finish(results, trace, mutation.release)
    assert isinstance(results.values["writer-a"], SimulatedProcessDeath)
    assert not isinstance(results.values["writer-b"], BaseException)
    assert mutation.injected
    assert crash_evidence["events"] == (("writer-a", "replace", 0),)
    crash_project = crash_evidence["project"]
    crash_owner = crash_evidence["owner"]
    assert isinstance(crash_project, dict) and isinstance(crash_owner, dict)
    model = recovered["model"]
    _assert_first_destination_crash(scenario, model, crash_project)
    _assert_active_crash_owner(scenario, crash_owner)
    assert recovered["crash_project"] == crash_project
    assert recovered["crash_owner"] == crash_owner
    _assert_uncommitted_model(model, scenario)
    _assert_no_mutation_before_acquire(
        mutations, acquisition_counts, "writer-b", 1, b_mutation_boundary
    )
    _assert_lock_schedule(trace, scenario.lock_identity, waiter_ordinal=1)
    timeline = trace.timeline
    assert timeline.index(("writer-b", "recovery-read", 1)) < timeline.index(
        ("writer-b", "released", 1)
    )
    assert timeline.index(("writer-b", "released", 1)) < timeline.index(
        ("writer-b", "planner-read", 1)
    )
    assert timeline.index(("writer-b", "planner-read", 1)) < timeline.index(
        ("writer-b", "acquired", 2)
    )
    _assert_bundle_projection(scenario.project, scenario.project_before, scenario.bundle)
    assert namespace_image(scenario.owner) == scenario.owner_clean
    _assert_noop_recovery_twice(
        scenario.project, scenario.owner, namespace_image(scenario.project),
        scenario.owner_clean, monkeypatch,
    )


def test_preplanned_writer_preserves_later_crash_evidence_until_fresh_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _compilation_scenario(tmp_path)
    planning = PlanningGate(("writer-b",))
    trace = FlockTrace("writer-b", 2)
    mutations = ThreadMutationTrace(frozenset({scenario.lock_identity}))
    crash_evidence: dict[str, object] = {}

    def capture_crash_evidence() -> None:
        crash_evidence["events"] = tuple(mutation.events)
        crash_evidence["project"] = namespace_image(scenario.project)
        crash_evidence["owner"] = namespace_image(scenario.owner)

    mutation = DurableMutationGate(
        "writer-a",
        scenario.destinations,
        crash=True,
        on_crash=capture_crash_evidence,
    )
    _install_planner_gate(monkeypatch, planning, compilation=True)
    trace.install(monkeypatch)
    mutations.install(monkeypatch)
    mutation.install(monkeypatch)
    results = ThreadResults()
    operation = lambda: authoring.publish_project_compilation(
        scenario.project, "release", state_dir=scenario.owner
    )
    b_mutation_boundary = 0
    try:
        results.start("writer-b", operation)
        planning.wait_for("writer-b", trace.events)
        b_mutation_boundary = len(mutations.events)
        planning.release_writer("writer-b")
        wait(trace.before_kernel, trace.events)
        results.start("writer-a", operation)
        wait(mutation.inside, trace.events)
        assert trace.holds("writer-a")
        trace.enter_kernel.set()
        trace.wait_attempt("writer-b", 2)
        assert not trace.paused_acquired.is_set()
        mutation.release.set()
    finally:
        planning.release_all()
        _finish(results, trace, trace.enter_kernel, mutation.release)
    assert isinstance(results.values["writer-a"], SimulatedProcessDeath)
    failure = results.values["writer-b"]
    assert type(failure) is AuthoringRecoveryRequired
    assert str(failure) == "active authoring transaction requires recovery"
    b_mutations = [
        event
        for event in mutations.events[b_mutation_boundary:]
        if event[0] == "writer-b"
    ]
    assert b_mutations == []
    _assert_lock_schedule(trace, scenario.lock_identity)
    mixed_project = namespace_image(scenario.project)
    mixed_owner = namespace_image(scenario.owner)
    assert crash_evidence["events"] == (("writer-a", "replace", 0),)
    assert mixed_project == crash_evidence["project"]
    assert mixed_owner == crash_evidence["owner"]
    journal, identity = AuthoringJournal.locate_for_project(
        scenario.owner, scenario.project
    )
    assert journal is not None
    with journal.locked_existing():
        model = journal.read_recovery_model(expected_project=identity)
    _assert_uncommitted_model(model, scenario)
    assert model.project == identity
    _assert_first_destination_crash(scenario, model, mixed_project)
    _assert_active_crash_owner(scenario, mixed_owner)
    assert namespace_image(scenario.project) == mixed_project
    assert namespace_image(scenario.owner) == mixed_owner
    AuthoringPublisher(scenario.owner).recover(scenario.project)
    _assert_all_old(scenario)
    assert namespace_image(scenario.owner) == scenario.owner_clean
    _assert_noop_recovery_twice(
        scenario.project, scenario.owner, namespace_image(scenario.project),
        scenario.owner_clean, monkeypatch,
    )
