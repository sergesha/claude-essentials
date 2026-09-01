"""Task 11 RED integration oracles for compiler-produced native parallel graphs."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest

from lockstep.recipe import yamlgraph_adapter as yg
from lockstep.runtime.effects.descriptors import (
    build_scope_result,
    derive_effect_id,
    parse_effect_descriptor,
)
from lockstep.runtime.effects.models import (
    AcceptDescriptor,
    ScopeDescriptor,
    ScopeResult,
)
from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
from lockstep.runtime.effects.owner_provisioning import provision_runtime_snapshot
from lockstep.runtime.engine import Engine
from lockstep.runtime.graph_runtime import GraphRuntime
from lockstep.runtime.providers.codex import CodexRunnerAdapter
from lockstep.runtime.service import preflight_recipe
from lockstep.templates import install_template
from lockstep.workflow.compiler import compile_workflow
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import InMemoryWorkflowCatalog, validate_semantics
from tests.runtime._runtime_commitment_harness import _runtime_config

CONTROLLED_EFFECT = (
    Path(__file__).parents[1] / "fixtures" / "controlled_effect_executable.py"
)


@dataclass(frozen=True)
class _PublicParallelTrace:
    resume_batch_sizes: tuple[int, ...]
    resume_batch_schemas: tuple[tuple[str, ...], ...]
    workspace_refs: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    bearer_tokens: tuple[str, ...]
    consent_refs: tuple[str, ...]
    receipt_digests: tuple[str, ...]
    spawn_effect_ids: tuple[str, ...]
    joined_value: object
    terminal: dict[str, object]


def _wait_for(command, predicate, *, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if command._pump_failure is not None:
            raise command._pump_failure
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail("public parallel-review lifecycle timed out")


def _pending_acceptances(command, run_id: str) -> tuple[AcceptDescriptor, ...]:
    with command._admission_recovery_lock:
        command.runtime.bind(command.catalog.get(run_id))
        snapshot = command.runtime.snapshot(run_id, subgraphs=True)
    descriptors = []
    for interrupt in snapshot.pending:
        descriptor = command._protected_interrupt_descriptor(interrupt)
        if isinstance(descriptor, AcceptDescriptor):
            descriptors.append(descriptor)
    return tuple(descriptors)


def _terminal_status(command, project: Path, run_id: str):
    observed = Engine.observe(command.state_dir, command.recipes_dir).status(
        run_id, str(project)
    )
    return observed if observed["status"] == "completed" else None


def _run_public_parallel_review_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _PublicParallelTrace:
    """Drive the packaged parallel review through real owner-bound adapters."""

    project = tmp_path / "project"
    project.mkdir()
    (project / "tracked.txt").write_bytes(b"parallel review input\n")
    template_state = tmp_path / "template-owner-state"
    install_template("parallel-review", "release", project, state_dir=template_state)
    recipes = project / ".lockstep" / "recipes"
    authorized = preflight_recipe(recipes, "release")
    requirements = RuntimeRequirementIndex.for_authorized_closure(
        authorized,
        project_identity=str(project.resolve()),
    )
    assert len(requirements.requirements) == 2
    assert {item.runner_selector for item in requirements.requirements} == {"codex"}

    config = _runtime_config(tmp_path)
    for selector in ("codex", "pinned"):
        binding = config[selector]
        assert isinstance(binding, dict)
        binding["executable"] = str(CONTROLLED_EFFECT)
    codex_environment = config["codex"]["environment"]
    assert isinstance(codex_environment, dict)
    barrier = (
        Path(codex_environment["TMPDIR"])
        / "lockstep-controlled-two-process-barrier"
    )
    barrier.mkdir()
    codex = config["codex"]
    pinned = config["pinned"]
    assert isinstance(codex, dict)
    assert isinstance(pinned, dict)
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))
    provision_runtime_snapshot(
        state_dir=owner_state,
        codex=codex,
        pinned=pinned,
        replacement_keys=tuple(
            item.grant_selection_key for item in requirements.requirements
        ),
        index=requirements,
        project=project,
    )

    resume_batches: list[tuple[str, ...]] = []
    resume_schemas: list[tuple[str, ...]] = []
    spawn_calls: list[tuple[CodexRunnerAdapter, str]] = []
    original_resume = GraphRuntime.resume
    original_ensure_started = CodexRunnerAdapter.ensure_started

    def observe_resume(runtime, run_id, source, results_by_interrupt_id):
        resume_batches.append(tuple(sorted(results_by_interrupt_id)))
        resume_schemas.append(
            tuple(
                sorted(str(result.get("schema")) for result in results_by_interrupt_id.values())
            )
        )
        return original_resume(runtime, run_id, source, results_by_interrupt_id)

    def observe_ensure_started(adapter, launch):
        spawn_calls.append((adapter, launch.effect_id))
        return original_ensure_started(adapter, launch)

    monkeypatch.setattr(GraphRuntime, "resume", observe_resume)
    monkeypatch.setattr(CodexRunnerAdapter, "ensure_started", observe_ensure_started)
    command = Engine.command(owner_state, recipes)
    restarted = None
    try:
        started = command.start("release", {}, str(project))
        run_id = started["run_id"]

        managed = _wait_for(
            command,
            lambda: (
                records
                if len(records := tuple(
                    record
                    for record in command.effects.list_for_thread(
                        command.catalog.get(run_id).thread_id
                    )
                    if record.effect_kind == "managed"
                )) == 2
                and all(record.phase == "delivered" for record in records)
                else None
            ),
        )
        assert all(record.result is not None for record in managed)
        managed_by_effect = {record.effect_id: record for record in managed}
        assert Counter(effect_id for _adapter, effect_id in spawn_calls) == Counter(
            {effect_id: 1 for effect_id in managed_by_effect}
        )
        runner = command._runtime_execution_composition.runners.codex
        assert type(runner) is CodexRunnerAdapter
        assert runner.spawn_count == 2
        assert {id(adapter) for adapter, _effect_id in spawn_calls} == {id(runner)}
        workspace_refs = tuple(record.workspace_ref for record in managed)
        assert None not in workspace_refs
        assert len(set(workspace_refs)) == 2
        artifact_refs = tuple(
            record.result.artifact_refs[0]  # type: ignore[union-attr]
            for record in managed
        )
        assert len(set(artifact_refs)) == 2
        artifacts = tuple(command.artifacts.read(ref) for ref in artifact_refs)
        assert {item.source_path for item in artifacts} == {
            "security-review.md",
            "architecture-review.md",
        }
        artifact_bytes = {
            str(artifact.ref): command.blobs.read(artifact.blob)
            for artifact in artifacts
        }
        for artifact in artifacts:
            producer = managed_by_effect[artifact.producer_effect_id]
            assert str(artifact.ref) in producer.result.artifact_refs
            assert artifact.public_run_id == run_id
            assert artifact.project_identity == str(project.resolve())
            assert artifact.definition_digest == command.catalog.get(
                run_id
            ).recipe_digest
            assert artifact.producer_request_digest == producer.request_digest
            assert artifact.workspace_ref == producer.workspace_ref
            assert artifact.producer_coordinate == producer.coordinate
            assert artifact.descriptor_digest == producer.descriptor_digest
            assert artifact.blob.digest == hashlib.sha256(
                artifact_bytes[str(artifact.ref)]
            ).hexdigest()
            assert artifact.blob.size == len(artifact_bytes[str(artifact.ref)])
        intervals = []
        for artifact in artifacts:
            lines = command.blobs.read(artifact.blob).decode().splitlines()
            intervals.append(
                (
                    int(next(line for line in lines if line.startswith("started_ns: ")).split()[1]),
                    int(next(line for line in lines if line.startswith("ended_ns: ")).split()[1]),
                )
            )
        assert max(start for start, _end in intervals) < min(
            end for _start, end in intervals
        )
        assert any(len(batch) == 2 for batch in resume_batches)
        assert resume_schemas.count(
            ("lockstep.effect-result/v1", "lockstep.effect-result/v1")
        ) == 1

        first_acceptances = _wait_for(
            command, lambda: _pending_acceptances(command, run_id)
        )
        assert len(first_acceptances) == 1
        first_descriptor = first_acceptances[0]
        first_preview = command.preview_publication_consent(
            run_id, first_descriptor.logical_id, project=str(project)
        )
        first = command.issue_publication_consent(
            run_id,
            first_descriptor.logical_id,
            first_preview["digest"],
            project=str(project),
        )
        after_first = command.scenario_accept_artifact(first.token, project=str(project))
        assert after_first["status"] in {"running", "awaiting"}
        assert len(_wait_for(command, lambda: _pending_acceptances(command, run_id))) == 1
        command.close()

        restarted = Engine.command(owner_state, recipes)
        restarted.scenario_recover(str(project), limit=128)
        second_acceptances = _wait_for(
            restarted, lambda: _pending_acceptances(restarted, run_id)
        )
        assert len(second_acceptances) == 1
        second_descriptor = second_acceptances[0]
        second_preview = restarted.preview_publication_consent(
            run_id, second_descriptor.logical_id, project=str(project)
        )
        second = restarted.issue_publication_consent(
            run_id,
            second_descriptor.logical_id,
            second_preview["digest"],
            project=str(project),
        )
        restarted.scenario_accept_artifact(
            second.token, project=str(project)
        )
        terminal = _wait_for(
            restarted, lambda: _terminal_status(restarted, project, run_id)
        )
        assert terminal == {
            "status": "completed",
            "run_id": run_id,
            "owner": "engine",
            "next_action": None,
        }
        assert len(spawn_calls) == 2
        assert restarted._runtime_execution_composition.runners.codex.spawn_count == 0

        records = restarted.effects.list_for_thread(
            restarted.catalog.get(run_id).thread_id
        )
        acceptance_records = tuple(
            record
            for record in records
            if record.effect_kind == "accept" and record.result is not None
        )
        accepted = tuple(record.result for record in acceptance_records)
        assert len(accepted) == 2
        consent_refs = tuple(result.consent_ref for result in accepted)
        receipt_digests = tuple(result.receipt_digest for result in accepted)
        assert first.token != second.token
        assert len(set(consent_refs)) == len(set(receipt_digests)) == 2
        assert resume_schemas.count(("lockstep.acceptance-result/v1",)) == 2
        expected_destinations = {
            "security-review.md": ".lockstep/security-review.md",
            "architecture-review.md": ".lockstep/architecture-review.md",
        }
        artifacts_by_ref = {str(artifact.ref): artifact for artifact in artifacts}
        descriptors_by_digest = {
            descriptor.digest: descriptor
            for descriptor in (first_descriptor, second_descriptor)
        }
        assert set(descriptors_by_digest) == {
            record.descriptor_digest for record in acceptance_records
        }
        assert {result.artifact_ref for result in accepted} == set(artifacts_by_ref)
        for record in acceptance_records:
            result = record.result
            assert result is not None
            artifact = artifacts_by_ref[result.artifact_ref]
            descriptor = descriptors_by_digest[record.descriptor_digest]
            destination = expected_destinations[artifact.source_path]
            assert result.artifact_digest == artifact.blob.digest
            assert result.destination == descriptor.destination == destination
            assert descriptor.artifact_handle.endswith(f".{artifact.declared_name}")
            assert result.transformation == "identity"
            assert result.audience == "local-project"
            assert (project / destination).read_bytes() == artifact_bytes[
                result.artifact_ref
            ]

        history = tuple(restarted.runtime.history(run_id))
        join_schedules = tuple(
            (index, snapshot)
            for index, snapshot in enumerate(history)
            if len(snapshot.next) == 1
            and snapshot.next[0].startswith("parallel-0-join-")
        )
        assert len(join_schedules) == 1
        join_index, join_schedule = join_schedules[0]
        assert "reviews_result" not in join_schedule.values
        assert join_index > 0
        newer_joined = next(
            snapshot
            for snapshot in history[:join_index]
            if "reviews_result" in snapshot.values
        )
        assert newer_joined.values["reviews_result"] == {
            "outcome": "PASS",
            "value": "pass",
        }
        joined = ["reviews_result" in snapshot.values for snapshot in history]
        assert any(joined)
        assert sum(left != right for left, right in pairwise(joined)) == 1
        joined_values = {
            json.dumps(snapshot.values["reviews_result"], sort_keys=True)
            for snapshot in history
            if "reviews_result" in snapshot.values
        }
        assert len(joined_values) == 1
        joined_value = json.loads(next(iter(joined_values)))
        assert joined_value == {"outcome": "PASS", "value": "pass"}
        assert (project / ".lockstep/security-review.md").is_file()
        assert (project / ".lockstep/architecture-review.md").is_file()
        return _PublicParallelTrace(
            resume_batch_sizes=tuple(len(batch) for batch in resume_batches),
            resume_batch_schemas=tuple(resume_schemas),
            workspace_refs=workspace_refs,  # type: ignore[arg-type]
            artifact_refs=artifact_refs,
            artifact_paths=tuple(item.source_path for item in artifacts),
            bearer_tokens=(first.token, second.token),
            consent_refs=consent_refs,
            receipt_digests=receipt_digests,
            spawn_effect_ids=tuple(effect_id for _adapter, effect_id in spawn_calls),
            joined_value=joined_value,
            terminal=terminal,
        )
    finally:
        command.close()
        if restarted is not None:
            restarted.close()


def test_packaged_parallel_review_overlaps_real_adapters_and_joins_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = _run_public_parallel_review_lifecycle(tmp_path, monkeypatch)

    assert 2 in trace.resume_batch_sizes
    assert len(set(trace.workspace_refs)) == 2
    assert len(set(trace.artifact_refs)) == 2
    assert len(trace.spawn_effect_ids) == len(set(trace.spawn_effect_ids)) == 2
    assert set(trace.artifact_paths) == {
        "security-review.md",
        "architecture-review.md",
    }
    assert trace.joined_value == {"outcome": "PASS", "value": "pass"}
    assert trace.terminal["status"] == "completed"


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


def _result(
    interrupt, outcome: str = "PASS", *, artifact_ref: str | None = None
) -> dict:
    descriptor = parse_effect_descriptor(interrupt.value["lockstep_effect"])
    result_outcome = "ERROR" if outcome == "ABORTED" else outcome
    return {
        "schema": "lockstep.effect-result/v1",
        "effect_id": derive_effect_id(interrupt.coordinate, descriptor.digest),
        "outcome": result_outcome,
        "result_ref": "blob:" + "a" * 64,
        "artifact_refs": [] if artifact_ref is None else [artifact_ref],
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


def test_branch_artifact_refs_remain_bound_to_each_native_result(tmp_path: Path) -> None:
    """The join may observe results but must not rewrite Task 10 provenance refs."""
    recipe = _compile(tmp_path)
    app = yg._open_native_path(recipe)  # noqa: SLF001 - integration oracle
    parked = app.invoke({}, thread_id="artifact-results")
    by_logical_id = {
        parse_effect_descriptor(item.value["lockstep_effect"]).logical_id: item
        for item in parked.pending
    }
    refs = {
        "security": "artifact:" + "1" * 64,
        "architecture": "artifact:" + "2" * 64,
    }
    completed = app.resume(
        thread_id="artifact-results",
        results_by_interrupt_id={
            item.coordinate.interrupt_id: _result(
                item, artifact_ref=refs[logical_id]
            )
            for logical_id, item in by_logical_id.items()
        },
    )
    app.close()

    assert completed.values["security_result"]["artifact_refs"] == [refs["security"]]
    assert completed.values["architecture_result"]["artifact_refs"] == [
        refs["architecture"]
    ]
    assert completed.values["gates_result"]["outcome"] == "PASS"


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


def test_branch_abort_has_precedence_only_after_native_join(tmp_path: Path) -> None:
    """Cancellation is recorded per branch and dominates only at the barrier."""
    recipe = _compile(tmp_path)
    app = yg._open_native_path(recipe)  # noqa: SLF001 - integration oracle
    parked = app.invoke({}, thread_id="abort-precedence")
    first, second = parked.pending
    waiting = app.resume(
        thread_id="abort-precedence",
        results_by_interrupt_id={first.coordinate.interrupt_id: _result(first, "ERROR")},
    )
    assert [item.coordinate for item in waiting.pending] == [second.coordinate]
    assert waiting.values.get("gates_result") is None

    completed = app.resume(
        thread_id="abort-precedence",
        results_by_interrupt_id={
            second.coordinate.interrupt_id: _result(second, "ABORTED")
        },
    )
    app.close()

    assert completed.values["gates_result"] == {
        "outcome": "ERROR",
        "value": "error",
        "fixed_error_code": "cancelled",
    }
    assert completed.values["lockstep_outcome"] == "ABORTED"


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


def test_bounded_parallel_scope_error_bypasses_fanout(tmp_path: Path) -> None:
    """A cooperative timeout fact may terminate only before branches are spawned."""
    recipe = _compile(tmp_path, bounded=True)
    app = yg._open_native_path(recipe)  # noqa: SLF001 - integration oracle
    scoped = app.invoke({}, thread_id="bounded-timeout")
    scope_interrupt = scoped.pending[0]
    scope = parse_effect_descriptor(scope_interrupt.value["lockstep_effect"])
    assert isinstance(scope, ScopeDescriptor)
    result = ScopeResult(
        "lockstep.scope-result/v1",
        derive_effect_id(scope_interrupt.coordinate, scope.digest),
        "ERROR",
        "parallel",
        scope.digest,
        fixed_error_code="scope_timeout",
    )
    completed = app.resume(
        thread_id="bounded-timeout",
        results_by_interrupt_id={
            scope_interrupt.coordinate.interrupt_id: result.to_dict()
        },
    )
    app.close()

    assert completed.pending == ()
    assert completed.values["lockstep_outcome"] == "ERROR"
    assert "security_result" not in completed.values
    assert "architecture_result" not in completed.values
