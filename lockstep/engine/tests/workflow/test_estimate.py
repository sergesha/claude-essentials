from __future__ import annotations

from pathlib import Path

import pytest

from lockstep.workflow.estimate import estimate_manual_recipe, estimate_workflow
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import (
    ChildWorkflowContract,
    InMemoryWorkflowCatalog,
)

EXPECTED_FIELDS = {
    "schema",
    "user_work_steps",
    "maximum_validator_submissions",
    "pinned_commands",
    "child_calls",
    "maximum_child_calls",
    "peak_parallel_branches",
    "peak_parallel_child_calls",
    "maximum_runner_timeout_seconds",
    "generated_node_count",
    "expanded_fragment_count",
    "controlled_time",
    "end_to_end_wall_time",
    "tokens",
    "money",
}


def _workflow(tmp_path: Path):
    source = tmp_path / "estimate.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\n"
        "name: estimate\n"
        "description: estimate every structural metric\n"
        "protect: ['**']\n"
        "flow:\n"
        "  - step: edit\n"
        "    task: Edit\n"
        "    exit: Done\n"
        "    retry: {limit: 2, exhausted: escalate}\n"
        "  - repeat:\n"
        "      id: cycle\n"
        "      limit: 3\n"
        "      until: tests.passed\n"
        "      exhausted: escalate\n"
        "      do:\n"
        "        - verify:\n"
        "            id: tests\n"
        "            command: pytest -q\n"
        "            timeout: 60\n"
        "  - call:\n"
        "      id: review\n"
        "      workflow: child\n"
        "      runner: codex\n"
    )
    return parse_workflow(load_workflow(source))


def test_estimate_has_the_exact_closed_normative_schema_and_honest_unknowns(
    tmp_path: Path,
) -> None:
    estimate = estimate_workflow(
        _workflow(tmp_path),
        InMemoryWorkflowCatalog(
            {"child": ChildWorkflowContract(("pass", "fail", "error"))}
        ),
    )
    data = estimate.to_dict()

    assert set(data) == EXPECTED_FIELDS
    assert not hasattr(estimate, "peak_parallel_subcalls")
    assert data["schema"] == "lockstep.structural-estimate/v1"
    assert data["user_work_steps"] == 1
    assert data["maximum_validator_submissions"] == 5
    assert data["pinned_commands"] == 1
    assert data["child_calls"] == 1
    assert data["maximum_child_calls"] == 1
    assert data["peak_parallel_branches"] == 0
    assert data["peak_parallel_child_calls"] == 0
    assert "peak_parallel_subcalls" not in data
    assert data["maximum_runner_timeout_seconds"] == 60
    assert data["generated_node_count"] > 0
    assert data["expanded_fragment_count"] == 0
    assert set(data["controlled_time"]) == {
        "available",
        "upper_bound_seconds",
        "formula",
        "assumptions",
        "unavailable_reasons",
    }
    assert data["controlled_time"]["available"] is False
    assert data["controlled_time"]["upper_bound_seconds"] is None
    assert "child call 'review' has no timeout" in data["controlled_time"][
        "unavailable_reasons"
    ]
    assert data["end_to_end_wall_time"] == {
        "available": False,
        "reason": "human and external-agent completion time is unbounded",
    }
    assert data["tokens"] == {
        "available": False,
        "reason": "owner-controlled runner metadata is unavailable",
        "assumptions": [],
    }
    assert data["money"] == data["tokens"]


def test_manual_estimate_uses_only_closed_recipe_facts(tmp_path: Path) -> None:
    recipe = tmp_path / "manual.recipe.yaml"
    recipe.write_text(
        "name: manual\n"
        "state: {result: dict}\n"
        "nodes:\n"
        "  edit:\n"
        "    type: interrupt\n"
        "    state_key: request\n"
        "    resume_key: result\n"
        "    idempotent: false\n"
        "    message:\n"
        "      lockstep_effect:\n"
        "        schema: lockstep.effect/v1\n"
        "        kind: manual\n"
        "        logical_id: edit\n"
        "        runner: null\n"
        "        inputs: {}\n"
        "        writes: [src/]\n"
        "        artifacts: []\n"
        "        deadline_seconds: null\n"
        "        scope_state_keys: []\n"
        "        result_schema: lockstep.effect-result/v1\n"
        "edges: [{from: START, to: edit}, {from: edit, to: END}]\n"
    )

    data = estimate_manual_recipe(recipe).to_dict()

    assert set(data) == EXPECTED_FIELDS
    assert data["user_work_steps"] == 1
    assert data["maximum_validator_submissions"] == 1
    assert data["generated_node_count"] == 1
    assert data["tokens"]["available"] is False
    assert data["money"]["available"] is False


