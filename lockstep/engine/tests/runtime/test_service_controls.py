from __future__ import annotations

import inspect
import threading
from collections import deque
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from lockstep.runtime import sessions
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_descriptor,
)
from lockstep.runtime.native_models import (
    NativeCoordinate,
    NativeInterrupt,
    NativeSnapshot,
)
from lockstep.runtime.owner_state import initialize_owner_state
from lockstep.runtime.service import LockstepError, LockstepCommandService
from lockstep.runtime.status import ScenarioStatus


def _service_double() -> LockstepCommandService:
    service = object.__new__(LockstepCommandService)
    service._activation_lock = threading.RLock()  # noqa: SLF001
    service._writable_core_active = True  # noqa: SLF001
    service._initial_recovery_exclusion = None  # noqa: SLF001
    service._owned_effect_bindings = set()  # noqa: SLF001
    service._active_effect_runs = set()  # noqa: SLF001
    service._queued_effect_runs = set()  # noqa: SLF001
    service._active_effect_queue = deque()  # noqa: SLF001
    service._active_effect_lock = threading.Lock()  # noqa: SLF001
    service._pump_wakeup = threading.Event()  # noqa: SLF001
    service._recovery_driver = SimpleNamespace(  # noqa: SLF001
        _sweep_run_drive_watches=lambda **_kwargs: ()
    )
    # These focused doubles model a service whose coordinator is already open.
    service._runtime_execution_context = object()  # noqa: SLF001
    service._reconstruct_runtime_execution_context = (  # noqa: SLF001
        lambda **_kwargs: None
    )
    return service


def test_writable_core_activation_is_retryable_after_recovery_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    service = LockstepCommandService(tmp_path / "state", recipes)
    real_recover = service._recover_engine_effects  # noqa: SLF001
    attempts = 0

    def fail_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("recovery failed")
        real_recover()

    monkeypatch.setattr(service, "_recover_engine_effects", fail_once)
    try:
        with pytest.raises(RuntimeError, match="recovery failed"):
            service._activate_writable_core()  # noqa: SLF001
        assert service._writable_core_active is False  # noqa: SLF001

        service._activate_writable_core()  # noqa: SLF001

        assert service._writable_core_active is True  # noqa: SLF001
        assert attempts == 2
    finally:
        service.close()


def test_static_admission_orders_admission_before_snapshot_and_activation() -> None:
    from lockstep.runtime.start_service import _WritableCoreActivation

    events = []

    class AdmissionLock:
        held = False

        def acquire(self, blocking=True):
            assert blocking is True
            assert not self.held
            self.held = True
            events.append("admission-enter")
            return True

        def release(self):
            assert self.held
            self.held = False
            events.append("admission-exit")

    class ActivationLock:
        attempts = 0

        def acquire(self, blocking=True):
            events.append("activation-wait" if blocking else "activation-try")
            if not blocking:
                self.attempts += 1
                return self.attempts > 1
            return True

        def release(self):
            events.append("activation-exit")

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_args):
            self.release()

    admission = AdmissionLock()

    class Current:
        def __enter__(self):
            assert admission.held
            events.append("snapshot-enter")

        def __exit__(self, *_args):
            events.append("snapshot-exit")

    decision = SimpleNamespace(assert_current=lambda _state_dir: Current())
    activation = _WritableCoreActivation(
        lock=ActivationLock(),
        admission_lock=admission,
        is_active=lambda: True,
        is_closed=lambda: False,
        prepare=lambda: None,
        finish=lambda: None,
        rollback=lambda: None,
        record_degraded=lambda _exc: None,
    )

    result = activation.admit(
        Path("/owner"),
        decision,
        lambda: (
            events.append("persist")
            or {"status": "starting"}
        ),
    )

    assert result == {"status": "starting"}
    assert events == [
        "admission-enter",
        "snapshot-enter",
        "activation-try",
        "snapshot-exit",
        "admission-exit",
        "activation-wait",
        "activation-exit",
        "admission-enter",
        "snapshot-enter",
        "activation-try",
        "persist",
        "snapshot-exit",
        "admission-exit",
        "activation-exit",
    ]


def test_static_admission_and_pump_snapshot_lock_cannot_deadlock() -> None:
    from lockstep.runtime.start_service import _WritableCoreActivation

    admission = threading.RLock()
    snapshot = threading.Lock()
    pump_has_admission = threading.Event()
    current_entered = threading.Event()
    pump_snapshot_result = []
    failures = []

    class Current:
        def __enter__(self):
            snapshot.acquire()
            current_entered.set()

        def __exit__(self, *_args):
            snapshot.release()

    decision = SimpleNamespace(assert_current=lambda _state_dir: Current())
    activation = _WritableCoreActivation(
        lock=threading.RLock(),
        admission_lock=admission,
        is_active=lambda: True,
        is_closed=lambda: False,
        prepare=lambda: None,
        finish=lambda: None,
        rollback=lambda: None,
        record_degraded=lambda _exc: None,
    )

    def pump() -> None:
        with admission:
            pump_has_admission.set()
            current_entered.wait(0.05)
            acquired = snapshot.acquire(timeout=0.2)
            pump_snapshot_result.append(acquired)
            if acquired:
                snapshot.release()

    def start() -> None:
        try:
            activation.admit(
                Path("/owner"),
                decision,
                lambda: (
                    admission.acquire()
                    and admission.release()
                    or {"status": "starting"}
                ),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    pump_thread = threading.Thread(target=pump)
    pump_thread.start()
    assert pump_has_admission.wait(1)
    start_thread = threading.Thread(target=start)
    start_thread.start()
    pump_thread.join(timeout=1)
    start_thread.join(timeout=1)

    assert not pump_thread.is_alive()
    assert not start_thread.is_alive()
    assert pump_snapshot_result == [True]
    assert failures == []


def test_writable_core_activation_is_retryable_after_thread_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    service = LockstepCommandService(tmp_path / "state", recipes)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            threading.Thread,
            "start",
            lambda _thread: (_ for _ in ()).throw(RuntimeError("start failed")),
        )
        with pytest.raises(RuntimeError, match="start failed"):
            service._activate_writable_core()  # noqa: SLF001
    try:
        assert service._writable_core_active is False  # noqa: SLF001
        service._activate_writable_core()  # noqa: SLF001
        assert service._writable_core_active is True  # noqa: SLF001
    finally:
        service.close()


