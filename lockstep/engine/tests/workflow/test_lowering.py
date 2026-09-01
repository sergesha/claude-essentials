from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.effects.models import AcceptDescriptor, DecisionDescriptor
from lockstep.recipe.authority import RecipeAuthorityPolicy, StrictRecipeIngress
from lockstep.recipe.yamlgraph_adapter import open_native_app
from lockstep.runtime.recipe_bundles import RecipeBundleStore
from lockstep.workflow.compiler import compile_workflow
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import InMemoryWorkflowCatalog, validate_semantics


def _compile(tmp_path: Path, flow: str, defaults: str = "") -> dict:
    source = tmp_path / "control.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\n"
        "name: control\n"
        "description: structured control\n"
        "protect: ['**']\n"
        f"{defaults}"
        f"flow:\n{flow}"
    )
    workflow = parse_workflow(load_workflow(source))
    catalog = InMemoryWorkflowCatalog({})
    return yaml.safe_load(
        compile_workflow(validate_semantics(workflow, catalog), catalog).recipe_bytes
    )


def _interrupts(document: dict) -> dict[str, dict]:
    return {
        name: node
        for name, node in document["nodes"].items()
        if node.get("type") == "interrupt"
    }


def _open_document(tmp_path: Path, document: dict):
    recipe = tmp_path / "compiled.recipe.yaml"
    recipe.write_text(yaml.safe_dump(document, sort_keys=False))
    store = RecipeBundleStore(tmp_path / "compiled-authority")
    materialized = (
        StrictRecipeIngress(tmp_path).inspect(recipe.name)
        .authorize(RecipeAuthorityPolicy()).capture(store).materialize(store)
    )
    return open_native_app(materialized)


def test_sequence_and_retry_are_native_edges_with_a_bounded_attempt_gate(
    tmp_path: Path,
) -> None:
    document = _compile(
        tmp_path,
        "  - step: edit\n"
        "    task: Edit\n"
        "    exit: Done\n"
        "    retry: {limit: 2, exhausted: escalate}\n"
        "  - verify:\n"
        "      id: tests\n"
        "      command: pytest -q\n"
        "      cwd: .\n"
        "      timeout: 30\n",
    )
    interrupts = _interrupts(document)
    assert {parse_effect_descriptor(n["message"]["lockstep_effect"]).kind for n in interrupts.values()} == {
        "manual",
        "verify",
    }
    assert document["loop_limits"]
    assert set(document["loop_limits"]) == set(document["loop_exits"])
    for target in document["loop_exits"].values():
        assert document["nodes"][target]["type"] == "passthrough"
        assert target not in interrupts
    verify = next(
        node
        for node in interrupts.values()
        if node["message"]["lockstep_effect"]["kind"] == "verify"
    )
    descriptor = parse_effect_descriptor(verify["message"]["lockstep_effect"])
    assert descriptor.deadline_seconds == 30
    assert descriptor.runner is not None
    assert descriptor.runner.selector == "pinned"
    command_key = dict(descriptor.inputs)["command"].state_key
    assert dict(descriptor.inputs)["snapshot"].runtime_key == "current_project_snapshot"
    assert document["state"][command_key] == "dict"
    assert any(
        node.get("output", {}).get(command_key)
        == {
            "schema": "lockstep.pinned-command/v1",
            "logical_argv": ["pytest", "-q"],
            "logical_cwd": ".",
            "result_source": "exit",
        }
        for node in document["nodes"].values()
    )


def test_repeat_final_failure_exits_without_an_extra_effect_attempt(
    tmp_path: Path,
) -> None:
    document = _compile(
        tmp_path,
        "  - repeat:\n"
        "      id: cycle\n"
        "      limit: 3\n"
        "      until: tests.passed\n"
        "      exhausted: escalate\n"
        "      do:\n"
        "        - step: edit\n"
        "          task: Edit\n"
        "          exit: Done\n"
        "        - verify:\n"
        "            id: tests\n"
        "            command: pytest -q\n",
    )

    assert 3 in document["loop_limits"].values()
    for source, target in document["loop_exits"].items():
        assert source in document["loop_limits"]
        assert document["nodes"][target]["type"] == "passthrough"
        assert document["nodes"][source]["type"] == "passthrough"


