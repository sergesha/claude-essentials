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
from lockstep.runtime.recovery_driver import RecoveryDriver as _RecoveryDriver
from lockstep.runtime.service import LockstepError, LockstepCommandService
from lockstep.runtime.status import ScenarioStatus


def _service_double() -> LockstepCommandService:
    service = object.__new__(LockstepCommandService)
    service._activation_lock = threading.RLock()  # noqa: SLF001
    service._writable_core_active = True  # noqa: SLF001
    service._initial_recovery_exclusion = None  # noqa: SLF001
    service._recovery_thread_cursor = None  # noqa: SLF001
    service._recovery_driver = _RecoveryDriver()  # noqa: SLF001
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


def test_startup_recovery_discovers_native_start_commit_before_ledger_prepare() -> None:
    from lockstep.runtime.blobs import BlobRef
    from lockstep.runtime.effects.ledger import EffectDispatchWatch

    binding = RunBinding("run-1", "thread-1", "a" * 64, "bundle", "/project")
    driven = []
    bound = []
    unbound = []
    service = _service_double()
    watch = EffectDispatchWatch(
        "run-1", BlobRef("b" * 64, 2), datetime(2026, 8, 20, tzinfo=UTC)
    )
    service.effects = SimpleNamespace(
        list_dispatch_watches=lambda **_kwargs: (watch,),
        list_recovery_threads=lambda **_kwargs: (),
    )
    service.catalog = SimpleNamespace(
        get=lambda _run_id: binding,
        find_by_thread=lambda _thread_id: pytest.fail("ledger unexpectedly populated"),
    )
    service.blobs = SimpleNamespace(read=lambda _ref: b"{}")
    service.runtime = SimpleNamespace(
        bind=bound.append,
        unbind=unbound.append,
        ensure_started=lambda _run_id, _values: SimpleNamespace(),
    )
    service._active_effect_runs = set()
    service._queued_effect_runs = set()
    service._active_effect_lock = threading.Lock()
    service._admission_recovery_lock = threading.RLock()
    service._recovery_thread_cursor = None

    def drive(run_id, **_kwargs):
        driven.append(run_id)
        service._deactivate_effect_run(run_id)

    service._drive_engine_owned = drive

    service._recover_engine_effects()

    assert bound == [binding]
    assert driven == ["run-1"]
    assert unbound == ["run-1"]


def test_dispatch_recovery_serializes_with_foreground_admission() -> None:
    service = _service_double()
    service._admission_recovery_lock = threading.RLock()
    entered = threading.Event()
    finished = threading.Event()
    service._recover_start_admissions = entered.set
    service._recover_effect_batch = lambda: None

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


def test_runtime_reconstruction_tracks_the_bounded_recovery_page() -> None:
    service = _service_double()
    service._runtime_execution_context = None
    service._recovery_thread_cursor = "thread-128"
    service._admission_recovery_lock = threading.RLock()
    observed = []
    service._reconstruct_runtime_execution_context = (
        lambda *, after_thread_id=None, limit=None: observed.append(
            (after_thread_id, limit)
        )
    )
    service._recover_start_admissions = lambda: None
    service._recover_effect_batch = lambda: None

    service._recover_engine_effects()

    assert observed == [("thread-128", None)]


def test_parallel_recovery_installs_one_runtime_composition() -> None:
    service = _service_double()
    service._runtime_execution_context = None
    service._recovery_thread_cursor = "thread-128"
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
    service._recover_start_admissions = lambda: None
    service._recover_effect_batch = lambda: None

    workers = [threading.Thread(target=service._recover_engine_effects) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=1)

    assert all(not worker.is_alive() for worker in workers)
    assert reconstruct_calls == [("thread-128", None), ("thread-128", None)]
    assert install_calls == [reconstructed]


def test_recovery_rejects_a_different_preinstalled_runtime_context() -> None:
    service = _service_double()
    service._recovery_thread_cursor = "thread-129"
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
    service._bind_existing = lambda *_args: binding
    service._worker_interrupt = lambda *_args: (binding, interrupt)

    def resume(*_args, **_kwargs):
        foreground_late.set()
        assert release.wait(1)
        return NativeSnapshot(values={"lockstep_outcome": "PASS"}, checkpoint_id="cp-2")

    service.runtime = SimpleNamespace(
        resume=resume,
        unbind=lambda _run_id: recovery_unbound.set(),
    )
    service._recover_start_admissions = lambda: service.runtime.unbind("run-1")
    service._recover_effect_batch = lambda: None
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
    service._bind_existing = lambda *_args: binding
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
    service._recover_start_admissions = lambda: service.runtime.unbind("run-1")
    service._recover_effect_batch = lambda: None
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


