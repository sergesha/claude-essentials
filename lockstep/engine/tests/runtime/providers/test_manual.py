from __future__ import annotations

from datetime import UTC, datetime
from itertools import count
from pathlib import Path

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.native_models import (
    NativeCoordinate,
    NativeInterrupt,
    NativeSnapshot,
)
from lockstep.runtime.storage import SQLiteStore
from tests.runtime.effects.test_coordinator import FakeRuntime
from tests.runtime.providers.fakes import FakeEffectAuthority


def _manual_descriptor() -> dict[str, object]:
    return {
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


def test_manual_handoff_captures_baseline_before_allowed_edit(tmp_path: Path) -> None:
    from lockstep.runtime.providers.manual import ManualProvider, ManualSubmission

    owner = tmp_path / "owner"
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    target = project / "src/app.py"
    target.write_text("VALUE = 1\n")
    binding = RunBinding(
        "run-1", "thread-1", "a" * 64, "bundle:" + "b" * 64, str(project)
    )
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    descriptor = parse_effect_descriptor(_manual_descriptor())
    interrupt = NativeInterrupt(coordinate, {"lockstep_effect": _manual_descriptor()})
    provider = ManualProvider(owner, BlobStore(owner))

    handoff = provider.prepare_handoff(binding, interrupt, descriptor)
    target.write_text("VALUE = 2\n")
    result = provider.submit(
        handoff,
        ManualSubmission.build("PASS", evidence={"reviewed": True}),
    )

    assert result.effect_id == handoff.effect_id
    assert result.outcome == "PASS"
    assert result.fixed_error_code is None
    assert len(result.evidence_refs) == 1


def test_manual_manifest_is_checked_on_fail_not_only_pass(tmp_path: Path) -> None:
    from lockstep.runtime.providers.manual import ManualProvider, ManualSubmission

    owner = tmp_path / "owner"
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "outside.txt").write_text("before")
    binding = RunBinding(
        "run-1", "thread-1", "a" * 64, "bundle:" + "b" * 64, str(project)
    )
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    descriptor = parse_effect_descriptor(_manual_descriptor())
    interrupt = NativeInterrupt(coordinate, {"lockstep_effect": _manual_descriptor()})
    provider = ManualProvider(owner, BlobStore(owner))
    handoff = provider.prepare_handoff(binding, interrupt, descriptor)
    (project / "outside.txt").write_text("forbidden")

    result = provider.submit(
        handoff,
        ManualSubmission.build("FAIL", reason="blocked"),
    )

    assert result.outcome == "ERROR"
    assert result.fixed_error_code == "manifest_invalid"


def test_manual_handoff_restart_reuses_original_baseline(tmp_path: Path) -> None:
    from lockstep.runtime.providers.manual import ManualProvider

    owner = tmp_path / "owner"
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    target = project / "src/app.py"
    target.write_text("VALUE = 1\n")
    binding = RunBinding(
        "run-1", "thread-1", "a" * 64, "bundle:" + "b" * 64, str(project)
    )
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    descriptor = parse_effect_descriptor(_manual_descriptor())
    interrupt = NativeInterrupt(coordinate, {"lockstep_effect": _manual_descriptor()})
    first = ManualProvider(owner, BlobStore(owner)).prepare_handoff(
        binding, interrupt, descriptor
    )
    target.write_text("VALUE = 2\n")

    restarted = ManualProvider(owner, BlobStore(owner)).prepare_handoff(
        binding, interrupt, descriptor
    )

    assert restarted == first
    assert restarted.baseline.sha256 == first.baseline.sha256


def test_coordinator_prepares_then_seals_manual_through_the_same_ledger(
    tmp_path: Path,
) -> None:
    from lockstep.runtime.effects.coordinator import EffectCoordinator
    from lockstep.runtime.effects.ledger import EffectLedger
    from lockstep.runtime.leases import LeaseStore
    from lockstep.runtime.providers.manual import ManualProvider, ManualSubmission

    now = datetime(2026, 8, 21, 10, tzinfo=UTC)
    owner = tmp_path / "owner"
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    target = project / "src/app.py"
    target.write_text("VALUE = 1\n")
    store = SQLiteStore(owner / "runtime.sqlite")
    try:
        from lockstep.runtime.catalog import RunCatalog

        catalog = RunCatalog(store, clock=lambda: now)
        binding = catalog.create(
            RunBinding(
                "run-1",
                "thread-1",
                "a" * 64,
                "bundle:" + "b" * 64,
                str(project),
            )
        )
        coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
        interrupt = NativeInterrupt(
            coordinate, {"lockstep_effect": _manual_descriptor()}
        )
        runtime = FakeRuntime(
            binding,
            NativeSnapshot(values={}, pending=(interrupt,), checkpoint_id="cp-1"),
        )
        ledger = EffectLedger(store, clock=lambda: now)
        manual = ManualProvider(owner, BlobStore(owner))
        owners = count()
        coordinator = EffectCoordinator(
            runtime=runtime,
            catalog=catalog,
            ledger=ledger,
            leases=LeaseStore(store, clock=lambda: now),
            runners={},
            authority=FakeEffectAuthority(clock=lambda: now),
            manual=manual,
            clock=lambda: now,
            owner_factory=lambda: f"owner-{next(owners)}",
        )

        prepared = coordinator.reconcile("run-1")
        assert prepared.action == "prepared"
        assert manual.lookup(prepared.effect_id).coordinate == coordinate
        assert ledger.get(prepared.effect_id).phase == "prepared"
        assert runtime.resume_calls == []

        target.write_text("VALUE = 2\n")
        status = coordinator.submit_manual(
            "run-1",
            coordinate,
            ManualSubmission.build("PASS", evidence={"reviewed": True}),
        )

        assert status.status == "completed"
        assert ledger.get(prepared.effect_id).phase == "delivered"
        assert runtime.resume_calls[0][2]["int-1"]["outcome"] == "PASS"
    finally:
        store.close()