def test_retry_limit_is_exact_and_terminal_routes_are_graph_owned(tmp_path: Path) -> None:
    document = _compile(
        tmp_path,
        "  - step: edit\n"
        "    task: Edit\n"
        "    exit: Done\n"
        "    retry: {limit: 2, exhausted: escalate}\n",
    )
    app = _open_document(tmp_path, document)
    first = app.invoke({}, thread_id="retry-exact")
    second = app.resume(
        thread_id="retry-exact",
        results_by_interrupt_id={first.pending[0].coordinate.interrupt_id: {"outcome": "FAIL"}},
    )
    exhausted = app.resume(
        thread_id="retry-exact",
        results_by_interrupt_id={second.pending[0].coordinate.interrupt_id: {"outcome": "FAIL"}},
    )
    app.close()

    assert len(first.pending) == len(second.pending) == 1
    assert first.pending[0].coordinate != second.pending[0].coordinate
    assert exhausted.pending == ()
    assert exhausted.values["lockstep_outcome"] == "FAIL"


@pytest.mark.parametrize(
    ("result", "terminal"),
    [
        ({"outcome": "ERROR", "fixed_error_code": "provider_error"}, "ERROR"),
        ({"outcome": "ERROR", "fixed_error_code": "cancelled"}, "ABORTED"),
    ],
)
def test_effect_error_routes_are_closed(tmp_path: Path, result: dict, terminal: str) -> None:
    document = _compile(tmp_path, "  - step: edit\n    task: Edit\n    exit: Done\n")
    app = _open_document(tmp_path, document)
    parked = app.invoke({}, thread_id=f"terminal-{terminal}")
    completed = app.resume(
        thread_id=f"terminal-{terminal}",
        results_by_interrupt_id={parked.pending[0].coordinate.interrupt_id: result},
    )
    app.close()

    assert completed.pending == ()
    assert completed.values["lockstep_outcome"] == terminal


def test_escalate_is_graph_owned_terminal_state_not_an_external_effect(
    tmp_path: Path,
) -> None:
    document = _compile(tmp_path, "  - escalate: {}\n")

    assert _interrupts(document) == {}
    assert any(
        node == {
            "type": "passthrough",
            "output": {"lockstep_outcome": "FAIL"},
        }
        for node in document["nodes"].values()
    )
    assert any(edge["to"] == "END" for edge in document["edges"])


def test_decide_and_choose_lower_to_closed_native_routing(tmp_path: Path) -> None:
    document = _compile(
        tmp_path,
        "  - decide:\n"
        "      id: risk\n"
        "      using:\n"
        "        type: changed-paths\n"
        "        since: start\n"
        "        cases: {high: [auth/**, permissions/**]}\n"
        "        default: low\n"
        "  - choose:\n"
        "      value: risk\n"
        "      cases:\n"
        "        high: [{escalate: {}}]\n"
        "        low: [{escalate: {}}]\n",
    )
    decide = next(
        parse_effect_descriptor(node["message"]["lockstep_effect"])
        for node in _interrupts(document).values()
        if node["message"]["lockstep_effect"]["kind"] == "decide"
    )
    assert isinstance(decide, DecisionDescriptor)
    assert decide.result_schema == "lockstep.decision-result/v1"
    assert not hasattr(decide, "runner")
    assert decide.to_dict()["decision"] == {
        "type": "changed-paths",
        "since": "start",
        "cases": [{"label": "high", "paths": ["auth/**", "permissions/**"]}],
        "default": "low",
    }
    conditions = [edge["condition"] for edge in document["edges"] if "condition" in edge]
    assert conditions.index("risk_result.value == 'high'") < conditions.index(
        "risk_result.value == 'low'"
    )
    assert "risk_result.outcome == 'ERROR'" in conditions


