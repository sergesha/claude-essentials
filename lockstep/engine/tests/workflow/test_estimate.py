from __future__ import annotations

from pathlib import Path

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
    "peak_parallel_subcalls",
    "maximum_configured_runner_timeout_seconds",
    "generated_node_count",
    "expanded_fragment_count",
    "controlled_time",
    "end_to_end_time",
    "token_estimate",
    "money_estimate",
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
    assert data["schema"] == "lockstep.structural-estimate/v1"
    assert data["user_work_steps"] == 1
    assert data["maximum_validator_submissions"] == 5
    assert data["pinned_commands"] == 1
    assert data["child_calls"] == 1
    assert data["maximum_child_calls"] == 1
    assert data["peak_parallel_branches"] == 0
    assert data["peak_parallel_subcalls"] == 0
    assert data["maximum_configured_runner_timeout_seconds"] == 60
    assert data["generated_node_count"] > 0
    assert data["expanded_fragment_count"] == 0
    assert set(data["controlled_time"]) == {
        "status",
        "value",
        "unit",
        "formula",
        "reasons",
        "assumptions",
    }
    assert data["controlled_time"]["status"] == "unavailable"
    assert data["controlled_time"]["value"] is None
    assert "child call 'review' has no timeout" in data["controlled_time"][
        "reasons"
    ]
    assert data["end_to_end_time"]["status"] == "unbounded"
    assert data["end_to_end_time"]["value"] is None
    assert data["token_estimate"]["status"] == "unavailable"
    assert data["money_estimate"]["status"] == "unavailable"
    assert data["token_estimate"]["value"] is None
    assert data["money_estimate"]["value"] is None


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
    assert data["token_estimate"]["status"] == "unavailable"
    assert data["money_estimate"]["status"] == "unavailable"