def test_start_recovery_defers_before_native_commit_when_active_batch_is_full() -> None:
    from lockstep.runtime.blobs import BlobRef
    from lockstep.runtime.effects.ledger import EffectDispatchWatch

    binding = RunBinding("deferred", "thread-deferred", "a" * 64, "bundle", "/p")
    watch = EffectDispatchWatch(
        "deferred", BlobRef("b" * 64, 2), datetime(2026, 8, 20, tzinfo=UTC)
    )
    service = _service_double()
    service.effects = SimpleNamespace(list_dispatch_watches=lambda **_kwargs: (watch,))
    service.catalog = SimpleNamespace(get=lambda _run_id: binding)
    service.blobs = SimpleNamespace(
        read=lambda _ref: pytest.fail("capacity rejection consumed start input")
    )
    service.runtime = SimpleNamespace(
        bind=lambda _binding: pytest.fail("capacity rejection bound native app"),
        ensure_started=lambda *_args: pytest.fail("capacity rejection invoked native"),
    )
    service._active_effect_runs = {
        f"run-{index}" for index in range(service._MAX_ACTIVE_EFFECT_RUNS)
    }
    service._queued_effect_runs = set(service._active_effect_runs)
    service._active_effect_lock = threading.Lock()
    service._pump_wakeup = threading.Event()

    service._recover_start_admissions()

    assert "deferred" not in service._active_effect_runs
    assert not service._pump_wakeup.is_set()


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
    service.runtime = SimpleNamespace(snapshot=lambda *_args, **_kwargs: snapshot)
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


def test_effect_recovery_cursor_does_not_skip_a_capacity_deferred_run() -> None:
    binding = RunBinding("deferred", "thread-deferred", "a" * 64, "bundle", "/p")
    service = _service_double()
    service.effects = SimpleNamespace(
        list_recovery_threads=lambda **_kwargs: ("thread-deferred",)
    )
    service.catalog = SimpleNamespace(find_by_thread=lambda _thread_id: binding)
    service.runtime = SimpleNamespace(
        bind=lambda _binding: pytest.fail("capacity-deferred effect was bound")
    )
    service._active_effect_runs = {
        f"run-{index}" for index in range(service._MAX_ACTIVE_EFFECT_RUNS)
    }
    service._active_effect_lock = threading.Lock()
    service._recovery_thread_cursor = None

    service._recover_effect_batch()

    assert service._recovery_thread_cursor is None
    assert "deferred" not in service._active_effect_runs


def test_scenario_recover_selects_active_effect_threads_before_catalog_limit(
    tmp_path,
) -> None:
    from lockstep.runtime.catalog import RunCatalog
    from lockstep.runtime.effects.ledger import EffectLedger
    from lockstep.runtime.storage import SQLiteStore

    project = tmp_path / "project"
    project.mkdir()
    project_identity = str(project.resolve())
    store = SQLiteStore(tmp_path / "runtime.db")
    try:
        catalog = RunCatalog(store)
        effects = EffectLedger(store)
        for index in range(128):
            catalog.create(
                RunBinding(
                    f"terminal-{index:03}",
                    f"thread-terminal-{index:03}",
                    "a" * 64,
                    "bundle:" + "b" * 64,
                    project_identity,
                    f"2026-08-20T10:{index // 60:02}:{index % 60:02}+00:00",
                )
            )
        active = catalog.create(
            RunBinding(
                "active-late",
                "thread-active-late",
                "a" * 64,
                "bundle:" + "b" * 64,
                project_identity,
                "2026-08-20T10:03:00+00:00",
            )
        )
        descriptor = parse_effect_descriptor(
            {
                "schema": "lockstep.effect/v1",
                "kind": "managed",
                "logical_id": "work",
                "runner": {
                    "selector": "codex",
                    "required_capabilities": ["workspace", "bounded_result"],
                },
                "inputs": {"brief": {"state_key": "brief"}},
                "writes": ["src/"],
                "artifacts": [],
                "deadline_seconds": 300,
                "scope_state_keys": [],
                "result_schema": "lockstep.effect-result/v1",
            }
        )
        effects.prepare(
            NativeCoordinate(
                active.thread_id, "checkpoint", "", "task", "interrupt"
            ),
            descriptor,
            deadline_at=datetime(2030, 1, 1, tzinfo=UTC),
            runner_binding_digest="c" * 64,
            workspace_ref="snapshot:" + "d" * 64,
            request_digest="e" * 64,
            grant_digest="f" * 64,
        )
        driven: list[str] = []
        service = _service_double()
        service.catalog = catalog
        service.effects = effects
        service.runtime = SimpleNamespace(bind=lambda _binding: None)
        service.coordinator = SimpleNamespace(MAX_DUE_PER_SCAN=128)
        service._drive_engine_owned = lambda run_id, **_kwargs: driven.append(run_id)
        service._admission_recovery_lock = threading.RLock()
        service._scenario_recovery_cursors = {}
        service._active_effect_runs = set()
        service._active_effect_lock = threading.Lock()

        result = service.scenario_recover(project_identity, limit=128)

        assert result["recovered"] == ["active-late"]
        assert driven == ["active-late"]
    finally:
        store.close()