def test_controlled_time_is_a_real_bound_when_every_engine_timeout_is_known(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bounded.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: bounded\ndescription: bounded\nprotect: ['**']\n"
        "flow:\n  - verify:\n      id: tests\n      command: pytest -q\n"
        "      timeout: 30\n      retry: {limit: 2, exhausted: escalate}\n"
    )
    estimate = estimate_workflow(
        parse_workflow(load_workflow(source)), InMemoryWorkflowCatalog({})
    ).to_dict()

    assert estimate["maximum_validator_submissions"] == 2
    assert estimate["controlled_time"] == {
        "available": True,
        "upper_bound_seconds": 60,
        "formula": "verify tests: 30s × 2",
        "assumptions": ["configured runner timeouts are enforced"],
        "unavailable_reasons": [],
    }


def test_parallel_estimate_uses_peak_wall_time_and_counts_all_child_calls(
    tmp_path: Path,
) -> None:
    source = tmp_path / "parallel-cost.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: parallel-cost\ndescription: parallel\nprotect: ['**']\n"
        "flow:\n  - parallel:\n      id: reviews\n      join: all\n"
        "      timeout_minutes: 2\n      branches:\n"
        "        security:\n          - call: {id: sec, workflow: child, runner: codex, timeout_minutes: 1}\n"
        "        architecture:\n          - call: {id: arch, workflow: child, runner: codex, timeout_minutes: 1}\n"
    )
    estimate = estimate_workflow(
        parse_workflow(load_workflow(source)),
        InMemoryWorkflowCatalog({"child": ChildWorkflowContract(("pass", "fail", "error"))}),
    ).to_dict()

    assert estimate["child_calls"] == 2
    assert estimate["maximum_child_calls"] == 2
    assert estimate["peak_parallel_branches"] == 2
    assert estimate["peak_parallel_child_calls"] == 2
    assert "peak_parallel_subcalls" not in estimate
    assert estimate["controlled_time"]["available"] is True
    assert estimate["controlled_time"]["upper_bound_seconds"] == 60
    assert "parallel reviews: max(60s, 60s), scope 120s" in estimate["controlled_time"]["formula"]


def test_repeat_parallel_scope_bound_applies_once_per_iteration(tmp_path: Path) -> None:
    source = tmp_path / "repeat-parallel.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: repeat-parallel\ndescription: cost\nprotect: ['**']\n"
        "flow:\n  - repeat:\n      id: rounds\n      limit: 3\n"
        "      until: tests.passed\n      exhausted: escalate\n      do:\n"
        "        - parallel:\n            id: reviews\n            join: all\n"
        "            timeout_minutes: 2\n            branches:\n"
        "              a:\n                - call: {id: a, workflow: child, runner: codex, timeout_minutes: 1}\n"
        "              b:\n                - verify: {id: tests, command: pytest, timeout: 60}\n"
    )
    estimate = estimate_workflow(
        parse_workflow(load_workflow(source)),
        InMemoryWorkflowCatalog(
            {"child": ChildWorkflowContract(("pass", "fail", "error"))}
        ),
    ).to_dict()

    assert estimate["controlled_time"]["available"] is True
    assert estimate["controlled_time"]["upper_bound_seconds"] == 180
    assert "scope 120s × 3" in estimate["controlled_time"]["formula"]


def test_manual_estimate_multiplies_bounded_effects(
    tmp_path: Path,
) -> None:
    recipe = tmp_path / "bounded-manual.recipe.yaml"
    recipe.write_text(
        "name: bounded-manual\n"
        "state: {command: dict, snapshot: str, result: dict}\n"
        "nodes:\n"
        "  gate: {type: passthrough}\n"
        "  verify:\n"
        "    type: interrupt\n    state_key: request\n    resume_key: result\n"
        "    idempotent: false\n    message:\n      lockstep_effect:\n"
        "        schema: lockstep.effect/v1\n        kind: pinned\n"
        "        logical_id: tests\n"
        "        runner: {selector: pinned, required_capabilities: [workspace, bounded_result, sandbox]}\n"
        "        inputs: {command: {state_key: command}, snapshot: {state_key: snapshot}}\n"
        "        writes: []\n        artifacts: []\n        deadline_seconds: 30\n"
        "        scope_state_keys: []\n        result_schema: lockstep.effect-result/v1\n"
        "  done: {type: passthrough}\n"
        "edges:\n  - {from: START, to: gate}\n  - {from: gate, to: verify}\n"
        "  - {from: verify, to: gate}\n  - {from: done, to: END}\n"
        "loop_limits: {gate: 3}\nloop_exits: {gate: done}\n"
    )

    data = estimate_manual_recipe(recipe).to_dict()

    assert data["maximum_validator_submissions"] == 3
    assert data["controlled_time"]["available"] is True
    assert data["controlled_time"]["upper_bound_seconds"] == 90
    assert "30s × 3" in data["controlled_time"]["formula"]
    assert data["pinned_commands"] == 1


