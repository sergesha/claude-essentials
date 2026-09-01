from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import lockstep.runtime.runtime_execution_recovery as recovery_module
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
from lockstep.runtime.runtime_execution_recovery import (
    RuntimeExecutionRecovery,
    _ProtectedRecoveryWork,
)


def _recovery(*, effects, catalog=None) -> RuntimeExecutionRecovery:
    instance = object.__new__(RuntimeExecutionRecovery)
    instance._state_dir = Path("/owner-state")
    instance._catalog = catalog or SimpleNamespace()
    instance._resolver = SimpleNamespace()
    instance._effects = effects
    return instance


def test_durable_recovery_page_uses_cursor_and_rejects_overflow() -> None:
    binding = RunBinding("run", "thread-129", "a" * 64, "bundle", "/project")
    observed = []
    effects = SimpleNamespace(
        max_run_drive_admission_seq=lambda: None,
        list_run_drive_watches=lambda **_kwargs: (),
        list_recovery_threads=lambda **kwargs: (
            observed.append(kwargs) or ("thread-129",)
        ),
        list_nonterminal_for_thread=lambda _thread, **_kwargs: (object(),) * 3,
    )
    recovery = _recovery(
        effects=effects,
        catalog=SimpleNamespace(find_by_thread=lambda _thread: binding),
    )

    with pytest.raises(
        ValueError, match="bounded nonterminal effect capacity"
    ):
        recovery._durable_runs(limit=2, after_thread_id="thread-128")

    assert observed == [{"limit": 2, "after_thread_id": "thread-128"}]


def test_durable_recovery_discovers_null_input_v2_watch() -> None:
    binding = RunBinding("run", "thread", "a" * 64, "bundle", "/project")
    observed = []
    effects = SimpleNamespace(
        max_run_drive_admission_seq=lambda: 1,
        list_run_drive_watches=lambda **kwargs: (
            observed.append(kwargs)
            or (
                SimpleNamespace(
                    admission_seq=1,
                    public_run_id="run",
                    input_blob_sha256=None,
                    input_blob_size=None,
                ),
            )
        ),
        list_recovery_threads=lambda **_kwargs: (),
    )
    recovery = _recovery(
        effects=effects,
        catalog=SimpleNamespace(get=lambda run_id: binding),
    )

    assert recovery._durable_runs(limit=128, after_thread_id=None) == (
        (binding, True, ()),
    )
    assert observed == [
        {"after_admission_seq": 0, "high_water": 1, "limit": 128}
    ]


def test_watch_discovery_pages_past_128_unprotected_runs() -> None:
    bindings = {
        f"run-{index:03d}": RunBinding(
            f"run-{index:03d}",
            f"thread-{index:03d}",
            "a" * 64,
            "bundle:" + "b" * 64,
            "/project",
        )
        for index in range(1, 130)
    }
    watches = tuple(
        SimpleNamespace(admission_seq=index, public_run_id=f"run-{index:03d}")
        for index in range(1, 130)
    )
    requirement = SimpleNamespace(
        protected_descriptor_digest="d" * 64,
        runner_selector="codex",
    )
    protected_index = SimpleNamespace(
        project_identity="/project", requirements=(requirement,)
    )
    empty_index = SimpleNamespace(project_identity="/project", requirements=())
    pages = []
    max_calls = []

    def capture_max():
        max_calls.append(True)
        if len(max_calls) > 1:
            raise AssertionError("watch high-water was captured more than once")
        return 129

    def list_watches(*, after_admission_seq, high_water, limit):
        pages.append((after_admission_seq, high_water, limit))
        return tuple(
            watch
            for watch in watches
            if after_admission_seq < watch.admission_seq <= high_water
        )[:limit]

    effects = SimpleNamespace(
        max_run_drive_admission_seq=capture_max,
        list_run_drive_watches=list_watches,
        list_recovery_threads=lambda **_kwargs: (),
    )
    recovery = _recovery(
        effects=effects,
        catalog=SimpleNamespace(get=lambda run_id: bindings[run_id]),
    )
    recovery._resolver = SimpleNamespace(
        index=lambda binding: (
            protected_index
            if binding.public_run_id == "run-129"
            else empty_index
        )
    )

    assert recovery._protected_work(
        limit=128, after_thread_id=None
    ) == (_ProtectedRecoveryWork(protected_index, ()),)
    assert max_calls == [True]
    assert pages == [(0, 129, 128), (128, 129, 128)]