def test_scenario_recover_pages_nonterminal_threads_with_stable_project_progress() -> None:
    project_identity = str(Path("/project").resolve())
    foreign = RunBinding(
        "foreign", "thread-a", "a" * 64, "bundle:" + "b" * 64, "/foreign"
    )
    local = RunBinding(
        "local", "thread-b", "a" * 64, "bundle:" + "b" * 64, project_identity
    )
    pages = {None: ("thread-a",), "thread-a": ("thread-b",)}
    cursors: list[str | None] = []

    def list_recovery_threads(*, limit: int, after_thread_id: str | None = None):
        assert limit == 1
        cursors.append(after_thread_id)
        return pages[after_thread_id]

    bindings = {foreign.thread_id: foreign, local.thread_id: local}
    driven: list[str] = []
    validated: list[tuple[str | None, int | None]] = []
    service = _service_double()
    service.effects = SimpleNamespace(list_recovery_threads=list_recovery_threads)
    service.catalog = SimpleNamespace(
        list=lambda *_args, **_kwargs: pytest.fail(
            "scenario recovery limited the unfiltered run catalog"
        ),
        find_by_thread=lambda thread_id: bindings[thread_id],
    )
    service.runtime = SimpleNamespace(bind=lambda _binding: None)
    service._drive_engine_owned = lambda run_id, **_kwargs: driven.append(run_id)
    service._admission_recovery_lock = threading.RLock()
    service._scenario_recovery_cursors = {}
    service._active_effect_runs = set()
    service._active_effect_lock = threading.Lock()
    service._install_recovered_runtime_execution = (
        lambda *, after_thread_id=None, limit=None: validated.append(
            (after_thread_id, limit)
        )
    )

    first = service.scenario_recover(project_identity, limit=1)
    second = service.scenario_recover(project_identity, limit=1)

    assert first["recovered"] == []
    assert second["recovered"] == ["local"]
    assert cursors == [None, "thread-a"]
    assert validated == [(None, 1), ("thread-a", 1)]
    assert driven == ["local"]


def test_scenario_recover_capacity_deferral_does_not_advance_or_report_recovery() -> None:
    project_identity = str(Path("/project").resolve())
    binding = RunBinding(
        "deferred",
        "thread-deferred",
        "a" * 64,
        "bundle:" + "b" * 64,
        project_identity,
    )

    def list_recovery_threads(*, limit: int, after_thread_id: str | None = None):
        assert limit == 1
        return (binding.thread_id,) if after_thread_id is None else ()

    bound: list[RunBinding] = []
    driven: list[str] = []
    service = _service_double()
    service.effects = SimpleNamespace(list_recovery_threads=list_recovery_threads)
    service.catalog = SimpleNamespace(find_by_thread=lambda _thread_id: binding)
    service.runtime = SimpleNamespace(bind=bound.append)
    service._drive_engine_owned = lambda run_id, **_kwargs: driven.append(run_id)
    service._admission_recovery_lock = threading.RLock()
    service._scenario_recovery_cursors = {}
    service._active_effect_runs = {
        f"active-{index}" for index in range(service._MAX_ACTIVE_EFFECT_RUNS)
    }
    service._active_effect_lock = threading.Lock()

    first = service.scenario_recover(project_identity, limit=1)

    assert first["recovered"] == []
    assert service._scenario_recovery_cursors == {}
    assert bound == []
    assert driven == []

    service._active_effect_runs.clear()
    second = service.scenario_recover(project_identity, limit=1)

    assert second["recovered"] == ["deferred"]
    assert service._scenario_recovery_cursors == {
        project_identity: binding.thread_id
    }
    assert bound == [binding]
    assert driven == ["deferred"]


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
    service.runtime = SimpleNamespace(snapshot=lambda *_args, **_kwargs: snapshot)
    service._deactivate_effect_run = lambda _run_id: None
    service._ack_start_if_observable = lambda *_args: None

    status = service._drive_engine_owned("run-1", binding=binding, snapshot=snapshot)

    assert status.status == "awaiting"
    assert status.owner == "worker"
    assert service.coordinator.calls == 1


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
        snapshot=lambda *_args, **_kwargs: state["snapshot"]
    )
    service._deactivate_effect_run = lambda _run_id: None
    service._ack_start_if_observable = lambda *_args: None

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
    service.runtime = SimpleNamespace(snapshot=lambda *_args, **_kwargs: pending)
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
    service = _service_double()
    service.effects = ()
    service.leases = ()
    service.coordinator = coordinator
    service.runtime = SimpleNamespace(
        snapshot=lambda *_args, **_kwargs: completed
    )
    service._deactivate_effect_run = deactivated.append
    service._ack_start_if_observable = lambda *_args: None

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
    service._bind_existing = lambda *_args: binding
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