def test_accept_and_publish_descriptors_bind_artifact_authority_without_fake_runner(
    tmp_path: Path,
) -> None:
    from lockstep.workflow.lowering import (
        lower_accept_descriptor,
        lower_publish_descriptor,
    )

    raw = lower_accept_descriptor(
        "accept-review",
        "review.review",
        "review_producer_result",
        "export-review",
        ".lockstep/review.md",
    )
    descriptor = parse_effect_descriptor(raw)

    assert isinstance(descriptor, AcceptDescriptor)
    assert descriptor.to_dict() == {
        "schema": "lockstep.effect/v1",
        "kind": "accept",
        "logical_id": "accept-review",
        "artifact_handle": "review.review",
        "producer_result_state_key": "review_producer_result",
        "declared_name": "export-review",
        "destination": ".lockstep/review.md",
        "transformation": "identity",
        "audience": "local-project",
        "verdict": "PASS",
        "result_schema": "lockstep.acceptance-result/v1",
    }
    assert not ({"runner", "writes", "deadline_seconds", "consent_ref"} & set(raw))

    publish = lower_publish_descriptor(
        "publish-review",
        artifact_handle="review.review",
        producer_result_state_key="review_producer_result",
        declared_name="export-review",
        acceptance_result_state_key="accept_review_result",
        destination=".lockstep/review.md",
    )
    assert publish == {
        "schema": "lockstep.effect/v1",
        "kind": "publish",
        "logical_id": "publish-review",
        "items": [
            {
                "qualified_handle": "review.review",
                "producer_result_state_key": "review_producer_result",
                "declared_name": "export-review",
                "acceptance_result_state_key": "accept_review_result",
                "destination": ".lockstep/review.md",
                "transformation": "identity",
                "audience": "local-project",
            }
        ],
        "result_schema": "lockstep.effect-result/v1",
    }
    assert "runner" not in publish

    changed_destination = lower_accept_descriptor(
        "accept-review",
        "review.review",
        "review_producer_result",
        "export-review",
        ".lockstep/other.md",
    )
    assert changed_destination != raw
    assert parse_effect_descriptor(changed_destination).digest != descriptor.digest
    assert changed_destination["destination"] == ".lockstep/other.md"


def test_generated_loop_exit_may_not_target_a_protected_interrupt_directly(
    tmp_path: Path,
) -> None:
    from lockstep.recipe.profile import check_recipe_full

    document = _compile(
        tmp_path,
        "  - step: edit\n"
        "    task: Edit\n"
        "    exit: Done\n",
    )
    interrupt = next(iter(_interrupts(document)))
    source = next(
        name for name, node in document["nodes"].items() if node["type"] == "passthrough"
    )
    document["loop_limits"] = {source: 1}
    document["loop_exits"] = {source: interrupt}
    recipe = tmp_path / "bad.recipe.yaml"
    recipe.write_text(yaml.safe_dump(document, sort_keys=False))
    errors, _warnings = check_recipe_full(recipe)

    assert any("loop_exits" in error and "interrupt" in error for error in errors)


def test_real_yamlgraph_loop_exit_gate_runs_interrupt_prepare_with_fresh_descriptor(
    tmp_path: Path,
) -> None:
    descriptor = {
        "schema": "lockstep.effect/v1",
        "kind": "manual",
        "logical_id": "after-loop",
        "runner": None,
        "inputs": {},
        "writes": [],
        "artifacts": [],
        "deadline_seconds": None,
        "scope_state_keys": [],
        "result_schema": "lockstep.effect-result/v1",
    }
    recipe = tmp_path / "safe-loop.recipe.yaml"
    recipe.write_text(yaml.safe_dump({
        "version": "1.0",
        "name": "safe-loop",
            "state": {"request": "dict", "result": "dict", "continue_loop": "bool"},
        "nodes": {
            "repeat": {"type": "passthrough", "output": {"continue_loop": True}},
            "exit-gate": {"type": "passthrough"},
            "protected": {
                "type": "interrupt", "message": {"lockstep_effect": descriptor},
                "state_key": "request", "resume_key": "result", "idempotent": False,
            },
        },
        "edges": [
            {"from": "START", "to": "repeat"},
            {"from": "repeat", "to": "repeat", "condition": "continue_loop == true"},
            {"from": "exit-gate", "to": "protected"},
        ],
        "loop_limits": {"repeat": 1},
        "loop_exits": {"repeat": "exit-gate"},
    }, sort_keys=False))
    store = RecipeBundleStore(tmp_path / "authority")
    materialized = (
        StrictRecipeIngress(tmp_path).inspect(recipe.name)
        .authorize(RecipeAuthorityPolicy()).capture(store).materialize(store)
    )
    app = open_native_app(materialized)
    parked = app.invoke({}, thread_id="safe-loop")
    app.close()

    assert len(parked.pending) == 1
    assert parked.pending[0].value == {"lockstep_effect": descriptor}
    assert parked.values["request"] == {"lockstep_effect": descriptor}