def test_manual_estimate_counts_subgraphs_and_marks_time_unavailable(
    tmp_path: Path,
) -> None:
    child = tmp_path / "child.recipe.yaml"
    child.write_text("name: child\nnodes: {}\nedges: []\n")
    recipe = tmp_path / "parent.recipe.yaml"
    recipe.write_text(
        "name: parent\nnodes:\n"
        "  child: {type: subgraph, graph: child.recipe.yaml, mode: direct}\n"
        "edges: [{from: START, to: child}, {from: child, to: END}]\n"
    )

    data = estimate_manual_recipe(recipe).to_dict()

    assert data["child_calls"] == data["maximum_child_calls"] == 1
    assert data["expanded_fragment_count"] == 1
    assert data["controlled_time"]["available"] is False
    assert data["controlled_time"]["unavailable_reasons"] == [
        "subgraph call 'child' has no timeout"
    ]


def test_manual_estimate_rejects_open_pseudo_effect_descriptor(tmp_path: Path) -> None:
    recipe = tmp_path / "open.recipe.yaml"
    recipe.write_text(
        "name: open\nnodes:\n  fake:\n    type: interrupt\n"
        "    state_key: request\n    resume_key: result\n    idempotent: false\n"
        "    message:\n      lockstep_effect: {schema: lockstep.effect/v1, kind: pinned, deadline_seconds: 1}\n"
        "edges: [{from: START, to: fake}, {from: fake, to: END}]\n"
    )

    with pytest.raises(ValueError, match="missing keys"):
        estimate_manual_recipe(recipe)


def test_manual_estimate_rejects_descriptor_with_unknown_state_key(tmp_path: Path) -> None:
    recipe = tmp_path / "unknown-state.recipe.yaml"
    recipe.write_text(
        "name: unknown-state\nstate: {result: dict}\nnodes:\n  verify:\n"
        "    type: interrupt\n    state_key: request\n    resume_key: result\n"
        "    idempotent: false\n    message:\n      lockstep_effect:\n"
        "        schema: lockstep.effect/v1\n        kind: pinned\n"
        "        logical_id: tests\n"
        "        runner: {selector: pinned, required_capabilities: [workspace, bounded_result, sandbox]}\n"
        "        inputs: {command: {state_key: missing}}\n"
        "        writes: []\n        artifacts: []\n        deadline_seconds: 1\n"
        "        scope_state_keys: []\n        result_schema: lockstep.effect-result/v1\n"
        "edges: [{from: START, to: verify}, {from: verify, to: END}]\n"
    )

    with pytest.raises(ValueError, match="unknown state"):
        estimate_manual_recipe(recipe)


def test_manual_estimate_rejects_compiler_only_scope_descriptor(tmp_path: Path) -> None:
    recipe = tmp_path / "scope.recipe.yaml"
    recipe.write_text(
        "name: scope\nstate: {scope_result: dict}\nnodes:\n  scope:\n"
        "    type: interrupt\n    state_key: request\n    resume_key: scope_result\n"
        "    idempotent: false\n    message:\n      lockstep_effect:\n"
        "        schema: lockstep.effect/v1\n        kind: scope\n"
        "        logical_id: child\n        scope_kind: call\n"
        "        duration_seconds: 60\n        runner_selector: codex\n"
        "        ancestor_deadline_state_keys: []\n"
        "        result_state_key: scope_result\n"
        "        result_schema: lockstep.scope-result/v1\n"
        "edges: [{from: START, to: scope}, {from: scope, to: END}]\n"
    )

    with pytest.raises(ValueError, match="compiler-only scope"):
        estimate_manual_recipe(recipe)
