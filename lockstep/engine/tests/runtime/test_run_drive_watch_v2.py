"""Task 12R0 Gate B0 behavioral REDs and independent B-schema contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from inspect import Parameter, signature
from pathlib import Path
import resource
import threading
from types import NoneType
from typing import get_type_hints

import pytest
from sqlalchemy import Integer, inspect as sa_inspect

from lockstep.runtime import sessions
from lockstep.runtime import engine_drive_service as engine_drive_module
from lockstep.runtime.effects.descriptors import (
    parse_effect_descriptor,
    parse_effect_result,
)
from lockstep.runtime.blobs import BlobRef
from lockstep.runtime.providers.base import TerminalSafetyObservation
from lockstep.runtime.providers.manual import ManualSubmission
from lockstep.runtime.service import LockstepCommandService
from lockstep.runtime.engine import Engine
from tests.runtime._legacy_run_drive_fixtures import (
    AutoGrantAuthority as _AutoGrantAuthority,
    compile_recipe as _compile,
    legacy_service as _legacy_service,
    stop_pump as _stop_pump,
)
from tests.runtime.providers.fakes import (
    FakeRunner,
    _legacy_command_service,
)


@dataclass(frozen=True)
class _DecisionCrash:
    state: Path
    recipes: Path
    project: Path
    run_id: str
    thread_id: str


def _bound_snapshot(service: LockstepCommandService, crash: _DecisionCrash):
    return _snapshot_existing(service, crash.run_id)


def _snapshot_existing(service: LockstepCommandService, run_id: str):
    service.runtime.bind(service.catalog.get(run_id))
    return service.runtime.snapshot(run_id, subgraphs=True)


def _run_drive_watches(service: LockstepCommandService):
    high_water = service.effects.max_run_drive_admission_seq()
    if high_water is None:
        return ()
    return service.effects.list_run_drive_watches(
        after_admission_seq=0,
        high_water=high_water,
        limit=128,
    )


def _create_manual_to_decision_park(
    tmp_path: Path,
) -> tuple[LockstepCommandService, _DecisionCrash]:
    recipes, compiled = _compile(
        tmp_path,
        "manual-decision",
        "  - step: edit\n"
        "    task: Edit the project\n"
        "    exit: Editing is complete\n"
        "  - decide:\n"
        "      id: risk\n"
        "      using:\n"
        "        type: changed-paths\n"
        "        since: start\n"
        "        cases: {high: [auth/**]}\n"
        "        default: low\n"
        "  - choose:\n"
        "      value: risk\n"
        "      cases:\n"
        "        high: [{escalate: {}}]\n"
        "        low: [{escalate: {}}]\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("unchanged\n")
    state = tmp_path / "state"
    service = _legacy_service(state, recipes)
    _stop_pump(service)
    started = service.start(
        "manual-decision",
        {},
        str(project),
        compiler_provenance=compiled.compiler_provenance,
    )
    run_id = started["run_id"]
    binding = service.catalog.get(run_id)
    manual = _snapshot_existing(service, run_id)
    assert len(manual.pending) == 1
    manual_descriptor = parse_effect_descriptor(
        manual.pending[0].value["lockstep_effect"]
    )
    assert manual_descriptor.kind == "manual"

    # Crash cut: seal/deliver the predecessor into native Decision state, but
    # do not call the service's subsequent engine drive.
    service.coordinator.submit_manual(
        run_id,
        manual.pending[0].coordinate,
        ManualSubmission.build("PASS", evidence={}),
    )
    decision = _snapshot_existing(service, run_id)
    assert len(decision.pending) == 1
    decision_descriptor = parse_effect_descriptor(
        decision.pending[0].value["lockstep_effect"]
    )
    assert decision_descriptor.__class__.__name__ == "DecisionDescriptor"
    effects = service.effects.list_for_thread(binding.thread_id)
    assert [record.effect_kind for record in effects] == ["manual"]
    assert effects[0].phase == "delivered"
    return service, _DecisionCrash(
        state,
        recipes,
        project,
        run_id,
        binding.thread_id,
    )


def _manual_to_decision_crash(tmp_path: Path) -> _DecisionCrash:
    """Create the historical acknowledged/no-watch b794 crash state."""

    service, crash = _create_manual_to_decision_park(tmp_path)
    service.effects.acknowledge_run_drive_watch(crash.run_id)
    assert _run_drive_watches(service) == ()
    service.close()
    return crash


def _normalized_facts(service: LockstepCommandService, crash: _DecisionCrash) -> dict:
    projection = Engine.observe(service.state_dir, service.recipes_dir)
    snapshot = _bound_snapshot(service, crash)
    effects = service.effects.list_for_thread(crash.thread_id)
    return {
        "catalog": service.catalog.get(crash.run_id),
        "watch": tuple(
            (item.public_run_id, item.input_blob_sha256, item.input_blob_size)
            for item in _run_drive_watches(service)
        ),
        "effects": tuple(
            (
                item.effect_id,
                item.effect_kind,
                item.phase,
                item.revision,
                item.result_ref,
            )
            for item in effects
        ),
        "checkpoint": snapshot.checkpoint_id,
        "pending": tuple((item.coordinate, item.value) for item in snapshot.pending),
        "next": snapshot.next,
        "values": snapshot.values,
        "history": tuple(
            (item["checkpoint_id"], item["status"])
            for item in projection.history(crash.run_id, str(crash.project))
        ),
        "events": tuple(
            tuple(sorted(item.items()))
            for item in projection.events(crash.run_id, str(crash.project))
        ),
    }


def test_recovery_consumes_rowless_decision_after_manual_delivery_crash(
    tmp_path: Path,
) -> None:
    crash = _manual_to_decision_crash(tmp_path)
    restarted = _legacy_service(crash.state, crash.recipes)
    try:
        _stop_pump(restarted)
        snapshot = _bound_snapshot(restarted, crash)
        effects = restarted.effects.list_for_thread(crash.thread_id)
        assert all(item.effect_kind != "decide" for item in effects)
        assert snapshot.pending == ()
        assert snapshot.values["lockstep_outcome"] == "FAIL"
    finally:
        restarted.close()


def _managed_park(tmp_path: Path, *, sealed: bool):
    recipes, compiled = _compile(
        tmp_path,
        "managed-park",
        "  - verify:\n"
        "      id: tests\n"
        "      command: pytest -q\n"
        "      timeout: 60\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    runner = FakeRunner()
    service = _legacy_command_service(
        tmp_path / "state",
        recipes,
        runners={"pinned": runner},
        effect_authority=_AutoGrantAuthority(),
    )
    _stop_pump(service)
    started = service.start(
        "managed-park", {}, str(project),
        compiler_provenance=compiled.compiler_provenance,
    )
    run_id = started["run_id"]
    record = service.effects.list_for_thread(service.catalog.get(run_id).thread_id)[0]
    assert record.phase == "running"
    if sealed:
        launch = runner.ensure_started_calls[-1]
        result = parse_effect_result({
            "schema": "lockstep.effect-result/v1",
            "effect_id": record.effect_id,
            "outcome": "PASS",
            "result_ref": "blob:" + "d" * 64,
            "artifact_refs": [],
            "snapshot_ref": None,
            "diff_ref": None,
            "fixed_error_code": None,
            "evidence_refs": [],
        })
        runner.inspect_observations.append(runner.terminal(launch, result))
        runner.safety_observations.append(
            TerminalSafetyObservation.proven_for(launch, result_stable=True)
        )
        for _ in range(4):
            service.coordinator.reconcile(run_id)
            record = service.effects.get(record.effect_id)
            if record.phase == "sealed":
                break
        assert record.phase == "sealed"
    return service, run_id


@pytest.mark.parametrize(
    "park",
    (
        "worker_manual",
        "managed_running",
        "sealed_external",
        "delivered_to_decision",
    ),
)
def test_drive_watch_survives_every_nonterminal_park(
    tmp_path: Path, park: str
) -> None:
    if park in {"managed_running", "sealed_external"}:
        service, run_id = _managed_park(
            tmp_path, sealed=park == "sealed_external"
        )
    elif park == "delivered_to_decision":
        service, crash = _create_manual_to_decision_park(tmp_path)
        run_id = crash.run_id
    else:
        recipes, compiled = _compile(
            tmp_path,
            "manual-park",
            "  - step: edit\n"
            "    task: Edit the project\n"
            "    exit: Editing is complete\n",
        )
        project = tmp_path / "project"
        project.mkdir()
        service = _legacy_service(tmp_path / "state", recipes)
        _stop_pump(service)
        started = service.start(
            "manual-park", {}, str(project),
            compiler_provenance=compiled.compiler_provenance,
        )
        run_id = started["run_id"]
    try:
        _stop_pump(service)
        assert _snapshot_existing(service, run_id).pending
        assert tuple(
            watch.public_run_id
            for watch in _run_drive_watches(service)
        ) == (run_id,)
    finally:
        service.close()


def test_watch_is_not_removed_at_nonterminal_manual_park(
    tmp_path: Path,
) -> None:
    recipes, compiled = _compile(
        tmp_path,
        "manual-lifetime",
        "  - step: edit\n"
        "    task: Edit the project\n"
        "    exit: Editing is complete\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    service = _legacy_service(tmp_path / "state", recipes)
    _stop_pump(service)
    started = service.start(
        "manual-lifetime", {}, str(project),
        compiler_provenance=compiled.compiler_provenance,
    )
    try:
        run_id = started["run_id"]
        snapshot = _snapshot_existing(service, run_id)
        assert snapshot.pending and snapshot.next
        assert tuple(
            watch.public_run_id
            for watch in _run_drive_watches(service)
        ) == (run_id,)
    finally:
        service.close()


def _blocked_then_decision_population(tmp_path: Path, *, revoke: bool = True):
    recipes, managed_compiled = _compile(
        tmp_path,
        "blocked-managed",
        "  - verify:\n"
        "      id: tests\n"
        "      command: pytest -q\n"
        "      timeout: 60\n",
    )
    _recipes, decision_compiled = _compile(
        tmp_path,
        "later-decision",
        "  - step: edit\n"
        "    task: Edit the project\n"
        "    exit: Editing is complete\n"
        "  - decide:\n"
        "      id: risk\n"
        "      using:\n"
        "        type: changed-paths\n"
        "        since: start\n"
        "        cases: {high: [auth/**]}\n"
        "        default: low\n"
        "  - choose:\n"
        "      value: risk\n"
        "      cases:\n"
        "        high: [{escalate: {}}]\n"
        "        low: [{escalate: {}}]\n",
    )
    _recipes, worker_compiled = _compile(
        tmp_path,
        "worker-only",
        "  - step: edit\n"
        "    task: Edit the project\n"
        "    exit: Editing is complete\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    foreign = tmp_path / "foreign-project"
    foreign.mkdir()
    authority = _AutoGrantAuthority()
    runner = FakeRunner()
    service = _legacy_command_service(
        tmp_path / "state", recipes,
        runners={"pinned": runner}, effect_authority=authority,
    )
    _stop_pump(service)
    foreign_park = service.start(
        "worker-only", {}, str(foreign),
        compiler_provenance=worker_compiled.compiler_provenance,
    )["run_id"]
    local_park = service.start(
        "worker-only", {}, str(project),
        compiler_provenance=worker_compiled.compiler_provenance,
    )["run_id"]
    assert _snapshot_existing(service, foreign_park).pending
    assert _snapshot_existing(service, local_park).pending
    managed = service.start(
        "blocked-managed", {}, str(project),
        compiler_provenance=managed_compiled.compiler_provenance,
    )
    managed_id = managed["run_id"]
    assert service.effects.list_for_thread(
        service.catalog.get(managed_id).thread_id
    )[0].phase == "running"
    intent_digest = authority.resolve_intents[-1].intent_digest
    if revoke:
        authority.revoke(intent_digest)
        authority.auto_authorize = False
    later = service.start(
        "later-decision", {}, str(project),
        compiler_provenance=decision_compiled.compiler_provenance,
    )
    later_id = later["run_id"]
    manual = _snapshot_existing(service, later_id)
    service.coordinator.submit_manual(
        later_id, manual.pending[0].coordinate,
        ManualSubmission.build("PASS", evidence={}),
    )
    assert _snapshot_existing(service, later_id).pending
    service._active_effect_runs.clear()  # noqa: SLF001 - fake capacity port
    service._queued_effect_runs.clear()  # noqa: SLF001
    service._active_effect_queue.clear()  # noqa: SLF001
    return service, project, managed_id, later_id, authority, runner


def _protected_action_trace(
    service: LockstepCommandService, managed_id: str, runner: FakeRunner
) -> dict:
    binding = service.catalog.get(managed_id)
    record = service.effects.list_for_thread(binding.thread_id)[0]
    return {
        "effect": (
            record.effect_id,
            record.request_digest,
            record.grant_digest,
            record.launch_commitment_digest,
            record.phase,
            record.revision,
        ),
        "runner_prepare": len(runner.prepare_calls),
        "runner_start": len(runner.ensure_started_calls),
        "runner_spawn": runner.spawn_count,
    }


@pytest.mark.parametrize("mode", ("explicit", "automatic"))
def test_watch_does_not_authorize_blocked_runner(
    tmp_path: Path, mode: str
) -> None:
    service, project, managed_id, later_id, _authority, runner = (
        _blocked_then_decision_population(tmp_path)
    )
    protected_before = _protected_action_trace(service, managed_id, runner)
    escaped = None
    try:
        try:
            if mode == "explicit":
                service.scenario_recover(str(project), limit=128)
            else:
                service._recover_engine_effects()  # noqa: SLF001
        except BaseException as exc:  # captured into the final trace oracle
            escaped = type(exc).__name__
        service.runtime.bind(service.catalog.get(later_id))
        target = _snapshot_existing(service, later_id)
        protected_after = _protected_action_trace(service, managed_id, runner)
        assert {
            "escaped": escaped,
            "protected_facts_unchanged": (
                protected_after["effect"] == protected_before["effect"]
            ),
            "new_runner_prepare": (
                protected_after["runner_prepare"]
                - protected_before["runner_prepare"]
            ),
            "new_runner_start": (
                protected_after["runner_start"]
                - protected_before["runner_start"]
            ),
            "new_runner_spawn": (
                protected_after["runner_spawn"]
                - protected_before["runner_spawn"]
            ),
            "later_pending": len(target.pending),
        } == {
            "escaped": None,
            "protected_facts_unchanged": True,
            "new_runner_prepare": 0,
            "new_runner_start": 0,
            "new_runner_spawn": 0,
            "later_pending": 0,
        }
    finally:
        service.close()


@pytest.mark.parametrize("mode", ("explicit", "automatic"))
def test_capacity_deferral_advances_current_sweep_and_preserves_next_eligibility(
    tmp_path: Path, mode: str
) -> None:
    service, project, managed_id, later_id, _authority, runner = (
        _blocked_then_decision_population(tmp_path, revoke=False)
    )
    protected_before = _protected_action_trace(service, managed_id, runner)
    # Capacity is the only faked decision. Catalog, effect, watch and native
    # populations above are real durable facts.
    service._active_effect_runs = {  # noqa: SLF001
        f"occupied-{index}" for index in range(service._MAX_ACTIVE_EFFECT_RUNS)
    }
    try:
        before = tuple(
            (item.effect_id, item.phase, item.revision)
            for binding in service.catalog.list(str(project.resolve()), limit=128)
            for item in service.effects.list_for_thread(binding.thread_id)
        )
        if mode == "explicit":
            service.scenario_recover(str(project), limit=128)
        else:
            service._recover_engine_effects()  # noqa: SLF001
        service.runtime.bind(service.catalog.get(later_id))
        current = _snapshot_existing(service, later_id)
        after = tuple(
            (item.effect_id, item.phase, item.revision)
            for binding in service.catalog.list(str(project.resolve()), limit=128)
            for item in service.effects.list_for_thread(binding.thread_id)
        )
        service._active_effect_runs.clear()  # noqa: SLF001
        if mode == "explicit":
            recovered = service.scenario_recover(str(project), limit=1)["recovered"]
            selected_next = recovered[0] if recovered else None
        else:
            service._recover_engine_effects()  # noqa: SLF001
            selected_next = (
                managed_id
                if managed_id in service._active_effect_runs  # noqa: SLF001
                else None
            )
        protected_after = _protected_action_trace(service, managed_id, runner)

        # One final trace proves both halves of the ordering contract.  b794
        # stops the first sweep at the capacity-full row, even though the same
        # row remains correctly eligible once capacity is released.
        assert {
            "later_pending": len(current.pending),
            "facts_after_deferral": after,
            "protected_facts_unchanged": (
                protected_after["effect"] == protected_before["effect"]
            ),
            "new_runner_prepare": (
                protected_after["runner_prepare"]
                - protected_before["runner_prepare"]
            ),
            "new_runner_start": (
                protected_after["runner_start"]
                - protected_before["runner_start"]
            ),
            "new_runner_spawn": (
                protected_after["runner_spawn"]
                - protected_before["runner_spawn"]
            ),
            "selected_next": selected_next,
        } == {
            "later_pending": 0,
            "facts_after_deferral": before,
            "protected_facts_unchanged": True,
            "new_runner_prepare": 0,
            "new_runner_start": 0,
            "new_runner_spawn": 0,
            "selected_next": managed_id,
        }
    finally:
        service.close()


@pytest.mark.parametrize("mode", ("explicit", "automatic"))
def test_explicit_and_automatic_recovery_fairness(
    tmp_path: Path, mode: str
) -> None:
    service, project, managed_id, later_id, _authority, runner = (
        _blocked_then_decision_population(tmp_path)
    )
    protected_before = _protected_action_trace(service, managed_id, runner)
    escaped = None
    try:
        try:
            if mode == "explicit":
                service.scenario_recover(str(project), limit=128)
            else:
                service._recover_engine_effects()  # noqa: SLF001
        except BaseException as exc:  # final trace records per-run isolation
            escaped = type(exc).__name__
        service.runtime.bind(service.catalog.get(later_id))
        later = _snapshot_existing(service, later_id)
        protected_after = _protected_action_trace(service, managed_id, runner)
        assert {
            "escaped": escaped,
            "protected_facts_unchanged": (
                protected_after["effect"] == protected_before["effect"]
            ),
            "new_runner_prepare": (
                protected_after["runner_prepare"]
                - protected_before["runner_prepare"]
            ),
            "new_runner_start": (
                protected_after["runner_start"]
                - protected_before["runner_start"]
            ),
            "new_runner_spawn": (
                protected_after["runner_spawn"]
                - protected_before["runner_spawn"]
            ),
            "later_pending": len(later.pending),
        } == {
            "escaped": None,
            "protected_facts_unchanged": True,
            "new_runner_prepare": 0,
            "new_runner_start": 0,
            "new_runner_spawn": 0,
            "later_pending": 0,
        }
    finally:
        service.close()


def test_start_watch_replays_only_before_first_checkpoint_non_null(
    tmp_path: Path,
) -> None:
    recipes, compiled = _compile(
        tmp_path,
        "manual-checkpoint",
        "  - step: edit\n"
        "    task: Edit the project\n"
        "    exit: Editing is complete\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    service = _legacy_service(tmp_path / "state", recipes)
    _stop_pump(service)

    # Real crash cut after atomic admission but before the native runtime has
    # established its first checkpoint.  The old non-null watch is the sole
    # restart input and must be replayed exactly once.
    real_start = service.runtime.ensure_started

    def crash_before_first_checkpoint(_run_id, _values):
        raise RuntimeError("crash before first checkpoint")

    service.runtime.ensure_started = crash_before_first_checkpoint
    with pytest.raises(RuntimeError, match="crash before first checkpoint"):
        service.start(
            "manual-checkpoint", {}, str(project),
            compiler_provenance=compiled.compiler_provenance,
        )
    service.runtime.ensure_started = real_start
    admission = _run_drive_watches(service)
    assert len(admission) == 1
    no_checkpoint_blob = BlobRef(
        admission[0].input_blob_sha256,
        admission[0].input_blob_size,
    )

    blob_reads = []
    starts = []
    real_read = service.blobs.read
    service.blobs.read = lambda ref: (blob_reads.append(ref), real_read(ref))[1]
    service.runtime.ensure_started = lambda rid, values: (
        starts.append((rid, values)), real_start(rid, values)
    )[1]
    service._recovery_driver._sweep_run_drive_watches(  # noqa: SLF001
        project_identity=None,
        limit=128,
    )
    before_checkpoint_trace = {
        "input_reads": sum(ref == no_checkpoint_blob for ref in blob_reads),
        "starts": len(starts),
    }

    blob_reads.clear()
    starts.clear()
    try:
        service.runtime.bind(service.catalog.get(admission[0].public_run_id))
        assert service.runtime.snapshot(
            admission[0].public_run_id,
            subgraphs=True,
        ).checkpoint_id
        service._recovery_driver._sweep_run_drive_watches(  # noqa: SLF001
            project_identity=None,
            limit=128,
        )
        assert {
            "before_checkpoint": before_checkpoint_trace,
            "after_checkpoint": {
                "input_reads": sum(
                    ref == no_checkpoint_blob for ref in blob_reads
                ),
                "starts": len(starts),
            },
        } == {
            "before_checkpoint": {"input_reads": 1, "starts": 1},
            "after_checkpoint": {"input_reads": 0, "starts": 0},
        }
    finally:
        service.close()


def test_fresh_driver_reaches_decision_after_128_worker_parks(
    tmp_path: Path,
) -> None:
    recipes, parked_compiled = _compile(
        tmp_path,
        "worker-park",
        "  - step: edit\n"
        "    task: Edit the project\n"
        "    exit: Editing is complete\n"
        "  - step: review\n"
        "    task: Review the project\n"
        "    exit: Review is complete\n",
    )
    _recipes, decision_compiled = _compile(
        tmp_path,
        "late-decision",
        "  - step: edit\n"
        "    task: Edit the project\n"
        "    exit: Editing is complete\n"
        "  - decide:\n"
        "      id: risk\n"
        "      using:\n"
        "        type: changed-paths\n"
        "        since: start\n"
        "        cases: {high: [auth/**]}\n"
        "        default: low\n"
        "  - choose:\n"
        "      value: risk\n"
        "      cases:\n"
        "        high: [{escalate: {}}]\n"
        "        low: [{escalate: {}}]\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    service = _legacy_service(state, recipes)
    _stop_pump(service)
    original_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    reduced_soft_limit = (
        64
        if original_limit[0] == resource.RLIM_INFINITY
        else min(original_limit[0], 64)
    )
    reduced_limit = (reduced_soft_limit, original_limit[1])
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, reduced_limit)
        parked_run_ids = []
        for _index in range(128):
            parked = service.start(
                "worker-park", {}, str(project),
                compiler_provenance=parked_compiled.compiler_provenance,
            )
            parked_id = parked["run_id"]
            parked_run_ids.append(parked_id)
            with pytest.raises(KeyError):
                service.runtime.binding(parked_id)
        for parked_id in parked_run_ids:
            sessions.touch(state, parked_id, "worker-session", 30)
            resumed = service.done(
                parked_id,
                "edit",
                {},
                session_id="worker-session",
                project=str(project),
            )
            assert (resumed["status"], resumed["owner"]) == ("awaiting", "worker")
            with pytest.raises(KeyError):
                service.runtime.binding(parked_id)
        late = service.start(
            "late-decision", {}, str(project),
            compiler_provenance=decision_compiled.compiler_provenance,
        )
        late_id = late["run_id"]
        late_binding = service.catalog.get(late_id)
        with pytest.raises(KeyError):
            service.runtime.binding(late_id)
        assert service.runtime.bind(late_binding) is True
        manual = service.runtime.snapshot(late_id, subgraphs=True)
        service.coordinator.submit_manual(
            late_id,
            manual.pending[0].coordinate,
            ManualSubmission.build("PASS", evidence={}),
        )
        assert service.runtime.snapshot(late_id, subgraphs=True).pending
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, original_limit)
        service.close()

    fresh = _legacy_service(state, recipes)
    try:
        _stop_pump(fresh)
        fresh.runtime.bind(late_binding)
        snapshot = fresh.runtime.snapshot(late_id, subgraphs=True)
        assert snapshot.pending == ()
    finally:
        fresh.close()


def test_start_drive_failure_releases_capacity_and_native_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes, compiled = _compile(
        tmp_path,
        "drive-failure",
        "  - step: edit\n"
        "    task: Edit the project\n"
        "    exit: Editing is complete\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    service = _legacy_service(tmp_path / "state", recipes)
    _stop_pump(service)
    driven = []

    def fail_drive(run_id: str, **_kwargs):
        driven.append(run_id)
        raise RuntimeError("drive failed")

    monkeypatch.setattr(service, "_drive_engine_owned", fail_drive)
    try:
        with pytest.raises(RuntimeError, match="drive failed"):
            service.start(
                "drive-failure", {}, str(project),
                compiler_provenance=compiled.compiler_provenance,
            )
        assert len(driven) == 1
        run_id = driven[0]
        assert run_id not in service._active_effect_runs  # noqa: SLF001
        assert run_id not in service._owned_effect_bindings  # noqa: SLF001
        with pytest.raises(KeyError):
            service.runtime.binding(run_id)
    finally:
        service.close()


def test_start_drive_failure_after_durable_launch_preserves_pump_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes, compiled = _compile(
        tmp_path,
        "drive-handoff-failure",
        "  - verify:\n"
        "      id: tests\n"
        "      command: pytest -q\n"
        "      timeout: 60\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    runner = FakeRunner()
    service = _legacy_command_service(
        tmp_path / "state",
        recipes,
        runners={"pinned": runner},
        effect_authority=_AutoGrantAuthority(),
    )
    _stop_pump(service)
    real_project_status = engine_drive_module.project_status
    observed_bindings = []
    injected_faults = []

    def fail_after_durable_handoff(binding, *args, **kwargs):
        observed_bindings.append(binding)
        records = service.effects.list_for_thread(binding.thread_id)
        running = len(records) == 1 and records[0].phase == "running"
        queued = binding.public_run_id in service._queued_effect_runs  # noqa: SLF001
        if running and queued and not injected_faults:
            injected_faults.append(binding.public_run_id)
            raise RuntimeError("post-handoff status failed")
        return real_project_status(binding, *args, **kwargs)

    monkeypatch.setattr(
        engine_drive_module,
        "project_status",
        fail_after_durable_handoff,
    )
    try:
        with pytest.raises(RuntimeError, match="post-handoff status failed"):
            service.start(
                "drive-handoff-failure", {}, str(project),
                compiler_provenance=compiled.compiler_provenance,
            )
        assert injected_faults == [observed_bindings[0].public_run_id]
        binding = observed_bindings[0]
        run_id = binding.public_run_id
        records = service.effects.list_for_thread(binding.thread_id)
        assert len(records) == 1
        assert records[0].phase == "running"
        assert len(runner.ensure_started_calls) == 1
        assert run_id in service._active_effect_runs  # noqa: SLF001
        assert run_id in service._queued_effect_runs  # noqa: SLF001
        assert run_id in service._owned_effect_bindings  # noqa: SLF001
        assert service.runtime.binding(run_id) == binding
    finally:
        service.close()


def test_periodic_pump_adopts_watch_after_pre_handoff_drive_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes, compiled = _compile(
        tmp_path,
        "periodic-pre-handoff-recovery",
        "  - step: edit\n"
        "    task: Edit the project\n"
        "    exit: Editing is complete\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    service = _legacy_service(tmp_path / "state", recipes)
    drives = []
    recoveries = []
    adopted = threading.Event()
    real_recovered_drive = service._recovery_driver._drive_recovered_run  # noqa: SLF001

    def fail_foreground_drive(run_id: str, **_kwargs):
        drives.append(run_id)
        raise RuntimeError("drive failed before handoff")

    def observe_recovered_drive(run_id: str) -> bool:
        result = real_recovered_drive(run_id)
        recoveries.append(run_id)
        adopted.set()
        return result

    monkeypatch.setattr(service, "_drive_engine_owned", fail_foreground_drive)
    monkeypatch.setattr(
        service._recovery_driver,  # noqa: SLF001
        "_drive_recovered_run",
        observe_recovered_drive,
    )
    try:
        with pytest.raises(RuntimeError, match="drive failed before handoff"):
            service.start(
                "periodic-pre-handoff-recovery", {}, str(project),
                compiler_provenance=compiled.compiler_provenance,
            )
        assert adopted.wait(2)
        assert recoveries == drives
        assert drives[0] not in service._active_effect_runs  # noqa: SLF001
    finally:
        service.close()


def test_b794_acknowledged_state_backfills_null_input_watch(tmp_path: Path) -> None:
    crash = _manual_to_decision_crash(tmp_path)
    restarted = _legacy_service(crash.state, crash.recipes)
    try:
        _stop_pump(restarted)
        snapshot = _bound_snapshot(restarted, crash)
        assert snapshot.pending == ()
        assert _run_drive_watches(restarted) == ()
    finally:
        restarted.close()


def test_repeated_recovery_is_idempotent(tmp_path: Path) -> None:
    crash = _manual_to_decision_crash(tmp_path)
    restarted = _legacy_service(crash.state, crash.recipes)
    try:
        _stop_pump(restarted)
        before = _normalized_facts(restarted, crash)
        restarted.scenario_recover(str(crash.project), limit=128)
        after_first = _normalized_facts(restarted, crash)
        restarted.scenario_recover(str(crash.project), limit=128)
        after_second = _normalized_facts(restarted, crash)
        assert before == after_first == after_second
        assert before["pending"] == ()
    finally:
        restarted.close()


# Exact B-schema missing-contract REDs; these make no behavioral claim.
def test_run_drive_watch_public_dto_contract() -> None:
    from lockstep.runtime.effects import ledger as ledger_module

    watch_type = getattr(ledger_module, "RunDriveWatch", None)
    assert watch_type is not None, "R2a must publish the exact RunDriveWatch DTO"
    assert tuple(watch_type.__dataclass_fields__) == (
        "admission_seq",
        "public_run_id",
        "input_blob_sha256",
        "input_blob_size",
        "admitted_at",
    )


def test_run_drive_watch_validates_frozen_value_domain() -> None:
    from lockstep.runtime.effects.ledger import RunDriveWatch

    utc_instant = datetime(2026, 8, 20, 10, tzinfo=UTC)
    migrated = RunDriveWatch(1, "run-1", None, None, utc_instant)
    assert migrated == RunDriveWatch(1, "run-1", None, None, utc_instant)

    offset_instant = datetime(
        2026,
        8,
        20,
        12,
        tzinfo=timezone(timedelta(hours=2)),
    )
    admitted = RunDriveWatch(2, "run-2", "a" * 64, 1, offset_instant)
    assert admitted.input_blob_size == 1
    assert admitted.admitted_at == utc_instant
    assert admitted.admitted_at.tzinfo is UTC
    zero_byte = RunDriveWatch(3, "run-3", "b" * 64, 0, utc_instant)
    assert zero_byte.input_blob_size == 0
    max_byte = RunDriveWatch(
        4,
        "run-4",
        "c" * 64,
        64 * 1024 * 1024,
        utc_instant,
    )
    assert max_byte.input_blob_size == 64 * 1024 * 1024

    for admission_seq in (0, -1, True, 1.0):
        with pytest.raises(
            ValueError,
            match="^admission_seq must be a positive integer$",
        ):
            RunDriveWatch(admission_seq, "run-1", None, None, utc_instant)
    for public_run_id in ("", 1):
        with pytest.raises(
            ValueError,
            match="^public_run_id must be a non-empty string$",
        ):
            RunDriveWatch(1, public_run_id, None, None, utc_instant)

    for digest, size in ((None, 1), ("a" * 64, None)):
        with pytest.raises(
            ValueError,
            match=(
                "^input blob digest and size must both be null or both be non-null$"
            ),
        ):
            RunDriveWatch(1, "run-1", digest, size, utc_instant)
    for digest in ("a" * 63, "A" * 64, "g" * 64, 1):
        with pytest.raises(
            ValueError,
            match="^input_blob_sha256 must be a lowercase SHA-256 digest$",
        ):
            RunDriveWatch(1, "run-1", digest, 1, utc_instant)
    for size in (-1, 64 * 1024 * 1024 + 1, True, 1.0):
        with pytest.raises(
            ValueError,
            match=(
                "^input_blob_size must be a non-negative integer "
                "not exceeding 64 MiB$"
            ),
        ):
            RunDriveWatch(1, "run-1", "a" * 64, size, utc_instant)

    for admitted_at in (datetime(2026, 8, 20, 10), "2026-08-20T10:00:00Z"):
        with pytest.raises(
            ValueError,
            match="^admitted_at must be a timezone-aware datetime$",
        ):
            RunDriveWatch(1, "run-1", None, None, admitted_at)


def test_run_drive_watch_ddl_contract(tmp_path: Path) -> None:
    from lockstep.runtime.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "runtime.db")
    try:
        table = getattr(store.tables, "run_drive_watches", None)
        assert table is not None, "R2a must replace the legacy watch table"
        assert tuple(table.c.keys()) == (
            "admission_seq", "public_run_id", "input_blob_sha256",
            "input_blob_size", "admitted_at",
        )
        assert table.c.admission_seq.primary_key
        assert isinstance(table.c.admission_seq.type, Integer)
        assert table.c.admission_seq.autoincrement is True
        assert table.c.public_run_id.unique
        assert not table.c.public_run_id.nullable
        assert table.c.input_blob_sha256.nullable
        assert table.c.input_blob_size.nullable
        assert not table.c.admitted_at.nullable
        assert len(table.c.public_run_id.foreign_keys) == 1
        checks = {
            "".join(item["sqltext"].lower().split())
            for item in sa_inspect(store.engine).get_check_constraints(
                "run_drive_watches"
            )
        }
        assert any(
            "input_blob_sha256isnull" in sql
            and "input_blob_sizeisnull" in sql
            and "input_blob_sha256isnotnull" in sql
            and "input_blob_sizeisnotnull" in sql
            and "or" in sql
            for sql in checks
        ), "blob digest and size must be null as a pair"
        assert table.dialect_options["sqlite"]["autoincrement"] is True
        assert "effect_dispatch_watches" not in store.metadata.tables
    finally:
        store.close()


def _exact_parameters(
    callable_value,
    expected: tuple[tuple[str, object], ...],
    expected_return,
) -> None:
    observed = tuple(
        (name, parameter.kind)
        for name, parameter in signature(callable_value).parameters.items()
    )
    assert observed == expected
    assert get_type_hints(callable_value)["return"] == expected_return


def test_run_drive_watch_high_water_api_exact_signature() -> None:
    from lockstep.runtime.effects.ledger import EffectLedger

    method = getattr(EffectLedger, "max_run_drive_admission_seq", None)
    assert callable(method)
    _exact_parameters(
        method,
        (("self", Parameter.POSITIONAL_OR_KEYWORD),),
        int | None,
    )


def test_run_drive_watch_page_api_exact_signature() -> None:
    from lockstep.runtime.effects import ledger as ledger_module
    from lockstep.runtime.effects.ledger import EffectLedger

    method = getattr(EffectLedger, "list_run_drive_watches", None)
    assert callable(method)
    watch_type = getattr(ledger_module, "RunDriveWatch", None)
    assert watch_type is not None
    _exact_parameters(
        method,
        (
            ("self", Parameter.POSITIONAL_OR_KEYWORD),
            ("after_admission_seq", Parameter.KEYWORD_ONLY),
            ("high_water", Parameter.KEYWORD_ONLY),
            ("limit", Parameter.KEYWORD_ONLY),
        ),
        tuple[watch_type, ...],
    )


def test_run_drive_watch_ack_api_exact_signature() -> None:
    from lockstep.runtime.effects.ledger import EffectLedger

    method = getattr(EffectLedger, "acknowledge_run_drive_watch", None)
    assert callable(method)
    _exact_parameters(
        method,
        (
            ("self", Parameter.POSITIONAL_OR_KEYWORD),
            ("public_run_id", Parameter.POSITIONAL_OR_KEYWORD),
        ),
        NoneType,
    )


def test_legacy_watch_lifecycle_surface_is_retired() -> None:
    from lockstep.runtime import engine_drive_service, service
    from lockstep.runtime.effects import ledger

    remaining = {
        name
        for owner, name in (
            (ledger, "EffectDispatchWatch"),
            (ledger.EffectLedger, "list_dispatch_watches"),
            (ledger.EffectLedger, "acknowledge_dispatch_watch"),
            (service.LockstepCommandService, "_recover_start_admissions"),
            (service.LockstepCommandService, "_ack_start_if_observable"),
        )
        if hasattr(owner, name)
    }
    if "acknowledge_start" in signature(
        engine_drive_service.EngineDriveService
    ).parameters:
        remaining.add("EngineDriveService.acknowledge_start")

    assert remaining == set()


def test_v2_write_transaction_is_exact_context_contract(tmp_path: Path) -> None:
    from lockstep.runtime.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "runtime.db")
    try:
        method = getattr(store, "_v2_write_transaction", None)
        assert callable(method)
        assert tuple(signature(method).parameters) == ()
        context = method()
        assert callable(getattr(context, "__enter__", None))
        assert callable(getattr(context, "__exit__", None))
    finally:
        store.close()