def test_watch_discovery_stops_materializing_at_protected_limit() -> None:
    bindings = {
        f"run-{index}": RunBinding(
            f"run-{index}",
            f"thread-{index}",
            "a" * 64,
            "bundle:" + "b" * 64,
            "/project",
        )
        for index in range(1, 4)
    }
    watches = tuple(
        SimpleNamespace(admission_seq=index, public_run_id=f"run-{index}")
        for index in range(1, 4)
    )
    requirement = SimpleNamespace(
        protected_descriptor_digest="d" * 64,
        runner_selector="codex",
    )
    index = SimpleNamespace(
        project_identity="/project", requirements=(requirement,)
    )
    pages = []
    materialized = []

    def list_watches(*, after_admission_seq, high_water, limit):
        pages.append((after_admission_seq, high_water, limit))
        return watches

    def get_binding(run_id):
        materialized.append(("catalog", run_id))
        return bindings[run_id]

    def resolve_index(binding):
        materialized.append(("resolver", binding.public_run_id))
        return index

    recovery = _recovery(
        effects=SimpleNamespace(
            max_run_drive_admission_seq=lambda: 3,
            list_run_drive_watches=list_watches,
            list_recovery_threads=lambda **_kwargs: (),
        ),
        catalog=SimpleNamespace(get=get_binding),
    )
    recovery._resolver = SimpleNamespace(index=resolve_index)

    work = recovery._protected_work(limit=2, after_thread_id=None)

    assert len(work) == 2
    assert pages == [(0, 3, 128)]
    assert materialized == [
        ("catalog", "run-1"),
        ("resolver", "run-1"),
        ("catalog", "run-2"),
        ("resolver", "run-2"),
    ]


@pytest.mark.parametrize(
    ("record", "message"),
    (
        (
            SimpleNamespace(
                descriptor_digest="missing", effect_kind="managed"
            ),
            "absent from immutable bundle",
        ),
        (
            SimpleNamespace(
                descriptor_digest="descriptor", effect_kind="verify"
            ),
            "differs from immutable selector",
        ),
    ),
)
def test_protected_record_matching_fails_closed(record, message: str) -> None:
    requirement = SimpleNamespace(
        protected_descriptor_digest="descriptor", runner_selector="codex"
    )
    index = SimpleNamespace(requirements=(requirement,))

    with pytest.raises(ValueError, match=message):
        _recovery(effects=SimpleNamespace())._match_records(index, (record,))


def test_reconstruction_validates_every_project_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = ("/project-a", "/project-b")
    work = tuple(
        _ProtectedRecoveryWork(RuntimeRequirementIndex(project, ()), ())
        for project in projects
    )
    captured = SimpleNamespace(codex_facts=object(), pinned_facts=object())
    observed = []
    recovery = _recovery(effects=SimpleNamespace())
    recovery._protected_work = lambda **_kwargs: work
    monkeypatch.setattr(
        recovery_module,
        "open_runtime_snapshot",
        lambda _state_dir: ("a" * 64, object()),
    )
    monkeypatch.setattr(
        recovery_module,
        "capture_runtime_execution_bindings",
        lambda _snapshot, *, project: observed.append(project) or captured,
    )
    monkeypatch.setattr(
        recovery_module,
        "OwnerRuntimeAuthority",
        lambda **_kwargs: SimpleNamespace(preflight=lambda _index: None),
    )

    context = recovery.reconstruct(limit=2)

    assert observed == [Path(project) for project in projects]
    assert context is not None
    assert context.bindings is captured


def test_reconstruction_rejects_durable_runner_binding_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirement = SimpleNamespace(runner_selector="codex")
    record = SimpleNamespace(runner_binding_digest="stale-binding")
    index = SimpleNamespace(project_identity="/project")
    work = (_ProtectedRecoveryWork(index, ((record, requirement),)),)
    snapshot = SimpleNamespace(
        codex=SimpleNamespace(binding_digest="current-codex"),
        pinned=SimpleNamespace(binding_digest="current-pinned"),
    )
    captured = SimpleNamespace(codex_facts=object(), pinned_facts=object())
    recovery = _recovery(effects=SimpleNamespace())
    recovery._protected_work = lambda **_kwargs: work
    monkeypatch.setattr(
        recovery_module,
        "open_runtime_snapshot",
        lambda _state_dir: ("a" * 64, snapshot),
    )
    monkeypatch.setattr(
        recovery_module,
        "capture_runtime_execution_bindings",
        lambda _snapshot, *, project: captured,
    )
    monkeypatch.setattr(
        recovery_module,
        "OwnerRuntimeAuthority",
        lambda **_kwargs: SimpleNamespace(preflight=lambda _index: None),
    )

    with pytest.raises(ValueError, match="different owner runner binding"):
        recovery.reconstruct(limit=1)
