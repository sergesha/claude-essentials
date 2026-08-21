"""Task 11 RED integration oracles for compiler-produced native parallel graphs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from lockstep.recipe import yamlgraph_adapter as yg
from lockstep.runtime.effects.descriptors import (
    build_scope_result,
    derive_effect_id,
    parse_effect_descriptor,
)
from lockstep.runtime.effects.models import ScopeDescriptor
from lockstep.workflow.compiler import compile_workflow
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import InMemoryWorkflowCatalog, validate_semantics


def _compile(tmp_path: Path, *, bounded: bool = False) -> Path:
    source = tmp_path / "parallel.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\n"
        "name: parallel\n"
        "description: native parallel integration\n"
        "protect: ['**']\n"
        "flow:\n"
        "  - parallel:\n"
        "      id: gates\n"
        "      join: all\n"
        + ("      timeout_minutes: 5\n" if bounded else "")
        + "      branches:\n"
        "        security:\n"
        "          - verify: {id: security, command: python -m security}\n"
        "        architecture:\n"
        "          - verify: {id: architecture, command: python -m architecture}\n"
    )
    catalog = InMemoryWorkflowCatalog({})
    workflow = parse_workflow(load_workflow(source))
    result = compile_workflow(validate_semantics(workflow, catalog), catalog)
    recipe = tmp_path / "parallel.recipe.yaml"
    recipe.write_bytes(result.recipe_bytes)
    return recipe


def _result(interrupt, outcome: str = "PASS") -> dict:
    descriptor = parse_effect_descriptor(interrupt.value["lockstep_effect"])
    return {
        "schema": "lockstep.effect-result/v1",
        "effect_id": derive_effect_id(interrupt.coordinate, descriptor.digest),
        "outcome": outcome,
        "result_ref": "blob:" + "a" * 64,
        "artifact_refs": [],
        "snapshot_ref": None,
        "diff_ref": None,
        "fixed_error_code": "cancelled" if outcome == "ABORTED" else None,
        "evidence_refs": [],
    }


def test_compiled_parallel_partial_resume_restart_and_native_join(
    tmp_path: Path,
) -> None:
    """A restart must recover LangGraph tasks, not a Lockstep branch table."""
    recipe = _compile(tmp_path)
    database = tmp_path / "native.sqlite"
    first = yg._open_native_path(recipe, database)  # noqa: SLF001 - integration oracle
    parked = first.invoke({}, thread_id="partial")
    assert len(parked.pending) == 2
    first_branch = parked.pending[0]
    waiting = first.resume(
        thread_id="partial",
        results_by_interrupt_id={first_branch.coordinate.interrupt_id: _result(first_branch)},
    )
    first.close()

    assert len(waiting.pending) == 1
    assert waiting.values.get("gates_result") is None

    restarted = yg._open_native_path(recipe, database)  # noqa: SLF001
    current = restarted.snapshot(thread_id="partial", subgraphs=True)
    assert [item.coordinate for item in current.pending] == [
        waiting.pending[0].coordinate
    ]
    completed = restarted.resume(
        thread_id="partial",
        results_by_interrupt_id={
            current.pending[0].coordinate.interrupt_id: _result(current.pending[0])
        },
    )
    restarted.close()

    assert completed.pending == ()
    assert completed.values["gates_result"] == {
        "outcome": "PASS",
        "value": "pass",
    }
    assert completed.values["lockstep_outcome"] == "PASS"


def test_compiled_parallel_one_batch_resume_reaches_native_join(tmp_path: Path) -> None:
    """One verified interrupt map must be one native Command(resume=...)."""
    recipe = _compile(tmp_path)
    app = yg._open_native_path(recipe)  # noqa: SLF001 - integration oracle
    parked = app.invoke({}, thread_id="batch")
    completed = app.resume(
        thread_id="batch",
        results_by_interrupt_id={
            item.coordinate.interrupt_id: _result(item) for item in parked.pending
        },
    )
    app.close()

    assert len(parked.pending) == 2
    assert completed.pending == ()
    assert completed.values["gates_result"]["outcome"] == "PASS"
    assert completed.values["lockstep_outcome"] == "PASS"


def test_branch_failure_waits_for_native_join_and_uses_closed_precedence(
    tmp_path: Path,
) -> None:
    """FAIL/ERROR/ABORTED are branch facts; none may cancel a sibling early."""
    recipe = _compile(tmp_path)
    app = yg._open_native_path(recipe)  # noqa: SLF001 - integration oracle
    parked = app.invoke({}, thread_id="failure-join")
    first, second = parked.pending
    waiting = app.resume(
        thread_id="failure-join",
        results_by_interrupt_id={first.coordinate.interrupt_id: _result(first, "FAIL")},
    )

    assert [item.coordinate for item in waiting.pending] == [second.coordinate]
    assert waiting.values.get("lockstep_outcome") is None
    assert waiting.values.get("gates_result") is None

    completed = app.resume(
        thread_id="failure-join",
        results_by_interrupt_id={second.coordinate.interrupt_id: _result(second, "ERROR")},
    )
    app.close()

    assert completed.pending == ()
    assert completed.values["gates_result"] == {
        "outcome": "ERROR",
        "value": "error",
    }
    assert completed.values["lockstep_outcome"] == "ERROR"


def test_bounded_parallel_scope_is_shared_by_all_native_branch_interrupts(
    tmp_path: Path,
) -> None:
    """All siblings inherit one immutable deadline fact from graph state."""
    recipe = _compile(tmp_path, bounded=True)
    app = yg._open_native_path(recipe)  # noqa: SLF001 - integration oracle
    scoped = app.invoke({}, thread_id="bounded")
    assert len(scoped.pending) == 1
    scope_interrupt = scoped.pending[0]
    scope = parse_effect_descriptor(scope_interrupt.value["lockstep_effect"])
    assert isinstance(scope, ScopeDescriptor)
    assert scope.scope_kind == "parallel"
    effect_id = derive_effect_id(scope_interrupt.coordinate, scope.digest)
    scope_result = build_scope_result(
        effect_id=effect_id,
        scope_digest=scope.digest,
        scope_kind="parallel",
        now=datetime(2026, 8, 21, 12, tzinfo=UTC),
        duration_seconds=scope.duration_seconds,
        ancestors=(),
    )
    branches = app.resume(
        thread_id="bounded",
        results_by_interrupt_id={
            scope_interrupt.coordinate.interrupt_id: scope_result.to_dict()
        },
    )
    app.close()

    assert len(branches.pending) == 2
    assert all(
        item.state_values is not None
        and item.state_values[scope.result_state_key] == scope_result.to_dict()
        and parse_effect_descriptor(
            item.value["lockstep_effect"]
        ).scope_state_keys == (scope.result_state_key,)
        for item in branches.pending
    )
