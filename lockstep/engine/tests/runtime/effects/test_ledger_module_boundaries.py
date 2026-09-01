"""Ownership and behavior freeze for the effect-ledger projection seam."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

from sqlalchemy import select


def test_ledger_facade_inherits_private_projection_owner_in_a_fresh_process() -> None:
    """Catch copied types, circular imports, or reads left on the stateful facade."""

    script = r'''
from lockstep.runtime.effects._ledger_queries import _EffectLedgerQueries
from lockstep.runtime.effects._ledger_policy import (
    PRELAUNCH_ERROR_CODES,
    EffectConflict,
    IllegalEffectTransition,
    StaleEffectLease,
    StaleEffectRevision,
    _terminal_transition_replay,
    _transition_values,
    _validate_effect_preparation,
    _validate_prelaunch_seal,
    _validate_prepare_coordinate,
    _validate_prepare_descriptor,
    _validate_result_kind,
    _validate_scope_seal,
    _validate_transition_facts,
)
from lockstep.runtime.effects._ledger_records import (
    EffectRecord,
    RunDriveWatch,
    _PreparedEffectFacts,
    _binding_digest,
    _clock_now,
    _dump,
    _load,
    _nonempty,
    _utc,
)
from lockstep.runtime.effects import ledger

read_methods = {
    "_run_drive_watch",
    "_result_for",
    "_from_row",
    "max_run_drive_admission_seq",
    "list_run_drive_watches",
    "list_run_drive_watches_by_public_run_ids",
    "get",
    "list_for_thread",
    "list_nonterminal",
    "list_nonterminal_for_thread",
    "list_recovery_threads",
    "list_due",
    "next_deadline",
}
record_definitions = {
    "_PreparedEffectFacts": _PreparedEffectFacts,
    "_utc": _utc,
    "_dump": _dump,
    "_load": _load,
    "_nonempty": _nonempty,
    "_binding_digest": _binding_digest,
    "_clock_now": _clock_now,
}
policy_definitions = {
    "_validate_effect_preparation": _validate_effect_preparation,
    "_validate_prepare_coordinate": _validate_prepare_coordinate,
    "_validate_prepare_descriptor": _validate_prepare_descriptor,
    "_validate_result_kind": _validate_result_kind,
    "_validate_scope_seal": _validate_scope_seal,
    "_validate_prelaunch_seal": _validate_prelaunch_seal,
    "_terminal_transition_replay": _terminal_transition_replay,
    "_validate_transition_facts": _validate_transition_facts,
    "_transition_values": _transition_values,
}
stateful_methods = {
    "__init__",
    "admit_start",
    "acknowledge_run_drive_watch",
    "prepare",
    "_insert_or_verify_prepared",
    "_validate_transition_edge",
    "_persist_transition",
    "_transition",
    "_validate_live_lease",
    "mark_launching",
    "mark_running",
    "seal",
    "mark_indeterminate",
    "mark_delivered",
}

assert ledger.RunDriveWatch is RunDriveWatch
assert ledger.EffectRecord is EffectRecord
assert ledger.EffectConflict is EffectConflict
assert ledger.StaleEffectRevision is StaleEffectRevision
assert ledger.IllegalEffectTransition is IllegalEffectTransition
assert ledger.StaleEffectLease is StaleEffectLease
assert ledger.PRELAUNCH_ERROR_CODES is PRELAUNCH_ERROR_CODES
assert ledger.EffectLedger.__module__ == "lockstep.runtime.effects.ledger"
assert _EffectLedgerQueries in ledger.EffectLedger.__mro__
assert "__init__" not in _EffectLedgerQueries.__dict__
assert read_methods <= _EffectLedgerQueries.__dict__.keys()
assert read_methods.isdisjoint(ledger.EffectLedger.__dict__)
assert "_now" not in ledger.EffectLedger.__dict__
assert {
    name for name, value in ledger.EffectLedger.__dict__.items() if callable(value)
} == stateful_methods
for name in read_methods:
    assert getattr(_EffectLedgerQueries, name).__module__ == (
        "lockstep.runtime.effects._ledger_queries"
    )
for name, definition in record_definitions.items():
    assert getattr(ledger, name) is definition
    assert definition.__module__ == "lockstep.runtime.effects._ledger_records"
for name, definition in policy_definitions.items():
    assert getattr(ledger, name) is definition
    assert definition.__module__ == "lockstep.runtime.effects._ledger_policy"
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def _durable_rows(store) -> tuple[tuple[tuple[object, ...], ...], ...]:
    tables = (
        store.tables.runs,
        store.tables.run_drive_watches,
        store.tables.effects,
        store.tables.effect_observations,
    )
    with store.read_connection() as connection:
        return tuple(
            tuple(tuple(row) for row in connection.execute(select(table)).all())
            for table in tables
        )


def test_projection_reads_return_exact_facts_without_mutating_durable_state(
    tmp_path,
) -> None:
    from lockstep.runtime.blobs import BlobRef
    from lockstep.runtime.catalog import RunBinding, RunCatalog
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor
    from lockstep.runtime.effects.ledger import EffectLedger, EffectRecord, RunDriveWatch
    from lockstep.runtime.native_models import NativeCoordinate
    from lockstep.runtime.storage import SQLiteStore

    now = datetime(2026, 8, 20, 10, tzinfo=UTC)
    deadline = datetime(2026, 8, 20, 11, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "runtime.sqlite3")
    try:
        ledger = EffectLedger(store, clock=lambda: now)
        binding = RunBinding(
            "run-1",
            "thread-1",
            "a" * 64,
            "bundle:" + "b" * 64,
            "/project",
        )
        _binding, admitted = ledger.admit_start(
            RunCatalog(store), binding, BlobRef("c" * 64, 7)
        )
        descriptor = parse_effect_descriptor(
            {
                "schema": "lockstep.effect/v1",
                "kind": "managed",
                "logical_id": "implement",
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
        prepared = ledger.prepare(
            NativeCoordinate("thread-1", "checkpoint", "", "task", "interrupt"),
            descriptor,
            deadline_at=deadline,
            runner_binding_digest="d" * 64,
            workspace_ref="snapshot:" + "e" * 64,
            request_digest="f" * 64,
            grant_digest="0" * 64,
        )
        table_names = frozenset(store.metadata.tables)
        rows_before = _durable_rows(store)

        assert ledger.max_run_drive_admission_seq() == 1
        assert ledger.list_run_drive_watches(
            after_admission_seq=0, high_water=1, limit=128
        ) == (admitted,)
        assert ledger.list_run_drive_watches_by_public_run_ids(("run-1",)) == (
            admitted,
        )
        assert isinstance(admitted, RunDriveWatch)
        assert ledger.get(prepared.effect_id) == prepared
        assert ledger.list_for_thread("thread-1") == (prepared,)
        assert ledger.list_nonterminal() == [prepared]
        assert ledger.list_nonterminal_for_thread("thread-1", limit=1) == [prepared]
        assert ledger.list_recovery_threads(limit=1) == ("thread-1",)
        assert ledger.list_due(deadline, limit=1) == [prepared]
        assert ledger.next_deadline() == deadline
        assert isinstance(prepared, EffectRecord)

        assert frozenset(store.metadata.tables) == table_names
        assert _durable_rows(store) == rows_before
    finally:
        store.close()