def test_close_serializes_with_first_writable_core_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    service = LockstepCommandService(tmp_path / "state", recipes)
    entered = threading.Event()
    release = threading.Event()
    real_recover = service._recover_engine_effects  # noqa: SLF001

    def blocked_recover() -> None:
        entered.set()
        assert release.wait(2)
        real_recover()

    monkeypatch.setattr(service, "_recover_engine_effects", blocked_recover)
    activation = threading.Thread(target=service._activate_writable_core)  # noqa: SLF001
    closing = threading.Thread(target=service.close)
    activation.start()
    assert entered.wait(2)
    closing.start()
    closing.join(0.1)
    closed_before_activation_finished = not closing.is_alive()

    release.set()
    activation.join(2)
    closing.join(2)
    pump = service._pump_thread  # noqa: SLF001
    if pump is not None and pump.is_alive():
        service._pump_stop.set()  # noqa: SLF001
        service._pump_wakeup.set()  # noqa: SLF001
        pump.join(2)

    assert closed_before_activation_finished is False
    assert not activation.is_alive()
    assert not closing.is_alive()
    assert service._closed is True  # noqa: SLF001
    assert service._writable_core_active is False  # noqa: SLF001
    assert pump is not None
    assert not pump.is_alive()


def test_service_composes_project_resolved_artifact_publication_and_acceptance(
    tmp_path,
) -> None:
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    service = LockstepCommandService(tmp_path / "state", recipes)
    try:
        service._activate_writable_core()  # noqa: SLF001 - composition unit seam
        assert service.artifacts is service.coordinator._artifacts
        one = service.coordinator._publisher_for(
            RunBinding("run-1", "thread-1", "a" * 64, "bundle", str(first))
        )
        two = service.coordinator._publisher_for(
            RunBinding("run-2", "thread-2", "b" * 64, "bundle", str(second))
        )
        assert one.binding_digest != two.binding_digest
        assert callable(service.scenario_accept_artifact)
        from lockstep.runtime.effects.owner_consent import OwnerConsentAuthority

        assert isinstance(service.authority, OwnerConsentAuthority)
        assert service.coordinator._authority is service.authority
    finally:
        service.close()

def test_engine_effect_queue_has_a_hard_admission_ceiling() -> None:
    service = _service_double()
    service._active_effect_runs = set()
    service._queued_effect_runs = set()
    service._active_effect_queue = deque()
    service._active_effect_lock = threading.Lock()
    service._pump_wakeup = threading.Event()

    for index in range(service._MAX_ACTIVE_EFFECT_RUNS):
        service._activate_effect_run(f"run-{index}")

    service._activate_effect_run("one-too-many")

    assert len(service._active_effect_runs) == service._MAX_ACTIVE_EFFECT_RUNS
    assert len(service._active_effect_queue) == service._MAX_ACTIVE_EFFECT_RUNS
    assert "one-too-many" not in service._active_effect_runs


def test_dispatch_recovery_serializes_with_foreground_admission() -> None:
    service = _service_double()
    service._admission_recovery_lock = threading.RLock()
    entered = threading.Event()
    finished = threading.Event()
    service._recovery_driver = SimpleNamespace(
        _sweep_run_drive_watches=lambda **_kwargs: entered.set()
    )

    with service._admission_recovery_lock:
        worker = threading.Thread(
            target=lambda: (service._recover_engine_effects(), finished.set())
        )
        worker.start()
        assert not entered.wait(0.05)
        assert not finished.is_set()

    worker.join(timeout=1)
    assert entered.is_set()
    assert finished.is_set()


def test_active_pump_drive_and_release_serialize_with_foreground() -> None:
    service = _service_double()
    service._admission_recovery_lock = threading.RLock()
    service._pump_stop = threading.Event()
    service._pump_wakeup = threading.Event()
    service._pump_wakeup.set()
    entered = threading.Event()
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    service._take_active_effect_runs = lambda: (binding.public_run_id,)
    service.catalog = SimpleNamespace(get=lambda _run_id: binding)
    service.runtime = SimpleNamespace(bind=lambda _binding: False)
    service._drive_engine_owned = lambda *_args, **_kwargs: entered.set()
    service._release_inactive_effect_binding = lambda _run_id: None
    recovery_lock_state = []

    def recover() -> None:
        acquired = []

        def contend() -> None:
            locked = service._admission_recovery_lock.acquire(blocking=False)
            acquired.append(locked)
            if locked:
                service._admission_recovery_lock.release()

        contender = threading.Thread(target=contend)
        contender.start()
        contender.join(timeout=1)
        assert not contender.is_alive()
        recovery_lock_state.extend(acquired)
        service._pump_stop.set()

    service._recover_engine_effects = recover

    with service._admission_recovery_lock:
        pump = threading.Thread(target=service._completion_pump)
        pump.start()
        assert not entered.wait(0.05)

    pump.join(timeout=1)
    assert not pump.is_alive()
    assert entered.is_set()
    assert recovery_lock_state == [True]


