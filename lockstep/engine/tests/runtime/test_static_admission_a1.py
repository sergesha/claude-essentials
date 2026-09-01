"""R1b-A1: owner-policy and activation races around static admission."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from lockstep.runtime import service as service_module
from lockstep.runtime import start_service as start_service_module
from lockstep.runtime.effects.owner_policy import RuntimeAdmissionDecision
from lockstep.runtime.effects.owner_snapshot_store import open_runtime_snapshot
from lockstep.runtime.errors import LockstepError
from tests.runtime._static_admission_a1_harness import (
    A1ConcurrentCleanup,
    A1Harness,
    owner_tree,
)


def test_supported_revocation_after_real_preflight_is_write_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = A1Harness.granted_runtime(tmp_path, monkeypatch)
    granted_digest, granted_snapshot = open_runtime_snapshot(harness.owner_state)
    original_plan = service_module.plan_authorized_start
    preflight_finished = threading.Event()
    release_start = threading.Event()

    def barrier_after_real_preflight(**kwargs):
        plan = original_plan(**kwargs)
        assert plan.runtime_admission is not None
        preflight_finished.set()
        if not release_start.wait(10.0):
            raise AssertionError("timed out waiting to release admitted start")
        return plan

    monkeypatch.setattr(service_module, "plan_authorized_start", barrier_after_real_preflight)
    service = harness.command()
    cleanup = A1ConcurrentCleanup(service.close, (release_start,))
    with cleanup:
        start = cleanup.launch(
            "lockstep-a1-start",
            lambda: service.start("target", {}, harness.project_identity),
        )
        assert preflight_finished.wait(10.0), "real static preflight did not finish"
        assert harness.provision((), suffix="revoke") == 0
        revoked_digest, revoked_snapshot = open_runtime_snapshot(harness.owner_state)
        assert revoked_digest != granted_digest
        assert revoked_snapshot.config_generation == granted_snapshot.config_generation
        assert revoked_snapshot.policy_generation == granted_snapshot.policy_generation + 1
        assert revoked_snapshot.codex == granted_snapshot.codex
        assert revoked_snapshot.pinned == granted_snapshot.pinned
        assert revoked_snapshot.grants == ()
        expected_after_drift = owner_tree(harness.owner_state)

    assert {
        "rejected": len(start.outcome) == 1 and isinstance(start.outcome[0], LockstepError),
        "worker_stopped": cleanup.threads_stopped,
        "owner_tree_unchanged": owner_tree(harness.owner_state) == expected_after_drift,
        "runtime_database_absent": not (harness.owner_state / "runtime.sqlite").exists(),
        "native_checkpoint_absent": not (
            harness.owner_state / "checkpoints" / "native.sqlite"
        ).exists(),
        "provider_marker_absent": not harness.provider_marker.exists(),
    } == {
        "rejected": True,
        "worker_stopped": True,
        "owner_tree_unchanged": True,
        "runtime_database_absent": True,
        "native_checkpoint_absent": True,
        "provider_marker_absent": True,
    }


def test_admission_first_holds_snapshot_lock_until_first_durable_start_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = A1Harness.granted_runtime(tmp_path, monkeypatch)
    write_entered = threading.Event()
    release_write = threading.Event()
    original_first_write = start_service_module.AuthorizedStartService._admit_and_park

    def blocked_first_write(self, *args, **kwargs):
        write_entered.set()
        if not release_write.wait(10.0):
            raise AssertionError("timed out waiting to persist admitted start")
        return original_first_write(self, *args, **kwargs)

    monkeypatch.setattr(
        start_service_module.AuthorizedStartService,
        "_admit_and_park",
        blocked_first_write,
    )
    service = harness.command()
    cleanup = A1ConcurrentCleanup(service.close, (release_write,))
    with cleanup:
        start = cleanup.launch(
            "lockstep-a1-admit-first",
            lambda: service.start("target", {}, harness.project_identity),
        )
        assert write_entered.wait(10.0), "start never reached its first durable write"
        revoke = cleanup.launch(
            "lockstep-a1-admit-first-provision",
            lambda: harness.provision((), suffix="admission-first-revoke"),
        )
        provisioning_blocked = not revoke.finished.wait(0.5)

    assert {
        "provisioning_blocked": provisioning_blocked,
        "threads_stopped": cleanup.threads_stopped,
        "start_committed": start.outcome[0].get("status") == "starting",
        "supported_revoke_succeeded": revoke.outcome == [0],
    } == {
        "provisioning_blocked": True,
        "threads_stopped": True,
        "start_committed": True,
        "supported_revoke_succeeded": True,
    }


def test_cold_recovery_runs_after_first_start_write_without_snapshot_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = A1Harness.granted_runtime(tmp_path, monkeypatch, include_prior=True)
    prior_service = harness.command()
    prior = prior_service.start("prior", {}, harness.project_identity)
    prior_service.close()
    assert prior["status"] == "awaiting"
    recovery_entered = threading.Event()
    release_recovery = threading.Event()
    service = harness.command()
    original_recovery = service._recover_engine_effects

    def blocked_recovery() -> None:
        recovery_entered.set()
        if not release_recovery.wait(10.0):
            raise AssertionError("timed out waiting to release cold recovery")
        original_recovery()

    monkeypatch.setattr(service, "_recover_engine_effects", blocked_recovery)
    cleanup = A1ConcurrentCleanup(service.close, (release_recovery,))
    with cleanup:
        start = cleanup.launch(
            "lockstep-a1-recovery",
            lambda: service.start("target", {}, harness.project_identity),
        )
        assert recovery_entered.wait(10.0), "cold activation never reached recovery"
        committed_before_recovery = (
            len(service.catalog.list(harness.project_identity)) == 2
        )
        revoke = cleanup.launch(
            "lockstep-a1-recovery-provision",
            lambda: harness.provision((), suffix="recovery-revoke"),
        )
        provision_completed = revoke.finished.wait(2.0)

    assert {
        "new_start_write_precedes_recovery": committed_before_recovery,
        "provision_completed_during_recovery": provision_completed,
        "supported_revoke_succeeded": revoke.outcome == [0],
        "start_committed": start.outcome[0].get("status") == "starting",
        "threads_stopped": cleanup.threads_stopped,
    } == {
        "new_start_write_precedes_recovery": True,
        "provision_completed_during_recovery": True,
        "supported_revoke_succeeded": True,
        "start_committed": True,
        "threads_stopped": True,
    }


def test_activation_waiter_does_not_hold_snapshot_lock_during_prior_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = A1Harness.granted_runtime(tmp_path, monkeypatch)
    recovery_entered = threading.Event()
    release_recovery = threading.Event()
    second_guard_entered = threading.Event()
    service = harness.command()
    original_recovery = service._recover_engine_effects
    original_assert_current = RuntimeAdmissionDecision.assert_current

    def blocked_recovery() -> None:
        recovery_entered.set()
        if not release_recovery.wait(10.0):
            raise AssertionError("timed out waiting to release first recovery")
        original_recovery()

    @contextmanager
    def observed_currentness(decision, state_dir):
        with original_assert_current(decision, state_dir):
            if threading.current_thread().name == "lockstep-a1-second-start":
                second_guard_entered.set()
            yield

    monkeypatch.setattr(service, "_recover_engine_effects", blocked_recovery)
    monkeypatch.setattr(RuntimeAdmissionDecision, "assert_current", observed_currentness)
    cleanup = A1ConcurrentCleanup(service.close, (release_recovery,))
    with cleanup:
        first = cleanup.launch(
            "lockstep-a1-first-start",
            lambda: service.start("target", {}, harness.project_identity),
        )
        assert recovery_entered.wait(10.0), "first start did not enter recovery"
        second = cleanup.launch(
            "lockstep-a1-second-start",
            lambda: service.start("target", {}, harness.project_identity),
        )
        assert second_guard_entered.wait(10.0), "second start missed currentness"
        revoke = cleanup.launch(
            "lockstep-a1-waiter-provision",
            lambda: harness.provision((), suffix="waiter-revoke"),
        )
        provision_completed = revoke.finished.wait(2.0)

    run_ids = harness.run_ids()
    assert {
        "provision_completed_during_recovery": provision_completed,
        "first_committed": first.outcome[0].get("status") == "starting",
        "second_rejected": isinstance(second.outcome[0], LockstepError),
        "exactly_one_run": len(run_ids) == 1,
        "threads_stopped": cleanup.threads_stopped,
    } == {
        "provision_completed_during_recovery": True,
        "first_committed": True,
        "second_rejected": True,
        "exactly_one_run": True,
        "threads_stopped": True,
    }


def test_committed_start_survives_post_persist_recovery_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = A1Harness.granted_runtime(tmp_path, monkeypatch)
    service = harness.command()

    def fail_recovery() -> None:
        raise RuntimeError("unrelated recovery failed after first durable start write")

    monkeypatch.setattr(service, "_recover_engine_effects", fail_recovery)
    try:
        try:
            result: object = service.start("target", {}, harness.project_identity)
        except BaseException as exc:
            result = exc
    finally:
        service.close()

    before_run_ids = harness.run_ids()
    after_run_ids = harness.run_ids()
    returned_run_id = result.get("run_id") if isinstance(result, dict) else None
    assert {
        "start_returned_committed_result": (
            isinstance(result, dict) and result.get("status") == "starting"
        ),
        "one_run_before_restart": len(before_run_ids) == 1,
        "one_run_after_failed_activation": len(after_run_ids) == 1,
        "same_committed_run": (
            returned_run_id is not None
            and before_run_ids[0] == returned_run_id
            and after_run_ids[0] == returned_run_id
        ),
    } == {
        "start_returned_committed_result": True,
        "one_run_before_restart": True,
        "one_run_after_failed_activation": True,
        "same_committed_run": True,
    }
