from lockstep.runtime.catalog import RunBinding
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
    return NativeSnapshot(values={}, pending=(NativeInterrupt(coordinate, value),), next=("work",))


def test_status_is_derived_not_catalogued(tmp_path):
    from lockstep.runtime.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "runtime.sqlite")
    try:
        status = project_status(_binding(), _parked(), (), ())
        assert status.status == "awaiting"
        assert "status" not in store.tables.runs.columns
    finally:
        store.close()


def test_status_maps_native_outcomes_and_active_engine_work():
    binding = _binding()
    assert project_status(binding, NativeSnapshot(values={}), (), ()).status == "starting"
    assert project_status(
        binding, NativeSnapshot(values={}, next=("node",)), (), ()
    ).status == "running"
    assert project_status(
        binding, NativeSnapshot(values={"lockstep_outcome": "PASS"}), (), ()
    ).status == "completed"
    assert project_status(
        binding, NativeSnapshot(values={"lockstep_outcome": "FAIL"}), (), ()
    ).status == "escalated"
    assert project_status(
        binding, NativeSnapshot(values={"lockstep_outcome": "ERROR"}), (), ()
    ).status == "escalated"
    assert project_status(
        binding, NativeSnapshot(values={"lockstep_outcome": "ABORTED"}), (), ()
    ).status == "aborted"


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