def test_cancelled_pump_wakeup_does_not_run_recovery() -> None:
    service = _service_double()
    service._admission_recovery_lock = threading.RLock()
    service._pump_stop = threading.Event()
    service._pump_wakeup.set()

    def take_cancelled_queue() -> tuple[str, ...]:
        service._pump_stop.set()
        return ()

    service._take_active_effect_runs = take_cancelled_queue
    service._recover_engine_effects = lambda: pytest.fail(
        "an empty, cancelled pump wakeup must not run recovery"
    )

    service._completion_pump()


def test_timed_empty_pump_cycle_still_runs_recovery() -> None:
    service = _service_double()
    service._admission_recovery_lock = threading.RLock()
    service._pump_stop = threading.Event()
    recovered = []

    class TimedOutWake:
        def wait(self, timeout: float) -> bool:
            assert timeout == 0.25
            return False

        def clear(self) -> None:
            pass

    def take_empty_queue() -> tuple[str, ...]:
        service._pump_stop.set()
        return ()

    service._pump_wakeup = TimedOutWake()
    service._take_active_effect_runs = take_empty_queue
    service._recover_engine_effects = lambda: recovered.append("recovered")

    service._completion_pump()

    assert recovered == ["recovered"]


def test_runtime_reconstruction_tracks_the_bounded_recovery_page() -> None:
    service = _service_double()
    service._runtime_execution_context = None
    service._admission_recovery_lock = threading.RLock()
    observed = []
    service._reconstruct_runtime_execution_context = (
        lambda *, after_thread_id=None, limit=None: observed.append(
            (after_thread_id, limit)
        )
    )
    service._recovery_driver = SimpleNamespace(
        _sweep_run_drive_watches=lambda **_kwargs: ()
    )

    service._recover_engine_effects()

    assert observed == [(None, None)]


def test_parallel_recovery_installs_one_runtime_composition() -> None:
    service = _service_double()
    service._runtime_execution_context = None
    service._admission_recovery_lock = threading.RLock()
    reconstructed = object()
    reconstruct_calls = []
    install_calls = []

    def reconstruct(*, after_thread_id=None, limit=None):
        reconstruct_calls.append((after_thread_id, limit))
        return reconstructed

    def install(context):
        install_calls.append(context)
        service._runtime_execution_context = context

    service._reconstruct_runtime_execution_context = reconstruct
    service._install_runtime_execution = install
    service._recovery_driver = SimpleNamespace(
        _sweep_run_drive_watches=lambda **_kwargs: ()
    )

    workers = [threading.Thread(target=service._recover_engine_effects) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=1)

    assert all(not worker.is_alive() for worker in workers)
    assert reconstruct_calls == [(None, None), (None, None)]
    assert install_calls == [reconstructed]


def test_recovery_rejects_a_different_preinstalled_runtime_context() -> None:
    service = _service_double()
    service._reconstruct_runtime_execution_context = lambda **_kwargs: object()
    service._admission_recovery_lock = threading.RLock()

    with pytest.raises(
        LockstepError, match="recovered runtime execution snapshot changed"
    ):
        service._recover_engine_effects()


