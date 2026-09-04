import json
from types import SimpleNamespace

import pytest

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_descriptor,
)
from lockstep.runtime.effects.ledger import EffectPhase
from lockstep.runtime.native_models import (
    NativeCoordinate,
    NativeInterrupt,
    NativeSnapshot,
)
from lockstep.runtime.status import project_status


def _binding() -> RunBinding:
    return RunBinding("run-1", "thread-1", "a" * 64, "b" * 64, "/project")


def _parked(value: object = "Work?") -> NativeSnapshot:
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    return NativeSnapshot(
        values={}, pending=(NativeInterrupt(coordinate, value),), next=("work",)
    )


def test_child_correlation_is_opaque_and_contains_no_native_namespace() -> None:
    coordinate = NativeCoordinate(
        "thread-1", "checkpoint-secret", "child:namespace-secret",
        "task-secret", "interrupt-secret",
    )
    snapshot = NativeSnapshot(
        values={}, pending=(NativeInterrupt(coordinate, "Work?"),), next=("work",)
    )

    projected = project_status(_binding(), snapshot, (), ()).to_dict()

    assert projected["child_run_id"].startswith("child-")
    assert "namespace-secret" not in projected["child_run_id"]
    assert "checkpoint-secret" not in projected["child_run_id"]


def test_child_correlation_is_stable_but_never_a_public_catalog_run(tmp_path) -> None:
    from lockstep.runtime.catalog import RunCatalog
    from lockstep.runtime.storage import SQLiteStore

    first = NativeInterrupt(
        NativeCoordinate("thread-1", "cp-1", "child:stable-task", "task-a", "int-a"),
        "Work?",
    )
    later = NativeInterrupt(
        NativeCoordinate("thread-1", "cp-2", "child:stable-task", "task-b", "int-b"),
        "Work again?",
    )
    first_id = project_status(
        _binding(), NativeSnapshot(values={}, pending=(first,)), (), ()
    ).to_dict()["child_run_id"]
    later_id = project_status(
        _binding(), NativeSnapshot(values={}, pending=(later,)), (), ()
    ).to_dict()["child_run_id"]
    assert first_id == later_id

    store = SQLiteStore(tmp_path / "runtime.sqlite")
    try:
        catalog = RunCatalog(store)
        catalog.create(_binding())
        with pytest.raises(KeyError):
            catalog.get(first_id)
        assert [item.public_run_id for item in catalog.list("/project")] == ["run-1"]
    finally:
        store.close()


def test_status_maps_native_outcomes_and_active_engine_work():
    binding = _binding()
    assert (
        project_status(binding, NativeSnapshot(values={}), (), ()).status == "starting"
    )
    assert (
        project_status(
            binding, NativeSnapshot(values={}, next=("node",)), (), ()
        ).status
        == "running"
    )
    assert (
        project_status(
            binding, NativeSnapshot(values={"lockstep_outcome": "PASS"}), (), ()
        ).status
        == "completed"
    )
    assert (
        project_status(
            binding, NativeSnapshot(values={"lockstep_outcome": "FAIL"}), (), ()
        ).status
        == "escalated"
    )
    assert (
        project_status(
            binding, NativeSnapshot(values={"lockstep_outcome": "ERROR"}), (), ()
        ).status
        == "escalated"
    )
    assert (
        project_status(
            binding, NativeSnapshot(values={"lockstep_outcome": "ABORTED"}), (), ()
        ).status
        == "aborted"
    )


def test_untrusted_outcome_cannot_override_active_native_coordinates():
    binding = _binding()
    parked = _parked()
    spoofed = NativeSnapshot(
        values={"lockstep_outcome": "PASS"},
        pending=parked.pending,
        next=parked.next,
    )
    assert project_status(binding, spoofed, (), ()).status == "awaiting"
    unknown = project_status(
        binding, NativeSnapshot(values={"lockstep_outcome": "SURPRISE"}), (), ()
    )
    assert unknown.status == "escalated"
    assert unknown.to_dict()["integrity_error"] == "unknown_terminal_outcome"


def test_protected_engine_interrupt_is_not_exposed_as_worker_authority():
    protected = {
        "lockstep_effect": {
            "schema": "lockstep.effect/v1",
            "kind": "managed",
            "logical_id": "implement",
        }
    }
    status = project_status(_binding(), _parked(protected), (), ())
    assert status.status == "running"
    assert status.owner == "engine"
    assert status.next_action == "scenario_wait"


def test_pinned_status_exposes_only_compiler_logical_command_and_ledger_phase():
    raw = {
        "schema": "lockstep.effect/v1",
        "kind": "pinned",
        "logical_id": "unit-tests",
        "runner": {
            "selector": "pinned",
            "required_capabilities": ["workspace", "bounded_result", "sandbox"],
        },
        "inputs": {
            "command": {"state_key": "pinned_command"},
            "snapshot": {"state_key": "snapshot_input"},
        },
        "writes": [],
        "artifacts": [],
        "deadline_seconds": 60,
        "scope_state_keys": [],
        "result_schema": "lockstep.effect-result/v1",
    }
    parked = _parked({"lockstep_effect": raw})
    descriptor = parse_effect_descriptor(raw)
    effect_id = derive_effect_id(parked.pending[0].coordinate, descriptor.digest)

    class Effects:
        def get(self, requested):
            assert requested == effect_id
            return SimpleNamespace(
                coordinate=parked.pending[0].coordinate,
                descriptor_digest=descriptor.digest,
                effect_kind="pinned",
                phase=EffectPhase.RUNNING,
            )

    snapshot = NativeSnapshot(
        values={
            "pinned_command": {
                "schema": "lockstep.pinned-command/v1",
                "logical_argv": ["python", "-m", "pytest", "-q"],
                "logical_cwd": ".",
                "result_source": "exit",
            },
            "snapshot_input": "secret-snapshot-ref",
        },
        pending=parked.pending,
        next=parked.next,
    )

    public = project_status(_binding(), snapshot, (), Effects()).to_dict()

    assert public["gate_execution"] == {
        "operation_id": effect_id,
        "execution_class": "pinned-validator",
        "logical_argv": ["python", "-m", "pytest", "-q"],
        "logical_cwd": ".",
        "phase": "running",
    }
    public_phase = public["gate_execution"]["phase"]
    assert type(public_phase) is str
    assert (
        json.dumps(
            {"phase": public_phase},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        == b'{"phase":"running"}'
    )
    rendered = repr(public)
    assert "secret-snapshot-ref" not in rendered
    assert "workspace_path" not in rendered
    assert "environment" not in rendered
