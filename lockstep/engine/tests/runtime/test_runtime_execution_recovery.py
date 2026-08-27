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