def test_worker_resume_blocks_recovery_unbind_for_the_whole_composite(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service_double()
    service._admission_recovery_lock = threading.RLock()
    service.state_dir = tmp_path
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    interrupt = NativeInterrupt(coordinate, {"step": "work"})
    foreground_late = threading.Event()
    release = threading.Event()
    recovery_unbound = threading.Event()
    failures: list[BaseException] = []
    service.catalog = SimpleNamespace(get=lambda _run_id: binding)
    service._bind_existing = lambda *_args: nullcontext(binding)
    service._worker_interrupt = lambda *_args: (binding, interrupt)

    def resume(*_args, **_kwargs):
        foreground_late.set()
        assert release.wait(1)
        return NativeSnapshot(values={"lockstep_outcome": "PASS"}, checkpoint_id="cp-2")

    service.runtime = SimpleNamespace(
        resume=resume,
        unbind=lambda _run_id: recovery_unbound.set(),
    )
    service._recovery_driver = SimpleNamespace(
        _sweep_run_drive_watches=lambda **_kwargs: service.runtime.unbind("run-1")
    )
    monkeypatch.setattr(sessions, "locked_owner", lambda *_args, **_kwargs: nullcontext())

    def foreground() -> None:
        try:
            service._resume_worker(
                "run-1", "work", {"outcome": "PASS"},
                session_id="session-1", project="/project",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    foreground_thread = threading.Thread(target=foreground)
    foreground_thread.start()
    assert foreground_late.wait(1)
    recovery_thread = threading.Thread(target=service._recover_engine_effects)
    recovery_thread.start()
    assert not recovery_unbound.wait(0.05)
    release.set()
    foreground_thread.join(timeout=1)
    recovery_thread.join(timeout=1)
    assert failures == []
    assert recovery_unbound.is_set()


def test_existing_binding_scope_serializes_owner_and_borrower() -> None:
    service = _service_double()
    borrower_lock_attempted = threading.Event()

    class InstrumentedRLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "binding-borrower":
                borrower_lock_attempted.set()
            self._lock.acquire()
            return self

        def __exit__(self, *_args):
            self._lock.release()

    service._admission_recovery_lock = InstrumentedRLock()
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    service.catalog = SimpleNamespace(get=lambda _run_id: binding)
    calls = []
    runtime_lock = threading.Lock()

    class Runtime:
        bound = False

        def bind(self, _binding):
            with runtime_lock:
                owned = not self.bound
                self.bound = True
                calls.append(("bind", owned))
                return owned

        def unbind(self, _run_id):
            with runtime_lock:
                assert self.bound
                self.bound = False
                calls.append(("unbind", True))

    service.runtime = Runtime()
    owner_entered = threading.Event()
    release_owner = threading.Event()
    borrower_entered = threading.Event()
    failures = []

    def owner() -> None:
        try:
            with service._bind_existing("run-1", "/project"):  # noqa: SLF001
                owner_entered.set()
                assert release_owner.wait(1)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def borrower() -> None:
        try:
            with service._bind_existing("run-1", "/project"):  # noqa: SLF001
                borrower_entered.set()
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    owner_thread = threading.Thread(target=owner)
    borrower_thread = threading.Thread(target=borrower, name="binding-borrower")
    owner_thread.start()
    assert owner_entered.wait(1)
    borrower_thread.start()
    attempted = borrower_lock_attempted.wait(1)
    overlapped = borrower_entered.wait(0.05)
    release_owner.set()
    owner_thread.join(1)
    borrower_thread.join(1)

    assert attempted is True
    assert overlapped is False
    assert not owner_thread.is_alive()
    assert not borrower_thread.is_alive()
    assert failures == []
    assert calls == [
        ("bind", True),
        ("unbind", True),
        ("bind", True),
        ("unbind", True),
    ]


def test_artifact_acceptance_blocks_recovery_unbind_through_drive(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service_double()
    service._admission_recovery_lock = threading.RLock()
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    foreground_late = threading.Event()
    release = threading.Event()
    recovery_unbound = threading.Event()
    failures: list[BaseException] = []
    service._bind_existing = lambda *_args: nullcontext(binding)
    stored = SimpleNamespace(
        commitment=SimpleNamespace(
            public_run_id="run-1",
            project_identity="/project",
            definition_digest="a" * 64,
            source=coordinate,
        ),
    )
    service.authority = SimpleNamespace(inspect_token=lambda token: stored)
    submit_calls = []
    service.coordinator = SimpleNamespace(
        submit_acceptance=lambda *args: submit_calls.append(args)
    )

    def drive(*_args, **_kwargs):
        foreground_late.set()
        assert release.wait(1)
        return ScenarioStatus("completed", "run-1", "engine", None)

    service._drive_engine_owned = drive
    service.runtime = SimpleNamespace(unbind=lambda _run_id: recovery_unbound.set())
    service._recovery_driver = SimpleNamespace(
        _sweep_run_drive_watches=lambda **_kwargs: service.runtime.unbind("run-1")
    )
    monkeypatch.setattr(
        sessions,
        "locked_owner",
        lambda *_args, **_kwargs: pytest.fail(
            "token acceptance consulted session authority"
        ),
    )

    def foreground() -> None:
        try:
            service.scenario_accept_artifact("secret-token", project="/project")
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    foreground_thread = threading.Thread(target=foreground)
    foreground_thread.start()
    assert foreground_late.wait(1)
    recovery_thread = threading.Thread(target=service._recover_engine_effects)
    recovery_thread.start()
    assert not recovery_unbound.wait(0.05)
    release.set()
    foreground_thread.join(timeout=1)
    recovery_thread.join(timeout=1)
    assert failures == []
    assert recovery_unbound.is_set()
    assert submit_calls == [("run-1", coordinate, "secret-token")]


def test_artifact_acceptance_public_signature_is_token_plus_ambient_project() -> None:
    signature = inspect.signature(LockstepCommandService.scenario_accept_artifact)
    assert tuple(signature.parameters) == ("self", "token", "project")
    assert signature.parameters["project"].kind is inspect.Parameter.KEYWORD_ONLY
    assert {
        "run_id",
        "step",
        "artifact_ref",
        "consent_ref",
        "approval_generation",
        "session_id",
    }.isdisjoint(signature.parameters)


def test_publication_consent_preview_is_read_only_and_issue_rechecks_digest() -> None:
    service = _service_double()
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    interrupt = NativeInterrupt(coordinate, {})
    service._pending_acceptance = lambda *_args, **_kwargs: (binding, interrupt)
    calls = []

    class Coordinator:
        def preview_acceptance(self, run_id, source):
            calls.append(("preview", run_id, source))
            return SimpleNamespace(
                to_dict=lambda: {
                    "schema": "lockstep.publication-consent-commitment/v1",
                    "digest": "b" * 64,
                    "destination": "docs/review.md",
                }
            )

        def issue_acceptance_consent(self, run_id, source, expected):
            calls.append(("issue", run_id, source, expected))
            raise RuntimeError("acceptance changed after owner consent preview")

    service.coordinator = Coordinator()
    service._admission_recovery_lock = threading.RLock()
    service._bind_existing = lambda *_args: nullcontext(binding)

    preview = service.preview_publication_consent(
        "run-1", "accept-review", project="/project"
    )
    assert preview["digest"] == "b" * 64
    assert "token" not in preview
    with pytest.raises(RuntimeError, match="changed after owner consent preview"):
        service.issue_publication_consent(
            "run-1", "accept-review", "b" * 64, project="/project"
        )
    assert calls == [
        ("preview", "run-1", coordinate),
        ("issue", "run-1", coordinate, "b" * 64),
    ]


def test_consent_activates_before_waiting_for_recovery_admission() -> None:
    service = _service_double()
    activation_entered = threading.Event()
    release_activation = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []

    def activate() -> None:
        activation_entered.set()
        assert release_activation.wait(1)

    service._activate_writable_core = activate
    service._pending_acceptance = lambda *_args, **_kwargs: (
        None,
        SimpleNamespace(coordinate="coordinate"),
    )
    service.coordinator = SimpleNamespace(
        issue_acceptance_consent=lambda *_args: "issued"
    )
    service._admission_recovery_lock = threading.RLock()
    service._bind_existing = lambda *_args: nullcontext(None)

    def issue() -> None:
        try:
            service.issue_publication_consent(
                "run", "accept", "a" * 64, project="/project"
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            finished.set()

    with service._admission_recovery_lock:
        worker = threading.Thread(target=issue)
        worker.start()
        assert activation_entered.wait(1)
        release_activation.set()
        assert not finished.wait(0.05)

    worker.join(timeout=1)
    assert failures == []
    assert finished.is_set()


def test_artifact_acceptance_foreign_ambient_project_is_generic_and_read_only() -> None:
    service = _service_double()
    token = "never-echo-this-token"
    service.authority = SimpleNamespace(
        inspect_token=lambda _token: SimpleNamespace(
            commitment=SimpleNamespace(project_identity="/owner-project")
        )
    )
    service.coordinator = SimpleNamespace(
        submit_acceptance=lambda *_args: pytest.fail("foreign token was redeemed")
    )

    with pytest.raises(LockstepError, match="invalid or stale") as exc:
        service.scenario_accept_artifact(token, project="/foreign-project")
    assert token not in str(exc.value)


def test_effect_recovery_defers_before_reconcile_when_active_batch_is_full() -> None:
    coordinate = NativeCoordinate("thread-pinned", "cp-1", "", "task-1", "int-1")
    interrupt = NativeInterrupt(
        coordinate,
        {
            "lockstep_effect": {
                "schema": "lockstep.effect/v1",
                "kind": "pinned",
                "logical_id": "tests",
                "runner": {
                    "selector": "pinned",
                    "required_capabilities": [
                        "workspace",
                        "bounded_result",
                        "sandbox",
                    ],
                },
                "inputs": {
                    "command": {"state_key": "command"},
                    "snapshot": {"state_key": "snapshot"},
                },
                "writes": [],
                "artifacts": [],
                "deadline_seconds": 60,
                "scope_state_keys": [],
                "result_schema": "lockstep.effect-result/v1",
            }
        },
    )
    snapshot = NativeSnapshot(values={}, pending=(interrupt,), checkpoint_id="cp-1")
    binding = RunBinding("run-pinned", "thread-pinned", "a" * 64, "bundle", "/project")
    service = _service_double()
    service.effects = SimpleNamespace(
        get=lambda _effect_id: (_ for _ in ()).throw(KeyError())
    )
    service.leases = ()
    service.runtime = SimpleNamespace(
        decision_guard=lambda _run_id: nullcontext(),
        snapshot=lambda *_args, **_kwargs: snapshot,
    )
    service.coordinator = SimpleNamespace(
        reconcile=lambda _run_id: pytest.fail("capacity deferral reconciled effect")
    )
    service._active_effect_runs = {
        f"run-{index}" for index in range(service._MAX_ACTIVE_EFFECT_RUNS)
    }
    service._active_effect_lock = threading.Lock()

    status = service._drive_engine_owned(
        "run-pinned", binding=binding, snapshot=snapshot
    )

    assert status.status == "running"
    assert "run-pinned" not in service._active_effect_runs


def test_scenario_recover_uses_only_the_recovery_driver() -> None:
    project_identity = str(Path("/project").resolve())
    validated = []
    service = _service_double()
    service._admission_recovery_lock = threading.RLock()
    service.effects = SimpleNamespace(
        list_recovery_threads=lambda **_kwargs: pytest.fail(
            "service started a second recovery scan"
        )
    )
    service._install_recovered_runtime_execution = (
        lambda *, after_thread_id=None, limit=None: validated.append(
            (after_thread_id, limit)
        )
    )
    service._recovery_driver = SimpleNamespace(
        _sweep_run_drive_watches=lambda **kwargs: (
            "run-1",
        )
        if kwargs == {"project_identity": project_identity, "limit": 1}
        else pytest.fail("unexpected driver boundary")
    )

    result = service.scenario_recover(project_identity, limit=1)

    assert result == {"recovered": ["run-1"], "count": 1, "limit": 1}
    assert validated == [(None, 1)]
    assert not hasattr(LockstepCommandService, "_recover_effect_batch")


def test_service_exposes_no_status_mutation_api() -> None:
    forbidden = {
        "set_status",
        "update_status",
        "mark_completed",
        "mark_escalated",
        "mark_aborted",
    }
    assert forbidden.isdisjoint(vars(LockstepCommandService))


def test_protected_manual_step_uses_descriptor_logical_id() -> None:
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    raw = {
        "schema": "lockstep.effect/v1",
        "kind": "manual",
        "logical_id": "edit",
        "runner": None,
        "inputs": {},
        "writes": ["src/"],
        "artifacts": [],
        "deadline_seconds": None,
        "scope_state_keys": [],
        "result_schema": "lockstep.effect-result/v1",
    }
    interrupt = NativeInterrupt(coordinate, {"lockstep_effect": raw})
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    service = _service_double()
    service._snapshot_status = lambda *_args: (
        binding,
        ScenarioStatus("awaiting", "run-1", "worker", "edit_then_scenario_done"),
    )
    service.runtime = SimpleNamespace(
        snapshot=lambda *_args, **_kwargs: NativeSnapshot(
            values={}, pending=(interrupt,), checkpoint_id="cp-1"
        )
    )

    assert service._worker_interrupt("run-1", "edit", "/project") == (
        binding,
        interrupt,
    )


def test_engine_progress_prepares_manual_handoff_before_returning_awaiting() -> None:
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    raw = {
        "schema": "lockstep.effect/v1",
        "kind": "manual",
        "logical_id": "edit",
        "runner": None,
        "inputs": {},
        "writes": ["src/"],
        "artifacts": [],
        "deadline_seconds": None,
        "scope_state_keys": [],
        "result_schema": "lockstep.effect-result/v1",
    }
    descriptor = parse_effect_descriptor(raw)
    interrupt = NativeInterrupt(coordinate, {"lockstep_effect": raw})
    snapshot = NativeSnapshot(values={}, pending=(interrupt,), checkpoint_id="cp-1")
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    effect_id = derive_effect_id(coordinate, descriptor.digest)

    class Effects:
        record = None

        def get(self, requested):
            assert requested == effect_id
            if self.record is None:
                raise KeyError(requested)
            return self.record

    effects = Effects()

    class Coordinator:
        calls = 0

        def reconcile_pending(self, run_id):
            assert run_id == "run-1"
            self.calls += 1
            effects.record = SimpleNamespace(
                coordinate=coordinate,
                descriptor_digest=descriptor.digest,
                effect_kind="manual",
                phase="prepared",
            )
            return (SimpleNamespace(action="prepared"),)

    service = _service_double()
    service.effects = effects
    service.leases = ()
    service.coordinator = Coordinator()
    service.runtime = SimpleNamespace(
        decision_guard=lambda _run_id: nullcontext(),
        snapshot=lambda *_args, **_kwargs: snapshot,
    )
    service._deactivate_effect_run = lambda _run_id: None

    status = service._drive_engine_owned("run-1", binding=binding, snapshot=snapshot)

    assert status.status == "awaiting"
    assert status.owner == "worker"
    assert service.coordinator.calls == 1


@pytest.mark.parametrize(
    ("action", "accepted"),
    tuple(
        (action, True)
        for action in (
            "prepared",
            "launch_claimed",
            "sealed",
            "delivered",
            "awaiting_delivery",
            "publication_claimed",
            "publication_progress",
            "running",
            "quiescence_pending",
            "indeterminate",
        )
    )
    + tuple(
        (action, False)
        for action in (
            "busy",
            "unchanged",
            "no_effect",
            "manual_pending",
            "acceptance_pending",
            "authority_blocked",
            "deadline_blocked",
        )
    ),
)
def test_recovered_engine_drive_counts_only_real_attempt_actions(
    action: str, accepted: bool
) -> None:
    from lockstep.runtime.engine_drive_service import EngineDriveService

    assert EngineDriveService._accepted_attempt({action}) is accepted


def test_recovered_engine_drive_attempt_accounting_uses_any_real_work() -> None:
    from lockstep.runtime.engine_drive_service import EngineDriveService

    assert EngineDriveService._accepted_attempt({"running", "busy"}) is True
    assert EngineDriveService._accepted_attempt({"busy", "unchanged"}) is False
    assert EngineDriveService._accepted_attempt(set()) is False


@pytest.mark.parametrize(
    ("owned", "active", "released"),
    ((True, False, True), (True, True, False), (False, False, False)),
)
def test_recovered_engine_drive_owns_binding_until_inactive_release(
    owned: bool,
    active: bool,
    released: bool,
) -> None:
    run_id = "run-1"
    binding = RunBinding(run_id, "thread-1", "a" * 64, "bundle", "/project")
    bound = {}
    events = []

    def bind_runtime(recovered_binding):
        bound[run_id] = recovered_binding
        events.append(("bind", recovered_binding))
        return owned

    def unbind_runtime(recovered_run_id):
        assert recovered_run_id in bound
        bound.pop(recovered_run_id)
        events.append(("unbind", recovered_run_id))

    def drive(recovered_run_id):
        assert bound[recovered_run_id] == binding
        events.append(("drive", recovered_run_id))
        return False

    service = _service_double()
    service.catalog = SimpleNamespace(get=lambda _run_id: binding)
    service.runtime = SimpleNamespace(
        bind=bind_runtime,
        unbind=unbind_runtime,
    )
    service._active_effect_lock = threading.Lock()
    service._active_effect_runs = {run_id} if active else set()
    service._engine_drive_service = lambda **_kwargs: SimpleNamespace(
        drive_recovered=drive
    )

    assert service._drive_recovered_run(run_id) is False
    expected = [("bind", binding), ("drive", run_id)]
    if released:
        expected.append(("unbind", run_id))
    assert events == expected


@pytest.mark.parametrize("inherited_active", (False, True))
def test_recovered_engine_drive_releases_only_its_failed_reservation(
    inherited_active: bool,
) -> None:
    run_id = "run-1"
    binding = RunBinding(run_id, "thread-1", "a" * 64, "bundle", "/project")
    events = []
    service = _service_double()
    service.catalog = SimpleNamespace(get=lambda _run_id: binding)
    service.runtime = SimpleNamespace(
        bind=lambda recovered_binding: (
            events.append(("bind", recovered_binding)) or not inherited_active
        ),
        unbind=lambda recovered_run_id: events.append(
            ("unbind", recovered_run_id)
        ),
    )
    service._active_effect_lock = threading.Lock()
    service._active_effect_runs = {run_id} if inherited_active else set()
    service._queued_effect_runs = {run_id} if inherited_active else set()
    service._active_effect_queue = deque((run_id,)) if inherited_active else deque()

    def engine_service(*, reserve_effect_run=None):
        def drive_recovered(recovered_run_id):
            assert reserve_effect_run(recovered_run_id) is True
            raise RuntimeError("failed after reserve")

        return SimpleNamespace(drive_recovered=drive_recovered)

    service._engine_drive_service = engine_service
    baseline = (
        set(service._active_effect_runs),
        set(service._queued_effect_runs),
        tuple(service._active_effect_queue),
    )

    with pytest.raises(RuntimeError, match="failed after reserve"):
        service._drive_recovered_run(run_id)

    assert (
        service._active_effect_runs,
        service._queued_effect_runs,
        tuple(service._active_effect_queue),
    ) == baseline
    expected = [("bind", binding)]
    if not inherited_active:
        expected.append(("unbind", run_id))
    assert events == expected


def test_recovered_binding_handoff_releases_only_after_deactivation() -> None:
    run_id = "run-1"
    unbound = []
    service = _service_double()
    service.runtime = SimpleNamespace(unbind=unbound.append)
    service._active_effect_lock = threading.Lock()
    service._active_effect_runs = {run_id}
    service._owned_effect_bindings = {run_id}

    service._release_inactive_effect_binding(run_id)
    assert service._owned_effect_bindings == {run_id}
    assert unbound == []

    service._active_effect_runs.clear()
    service._release_inactive_effect_binding(run_id)
    assert service._owned_effect_bindings == set()
    assert unbound == [run_id]


@pytest.mark.parametrize("service_owned", (False, True))
def test_common_engine_drive_releases_only_service_owned_inactive_binding(
    service_owned: bool,
) -> None:
    run_id = "run-1"
    binding = RunBinding(run_id, "thread-1", "a" * 64, "bundle", "/project")
    unbound = []
    service = _service_double()
    service.runtime = SimpleNamespace(unbind=unbound.append)
    service._active_effect_lock = threading.Lock()
    service._active_effect_runs = {run_id}
    service._owned_effect_bindings = {run_id} if service_owned else set()

    def drive(*_args, **_kwargs):
        service._active_effect_runs.clear()
        return ScenarioStatus("completed", run_id, "engine", None)

    service._engine_drive_service = lambda: SimpleNamespace(drive=drive)

    service._drive_engine_owned(run_id, binding=binding)

    assert service._owned_effect_bindings == set()
    assert unbound == ([run_id] if service_owned else [])


def test_engine_progress_delivers_scope_result_without_status_mutation() -> None:
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    interrupt = NativeInterrupt(
        coordinate,
        {
            "lockstep_effect": {
                "schema": "lockstep.effect/v1",
                "kind": "scope",
                "logical_id": "child-scope",
                "scope_kind": "call",
                "duration_seconds": 60,
                "runner_selector": "codex",
                "ancestor_deadline_state_keys": [],
                "result_state_key": "child_scope_result",
                "result_schema": "lockstep.scope-result/v1",
            }
        },
    )
    pending = NativeSnapshot(values={}, pending=(interrupt,), checkpoint_id="cp-1")
    completed = NativeSnapshot(
        values={"lockstep_outcome": "PASS"}, checkpoint_id="cp-2"
    )
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    state = {"snapshot": pending}

    class Coordinator:
        def __init__(self):
            self.actions = iter(("sealed", "awaiting_delivery"))
            self.deliveries = 0

        def reconcile_pending(self, _run_id):
            return (SimpleNamespace(action=next(self.actions)),)

        def deliver_ready(self, _run_id):
            self.deliveries += 1
            state["snapshot"] = completed

        def reconcile_consumed(self, _run_id):
            return ()

    service = _service_double()
    service.effects = ()
    service.leases = ()
    service.coordinator = Coordinator()
    service.runtime = SimpleNamespace(
        decision_guard=lambda _run_id: nullcontext(),
        snapshot=lambda *_args, **_kwargs: state["snapshot"]
    )
    service._deactivate_effect_run = lambda _run_id: None

    status = service._drive_engine_owned("run-1", binding=binding, snapshot=pending)

    assert status.status == "completed"
    assert service.coordinator.deliveries == 1


def test_engine_progress_requeues_a_delivery_held_by_another_owner() -> None:
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    interrupt = NativeInterrupt(
        coordinate,
        {
            "lockstep_effect": {
                "schema": "lockstep.effect/v1",
                "kind": "scope",
                "logical_id": "child-scope",
                "scope_kind": "call",
                "duration_seconds": 60,
                "runner_selector": "codex",
                "ancestor_deadline_state_keys": [],
                "result_state_key": "child_scope_result",
                "result_schema": "lockstep.scope-result/v1",
            }
        },
    )
    pending = NativeSnapshot(values={}, pending=(interrupt,), checkpoint_id="cp-1")
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")

    class Coordinator:
        calls = 0

        def reconcile_pending(self, _run_id):
            self.calls += 1
            return (SimpleNamespace(action="awaiting_delivery"),)

        def deliver_ready(self, _run_id):
            return None

    activated = []
    service = _service_double()
    service.effects = ()
    service.leases = ()
    service.coordinator = Coordinator()
    service.runtime = SimpleNamespace(
        decision_guard=lambda _run_id: nullcontext(),
        snapshot=lambda *_args, **_kwargs: pending,
    )
    service._activate_effect_run = activated.append

    status = service._drive_engine_owned("run-1", binding=binding, snapshot=pending)

    assert status.status == "running"
    assert service.coordinator.calls == 1
    assert activated == ["run-1"]


def test_engine_progress_recovers_capacity_bound_consumed_facts_in_one_sweep() -> None:
    """Cleanup capacity is independent of the ordinary progress decision budget."""
    completed = NativeSnapshot(
        values={"lockstep_outcome": "PASS"}, checkpoint_id="cp-2"
    )
    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")

    class Coordinator:
        def __init__(self):
            self.calls = 0

        def reconcile_consumed(self, _run_id):
            self.calls += 1
            return tuple(
                SimpleNamespace(action="delivered") for _index in range(128)
            )

    coordinator = Coordinator()
    deactivated = []

    class NoWatchAcknowledgement:
        def acknowledge_dispatch_watch(self, _run_id):
            pytest.fail("EngineDriveService used the retired watch acknowledgement")

        def acknowledge_run_drive_watch(self, _run_id):
            pytest.fail("EngineDriveService bypassed RecoveryDriver cleanup")

    service = _service_double()
    service.effects = NoWatchAcknowledgement()
    service.leases = ()
    service.coordinator = coordinator
    service.runtime = SimpleNamespace(
        decision_guard=lambda _run_id: nullcontext(),
        snapshot=lambda *_args, **_kwargs: completed
    )
    service._deactivate_effect_run = deactivated.append

    status = service._drive_engine_owned(
        "run-1", binding=binding, snapshot=completed
    )

    assert status.status == "completed"
    assert coordinator.calls == 1
    assert deactivated == ["run-1"]


def test_protected_manual_done_uses_coordinator_not_direct_native_resume(
    tmp_path,
) -> None:
    state = initialize_owner_state(tmp_path / "state")
    sessions.touch(state, "run-1", "session-1", 30)
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    interrupt = NativeInterrupt(
        coordinate,
        {
            "lockstep_effect": {
                "schema": "lockstep.effect/v1",
                "kind": "manual",
                "logical_id": "edit",
                "runner": None,
                "inputs": {},
                "writes": ["src/"],
                "artifacts": [],
                "deadline_seconds": None,
                "scope_state_keys": [],
                "result_schema": "lockstep.effect-result/v1",
            },
        },
    )
    binding = RunBinding(
        "run-1", "thread-1", "a" * 64, "bundle:" + "b" * 64, str(tmp_path)
    )

    class Coordinator:
        def __init__(self):
            self.calls = []

        def submit_manual(self, run_id, source, submission):
            self.calls.append((run_id, source, submission.kind))
            return ScenarioStatus("completed", run_id, "engine", None)

    class Runtime:
        def resume(self, *_args, **_kwargs):
            raise AssertionError("protected manual result bypassed the coordinator")

    class Leases:
        def __init__(self):
            self.calls = []

        def acquire(self, scope, key, owner, ttl):
            self.calls.append((scope, key, owner, ttl))
            return object()

        def release(self, _lease):
            return None

    service = _service_double()
    service.state_dir = state
    service.runtime = Runtime()
    service.coordinator = Coordinator()
    service.leases = Leases()
    service.catalog = SimpleNamespace(get=lambda _run_id: binding)
    service._bind_existing = lambda *_args: nullcontext(binding)
    service._worker_interrupt = lambda *_args: (binding, interrupt)
    service._drive_engine_owned = lambda *_args, **_kwargs: ScenarioStatus(
        "completed", "run-1", "engine", None
    )
    service._admission_recovery_lock = threading.RLock()
    service._closed = False

    completed = service.scenario_done(
        "run-1",
        "edit",
        {"reviewed": True},
        session_id="session-1",
        project=str(tmp_path),
    )

    assert completed["status"] == "completed"
    assert service.coordinator.calls == [("run-1", coordinate, "done")]
