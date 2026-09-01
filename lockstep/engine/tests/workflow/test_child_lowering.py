"""Task 9 red contracts for DSL direct-child lowering.

Each test names the production regression it must catch.  These assertions are
deliberately at the compiler/profile boundary: the implementation may choose
stable generated names, but may not replace native child composition with an
outer scheduler.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lockstep.recipe import yamlgraph_adapter as yg
from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.effects.models import ScopeDescriptor
from lockstep.workflow.compiler import compile_workflow
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import (
    CanonicalCompiledBundle,
    CatalogFile,
    ChildArtifactContract,
    ChildWorkflowContract,
    ResolvedCatalog,
    ResolvedChild,
    validate_semantics,
)


def _workflow(tmp_path: Path, name: str, flow: str):
    path = tmp_path / f"{name}.workflow.yaml"
    path.write_text(
        "workflow_version: '1'\n"
        f"name: {name}\n"
        "description: child lowering\n"
        "protect: ['**']\n"
        f"flow:\n{flow}"
    )
    return parse_workflow(load_workflow(path))


def _catalog() -> ResolvedCatalog:
    child_bytes = (
        b"version: '1.0'\nname: child\nstate: {lockstep_outcome: str}\n"
        b"nodes:\n  pass: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        b"edges: [{from: START, to: pass}, {from: pass, to: END}]\n"
    )
    child_file = CatalogFile.build("child.recipe.yaml", child_bytes)
    bundle = CanonicalCompiledBundle.build(
        root_relative_path="child.recipe.yaml",
        files=(child_file,),
        compiler_version="1",
    )
    contract = ChildWorkflowContract(outcomes=("pass", "fail", "error"))
    return ResolvedCatalog(
        children={
            "child": ResolvedChild(
                logical_name="child",
                contract=contract,
                source_definition_sha256="1" * 64,
                standalone=bundle,
            )
        }
    )


def _document(tmp_path: Path, flow: str) -> dict:
    catalog = _catalog()
    workflow = _workflow(tmp_path, "parent", flow)
    return yaml.safe_load(
        compile_workflow(validate_semantics(workflow, catalog), catalog).recipe_bytes
    )


def _protected(document: dict):
    for node in document["nodes"].values():
        message = node.get("message", {})
        descriptor = message.get("lockstep_effect")
        if isinstance(descriptor, dict):
            yield node, parse_effect_descriptor(descriptor)


def test_call_lowers_to_a_native_direct_subgraph_with_scope_before_and_after_mapping(
    tmp_path: Path,
) -> None:
    """Catches an outer child scheduler or a direct call without contract adapters."""
    document = _document(
        tmp_path,
        "  - call:\n"
        "      id: review\n"
        "      workflow: child\n"
        "      runner: codex\n"
        "      timeout_minutes: 5\n"
        "  - escalate: {}\n",
    )

    direct = [
        node
        for node in document["nodes"].values()
        if node.get("type") == "subgraph" and node.get("mode") == "direct"
    ]
    scopes = [
        (node, descriptor)
        for node, descriptor in _protected(document)
        if isinstance(descriptor, ScopeDescriptor) and descriptor.scope_kind == "call"
    ]

    assert len(direct) == 1
    assert direct[0].get("graph")
    assert len(scopes) == 1
    scope_node, scope = scopes[0]
    assert scope.runner_selector == "codex"
    assert scope.duration_seconds == 300
    assert document["state"][scope.result_state_key] == "dict"
    assert scope_node["resume_key"] == scope.result_state_key

    direct_name = next(name for name, node in document["nodes"].items() if node is direct[0])
    incoming = [edge["from"] for edge in document["edges"] if edge["to"] == direct_name]
    outgoing = [edge["to"] for edge in document["edges"] if edge["from"] == direct_name]
    assert incoming and outgoing
    assert incoming[0] != "START" and outgoing[0] != "END"


def test_call_bridges_only_declared_typed_inputs_and_exports(tmp_path: Path) -> None:
    child_bytes = (
        b"version: '1.0'\nname: child\nstate: {note: str, lockstep_outcome: str}\n"
        b"nodes:\n  pass: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        b"edges: [{from: START, to: pass}, {from: pass, to: END}]\n"
    )
    child_file = CatalogFile.build("child.recipe.yaml", child_bytes)
    contract = ChildWorkflowContract(
        outcomes=("pass", "fail", "error"),
        state_inputs={"note": "str"},
        state_exports={"note": "str"},
    )
    catalog = ResolvedCatalog(children={
        "child": ResolvedChild(
            "child", contract, "2" * 64,
            CanonicalCompiledBundle.build(
                root_relative_path="child.recipe.yaml",
                files=(child_file,), compiler_version="1",
            ),
        )
    })
    workflow = _workflow(
        tmp_path, "parent",
        "  - call:\n      id: review\n      workflow: child\n      runner: codex\n",
    )
    document = yaml.safe_load(
        compile_workflow(validate_semantics(workflow, catalog), catalog).recipe_bytes
    )
    direct = next(node for node in document["nodes"].values() if node.get("mode") == "direct")
    namespace = next(
        key.removesuffix("_note")
        for key in document["state"]
        if key.startswith("call_") and key.endswith("_note")
    )
    pre = next(
        node for node in document["nodes"].values()
        if node.get("output", {}).get(f"{namespace}_note") == "{state.note}"
    )
    post = next(
        node for node in document["nodes"].values()
        if node.get("output", {}).get("note") == f"{{state.{namespace}_note}}"
    )
    generated = next(
        item for item in compile_workflow(
            validate_semantics(workflow, catalog), catalog
        ).generated_files if item.relative_path == direct["graph"]
    )
    child_document = yaml.safe_load(generated.content)
    assert pre and post
    assert child_document["state"][f"{namespace}_note"] == "str"
    assert set(contract.state_inputs) == set(contract.state_exports) == {"note"}


@pytest.mark.parametrize(
    ("child_state", "message"),
    [
        ("{}", "missing"),
        ("{note: int}", "type mismatch"),
    ],
)
def test_call_rejects_missing_or_mismatched_child_contract_state(
    tmp_path: Path, child_state: str, message: str
) -> None:
    child_bytes = (
        f"version: '1.0'\nname: child\nstate: {child_state}\n"
        "nodes: {done: {type: passthrough}}\n"
        "edges: [{from: START, to: done}, {from: done, to: END}]\n"
    ).encode()
    child_file = CatalogFile.build("child.recipe.yaml", child_bytes)
    catalog = ResolvedCatalog(children={
        "child": ResolvedChild(
            "child",
                ChildWorkflowContract(
                    ("pass", "fail", "error"), state_inputs={"note": "str"}
                ),
            "3" * 64,
            CanonicalCompiledBundle.build(
                root_relative_path="child.recipe.yaml",
                files=(child_file,), compiler_version="1",
            ),
        )
    })
    workflow = _workflow(
        tmp_path, "parent",
        "  - call:\n      workflow: child\n      runner: codex\n",
    )
    with pytest.raises(ValueError, match=message):
        compile_workflow(validate_semantics(workflow, catalog), catalog)


def test_two_calls_have_distinct_specializations_and_restore_parent_context(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    workflow = _workflow(
        tmp_path, "parent",
        "  - call:\n      id: first\n      workflow: child\n      runner: codex\n"
        "  - call:\n      id: second\n      workflow: child\n      runner: codex\n",
    )
    result = compile_workflow(validate_semantics(workflow, catalog), catalog)
    document = yaml.safe_load(result.recipe_bytes)
    direct = [node for node in document["nodes"].values() if node.get("mode") == "direct"]
    assert len(direct) == 2
    assert len({node["graph"] for node in direct}) == 2
    assert len(result.generated_files) == 2
    post_nodes = [
        node for node in document["nodes"].values()
        if set(node.get("output", {})) >= {
            "current_step", "_loop_counts", "_loop_limit_reached",
        }
        and isinstance(node["output"]["current_step"], str)
    ]
    assert len(post_nodes) == 8
    for post in post_nodes:
        assert all(str(post["output"][key]).startswith("{state.call_") for key in (
            "current_step", "_loop_counts", "_loop_limit_reached",
        ))
    outcome_routers = [
        name for name in document["nodes"]
        if {
            condition
            for edge in document["edges"] if edge["from"] == name
            for condition in [edge.get("condition")]
            if condition
        }
        and any(
            "ABORTED" in edge.get("condition", "")
            for edge in document["edges"] if edge["from"] == name
        )
    ]
    assert len(outcome_routers) == 2
    for post_name in outcome_routers:
        routed = {
            edge.get("condition") for edge in document["edges"] if edge["from"] == post_name
        }
        assert any("ABORTED" in condition for condition in routed if condition)


def test_child_condition_rewrite_preserves_quoted_literals(tmp_path: Path) -> None:
    """Only parsed state references, never quoted values, are specialized."""
    child_bytes = (
        b"version: '1.0'\nname: child\nstate: {status: str, lockstep_outcome: str}\n"
        b"nodes:\n  pass: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        b"  fail: {type: passthrough, output: {lockstep_outcome: FAIL}}\n"
        b"edges:\n  - {from: START, to: pass, condition: \"status == 'status'\"}\n"
        b"  - {from: START, to: fail, condition: \"status != 'status'\"}\n"
        b"  - {from: pass, to: END}\n  - {from: fail, to: END}\n"
    )
    child_file = CatalogFile.build("child.recipe.yaml", child_bytes)
    catalog = ResolvedCatalog(children={
        "child": ResolvedChild(
            "child",
            ChildWorkflowContract(("pass", "fail", "error")),
            "7" * 64,
            CanonicalCompiledBundle.build(
                root_relative_path="child.recipe.yaml",
                files=(child_file,),
                compiler_version="1",
            ),
        )
    })
    workflow = _workflow(
        tmp_path, "parent",
        "  - call:\n      workflow: child\n      runner: codex\n",
    )

    result = compile_workflow(validate_semantics(workflow, catalog), catalog)
    child = yaml.safe_load(result.generated_files[0].content)
    conditions = [edge["condition"] for edge in child["edges"] if "condition" in edge]

    assert all("'status'" in condition for condition in conditions)
    assert all("'call_" not in condition for condition in conditions)
    assert all(condition.startswith("call_") for condition in conditions)


def test_child_public_state_cannot_alias_a_generated_effect_channel(
    tmp_path: Path,
) -> None:
    child_bytes = (
        b"version: '1.0'\nname: child\n"
        b"state: {foo_result: dict, lockstep_outcome: str}\n"
        b"nodes: {done: {type: passthrough, output: {lockstep_outcome: PASS}}}\n"
        b"edges: [{from: START, to: done}, {from: done, to: END}]\n"
    )
    child_file = CatalogFile.build("child.recipe.yaml", child_bytes)
    catalog = ResolvedCatalog(children={
        "child": ResolvedChild(
            "child",
            ChildWorkflowContract(
                ("pass", "fail", "error"), state_inputs={"foo_result": "dict"}
            ),
            "8" * 64,
            CanonicalCompiledBundle.build(
                root_relative_path="child.recipe.yaml",
                files=(child_file,),
                compiler_version="1",
            ),
        )
    })
    workflow = _workflow(
        tmp_path,
        "parent",
        "  - step: foo\n"
        "    id: foo\n"
        "    task: do work\n"
        "    'exit': when complete\n"
        "  - call:\n      workflow: child\n      runner: codex\n",
    )

    with pytest.raises(ValueError, match="generated channel"):
        compile_workflow(validate_semantics(workflow, catalog), catalog)


@pytest.mark.parametrize(
    "reserved_source_key",
    [
        "scope_request",
        "scope_result",
        "outcome",
        "parent_current_step",
        "parent_loop_counts",
        "parent_loop_limit_reached",
    ],
)
def test_child_state_cannot_alias_compiler_reserved_call_channels(
    tmp_path: Path, reserved_source_key: str,
) -> None:
    child_bytes = (
        b"version: '1.0'\nname: child\nstate:\n"
        + f"  {reserved_source_key}: dict\n".encode()
        + b"  lockstep_outcome: str\n"
        + b"nodes:\n  done:\n    type: passthrough\n    output:\n"
        + f"      {reserved_source_key}: {{forged: true}}\n".encode()
        + b"      lockstep_outcome: PASS\n"
        + b"edges: [{from: START, to: done}, {from: done, to: END}]\n"
    )
    child_file = CatalogFile.build("child.recipe.yaml", child_bytes)
    catalog = ResolvedCatalog(children={
        "child": ResolvedChild(
            "child",
            ChildWorkflowContract(("pass", "fail", "error")),
            "a" * 64,
            CanonicalCompiledBundle.build(
                root_relative_path="child.recipe.yaml",
                files=(child_file,),
                compiler_version="1",
            ),
        )
    })
    workflow = _workflow(
        tmp_path,
        "parent-reserved",
        "  - call:\n      workflow: child\n      runner: codex\n",
    )
    with pytest.raises(ValueError, match="compiler-reserved call channels"):
        compile_workflow(validate_semantics(workflow, catalog), catalog)


def test_two_native_calls_restore_parent_execution_context_at_runtime(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    workflow = _workflow(
        tmp_path,
        "parent",
        "  - call:\n      id: first\n      workflow: child\n      runner: codex\n"
        "  - call:\n      id: second\n      workflow: child\n      runner: codex\n",
    )
    result = compile_workflow(validate_semantics(workflow, catalog), catalog)
    for relative_path, content in result.executable_files.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    initial = {
        "current_step": "parent-step",
        "_loop_counts": {"parent-loop": 2},
        "_loop_limit_reached": True,
    }
    app = yg._open_native_path(tmp_path / result.root_relative_path)  # noqa: SLF001
    first_scope = app.invoke(initial, thread_id="sequential-context")
    second_scope = app.resume(
        thread_id="sequential-context",
        results_by_interrupt_id={
            first_scope.pending[0].coordinate.interrupt_id: {"outcome": "PASS"}
        },
    )
    assert second_scope.pending, second_scope
    completed = app.resume(
        thread_id="sequential-context",
        results_by_interrupt_id={
            second_scope.pending[0].coordinate.interrupt_id: {"outcome": "PASS"}
        },
    )
    app.close()

    saved_steps = {
        value for key, value in completed.values.items()
        if key.endswith("_parent_current_step")
    }
    assert saved_steps == {initial["current_step"]}
    saved_loop_counts = [
        value for key, value in completed.values.items()
        if key.endswith("_parent_loop_counts")
    ]
    assert saved_loop_counts
    assert all(
        not any(".pass" in node for node in counts)
        for counts in saved_loop_counts
    )
    assert all(
        value is True for key, value in completed.values.items()
        if key.endswith("_parent_loop_limit_reached")
    )
    assert completed.values["_loop_limit_reached"] is True
    assert completed.values["lockstep_outcome"] == "PASS"


def test_parent_effect_after_child_does_not_inherit_child_scope_lineage(
    tmp_path: Path,
) -> None:
    """A completed call scope is local to that child, not ambient authority."""
    catalog = _catalog()
    workflow = _workflow(
        tmp_path,
        "parent-after-child",
        "  - call:\n      workflow: child\n      runner: codex\n"
        "  - step: parent-work\n"
        "    task: parent work\n"
        "    'exit': when complete\n",
    )
    result = compile_workflow(validate_semantics(workflow, catalog), catalog)
    document = yaml.safe_load(result.recipe_bytes)
    parent_effect = next(
        descriptor
        for _node, descriptor in _protected(document)
        if descriptor.kind == "manual"
    )
    assert parent_effect.scope_state_keys == ()

    for relative_path, content in result.executable_files.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    app = yg._open_native_path(tmp_path / result.root_relative_path)  # noqa: SLF001
    child_scope = app.invoke({}, thread_id="parent-after-child")
    parent_pending = app.resume(
        thread_id="parent-after-child",
        results_by_interrupt_id={
            child_scope.pending[0].coordinate.interrupt_id: {"outcome": "PASS"}
        },
    )
    app.close()
    pending_descriptor = parse_effect_descriptor(
        parent_pending.pending[0].value["lockstep_effect"]
    )
    assert pending_descriptor.kind == "manual"
    assert pending_descriptor.scope_state_keys == ()


def test_specialization_preserves_topology_and_leaves_standalone_manual_bytes_stable(
    tmp_path: Path,
) -> None:
    child_bytes = (
        b"version: '1.0'\nname: child\nstate: {result: dict, lockstep_outcome: str}\n"
        b"nodes:\n  work:\n    type: interrupt\n    state_key: request\n"
        b"    resume_key: result\n    idempotent: false\n    message:\n"
        b"      lockstep_effect:\n        schema: lockstep.effect/v1\n"
        b"        kind: manual\n        logical_id: work\n        runner: null\n"
        b"        inputs: {}\n        writes: []\n        artifacts: []\n"
        b"        deadline_seconds: null\n        scope_state_keys: []\n"
        b"        result_schema: lockstep.effect-result/v1\n"
        b"  done: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        b"edges: [{from: START, to: work}, {from: work, to: done}, {from: done, to: END}]\n"
    )
    child_file = CatalogFile.build("child.recipe.yaml", child_bytes)
    bundle = CanonicalCompiledBundle.build(
        root_relative_path="child.recipe.yaml",
        files=(child_file,),
        compiler_version="1",
    )
    catalog = ResolvedCatalog(children={
        "child": ResolvedChild(
            "child", ChildWorkflowContract(("pass", "fail", "error")),
            "a" * 64, bundle,
        )
    })
    workflow = _workflow(
        tmp_path, "parent",
        "  - call:\n      workflow: child\n      runner: reviewer\n",
    )
    result = compile_workflow(validate_semantics(workflow, catalog), catalog)
    original = yaml.safe_load(child_bytes)
    specialized = yaml.safe_load(result.generated_files[0].content)
    node_map = {
        original_name: next(
            name for name in specialized["nodes"] if name.endswith("." + original_name)
        )
        for original_name in original["nodes"]
    }
    expected_edges = {
        (
            node_map.get(edge["from"], edge["from"]),
            node_map.get(edge["to"], edge["to"]),
        )
        for edge in original["edges"]
    }
    observed_edges = {(edge["from"], edge["to"]) for edge in specialized["edges"]}
    descriptor = specialized["nodes"][node_map["work"]]["message"]["lockstep_effect"]

    assert bundle.files[0].content == child_bytes
    assert set(node_map.values()) == set(specialized["nodes"])
    assert expected_edges == observed_edges
    assert original["nodes"]["work"]["message"]["lockstep_effect"]["runner"] is None
    assert descriptor["kind"] == "managed"
    assert descriptor["runner"]["selector"] == "reviewer"


def test_standalone_exported_step_remains_manual_and_retains_export_metadata(
    tmp_path: Path,
) -> None:
    workflow = _workflow(
        tmp_path,
        "standalone-export",
        "  - step: review\n"
        "    task: Review the change\n"
        "    'exit': Review is complete\n"
        "    writes: [review.md]\n"
        "    artifact:\n"
        "      handle: review\n"
        "      path: review.md\n"
        "      markdown: {sections: [Findings, Verdict]}\n",
    )
    catalog = ResolvedCatalog()

    compiled = compile_workflow(validate_semantics(workflow, catalog), catalog)
    document = yaml.safe_load(compiled.recipe_bytes)
    message = next(
        node["message"]
        for node in document["nodes"].values()
        if node.get("message", {}).get("lockstep_effect", {}).get("logical_id")
        == "review"
    )

    assert message["lockstep_effect"]["kind"] == "manual"
    assert message["lockstep_effect"]["runner"] is None
    assert message["lockstep_effect"]["writes"] == ["review.md"]
    assert message["lockstep_effect"]["artifacts"] == []
    assert message["artifact_contract"] == {
        "handle": "review",
        "path": "review.md",
        "markdown": {"sections": ["Findings", "Verdict"]},
    }


def test_accept_after_child_artifact_bridge_lowers_exact_publish(
    tmp_path: Path,
) -> None:
    """Catches live-path export or publication that bypasses explicit acceptance."""
    child_bytes = (
        b"version: '1.0'\nname: child\n"
        b"state: {review_result: dict, lockstep_outcome: str}\n"
        b"nodes:\n  review:\n    type: interrupt\n"
        b"    state_key: review_request\n    resume_key: review_result\n"
        b"    idempotent: false\n    message:\n"
        b"      lockstep_effect:\n        schema: lockstep.effect/v1\n"
        b"        kind: manual\n        logical_id: review\n        runner: null\n"
        b"        inputs: {}\n        writes: [review.md]\n"
        b"        artifacts:\n"
        b"          - {name: review, source_path: review.md, media_type: text/markdown, required: true}\n"
        b"        deadline_seconds: null\n        scope_state_keys: []\n"
        b"        result_schema: lockstep.effect-result/v1\n"
        b"  pass: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        b"edges: [{from: START, to: review}, {from: review, to: pass}, "
        b"{from: pass, to: END}]\n"
    )
    child_file = CatalogFile.build("child.recipe.yaml", child_bytes)
    catalog = ResolvedCatalog(
        children={
            "child": ResolvedChild(
                "child",
                ChildWorkflowContract(
                    ("pass", "fail", "error"),
                    exports={
                        "review": ChildArtifactContract(
                            "review", "review.md", "review", "text/markdown",
                            "review", "review_result"
                        )
                    },
                ),
                "d" * 64,
                CanonicalCompiledBundle.build(
                    root_relative_path="child.recipe.yaml",
                    files=(child_file,),
                    compiler_version="1",
                ),
            )
        }
    )
    workflow = _workflow(
        tmp_path,
        "parent",
        "  - call:\n"
        "      id: review-call\n"
        "      workflow: child\n"
        "      runner: codex\n"
        "      artifacts: {review: .lockstep/review.md}\n"
        "  - accept:\n"
        "      artifact_from: review-call.review\n"
        "      verdict: PASS\n",
    )
    compiled = compile_workflow(validate_semantics(workflow, catalog), catalog)
    document = yaml.safe_load(compiled.recipe_bytes)
    specialized = yaml.safe_load(compiled.generated_files[0].content)
    child_declaration = next(
        artifact
        for node in specialized["nodes"].values()
        for artifact in node.get("message", {})
        .get("lockstep_effect", {})
        .get("artifacts", [])
    )

    accept_nodes = [
        node
        for node in document["nodes"].values()
        if node.get("message", {}).get("lockstep_effect", {}).get("kind")
        == "accept"
    ]
    publish_nodes = [
        node
        for node in document["nodes"].values()
        if node.get("message", {}).get("lockstep_effect", {}).get("kind")
        == "publish"
    ]
    assert len(accept_nodes) == 1
    assert len(publish_nodes) == 1
    descriptor = publish_nodes[0]["message"]["lockstep_effect"]
    assert "runner" not in descriptor
    assert len(descriptor["items"]) == 1
    item = descriptor["items"][0]
    assert item["qualified_handle"] == "review-call.review"
    assert item["declared_name"] == child_declaration["name"]
    assert item["acceptance_result_state_key"] == accept_nodes[0]["resume_key"]
    assert item["destination"] == ".lockstep/review.md"
    assert item["transformation"] == "identity"
    assert item["audience"] == "local-project"
    bridge_key = item["producer_result_state_key"]
    assert document["state"][bridge_key] == "dict"
    assert any(
        node.get("output", {}).get(bridge_key, "").endswith("_review_result}")
        for node in document["nodes"].values()
    )


def _artifact_child_catalog(
    child_bytes: bytes,
    export: ChildArtifactContract | dict[str, ChildArtifactContract],
):
    child_file = CatalogFile.build("child.recipe.yaml", child_bytes)
    exports = export if isinstance(export, dict) else {"review": export}
    return ResolvedCatalog(
        children={
            "child": ResolvedChild(
                "child",
                ChildWorkflowContract(
                    ("pass", "fail", "error"), exports=exports
                ),
                "e" * 64,
                CanonicalCompiledBundle.build(
                    root_relative_path="child.recipe.yaml",
                    files=(child_file,),
                    compiler_version="1",
                ),
            )
        }
    )


def _exact_child_export_contract(**overrides) -> ChildArtifactContract:
    values = {
        "handle": "review",
        "fixed_source": "review.md",
        "declared_name": "review",
        "media_type": "text/markdown",
        "producer_logical_id": "review",
        "producer_result_state_key": "review_result",
    }
    values.update(overrides)
    return ChildArtifactContract(**values)


def _compile_artifact_child(tmp_path: Path, catalog: ResolvedCatalog):
    workflow = _workflow(
        tmp_path,
        "parent",
        "  - call:\n"
        "      id: review-call\n"
        "      workflow: child\n"
        "      runner: codex\n"
        "      artifacts: {review: .lockstep/review.md}\n"
        "  - accept:\n"
        "      artifact_from: review-call.review\n"
        "      verdict: PASS\n",
    )
    return compile_workflow(validate_semantics(workflow, catalog), catalog)


def test_child_artifact_contract_preserves_exact_producer_declaration_and_result_key(
    tmp_path: Path,
) -> None:
    child_bytes = (
        b"version: '1.0'\nname: child\n"
        b"state: {review_result: dict, lockstep_outcome: str}\n"
        b"nodes:\n  review:\n    type: interrupt\n"
        b"    state_key: review_request\n    resume_key: review_result\n"
        b"    idempotent: false\n    message:\n"
        b"      artifact_contract: {handle: review, path: review.md, markdown: {sections: [Findings, Verdict]}}\n"
        b"      lockstep_effect:\n        schema: lockstep.effect/v1\n"
        b"        kind: manual\n        logical_id: review\n        runner: null\n"
        b"        inputs: {}\n        writes: [review.md]\n"
        b"        artifacts:\n"
        b"          - {name: review, source_path: review.md, media_type: text/markdown, required: true}\n"
        b"        deadline_seconds: null\n        scope_state_keys: []\n"
        b"        result_schema: lockstep.effect-result/v1\n"
        b"  pass: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        b"edges: [{from: START, to: review}, {from: review, to: pass}, "
        b"{from: pass, to: END}]\n"
    )
    compiled = _compile_artifact_child(
        tmp_path,
        _artifact_child_catalog(child_bytes, _exact_child_export_contract()),
    )
    parent = yaml.safe_load(compiled.recipe_bytes)
    child = yaml.safe_load(compiled.generated_files[0].content)
    producer = next(
        node
        for node in child["nodes"].values()
        if node.get("message", {}).get("lockstep_effect", {}).get("artifacts")
    )
    declaration = producer["message"]["lockstep_effect"]["artifacts"][0]
    publish = next(
        node["message"]["lockstep_effect"]
        for node in parent["nodes"].values()
        if node.get("message", {}).get("lockstep_effect", {}).get("kind")
        == "publish"
    )

    assert declaration == {
        "name": "review",
        "source_path": "review.md",
        "media_type": "text/markdown",
        "required": True,
    }
    assert producer["message"]["artifact_contract"] == {
        "handle": "review",
        "path": "review.md",
        "markdown": {"sections": ["Findings", "Verdict"]},
    }
    bridge_key = publish["items"][0]["producer_result_state_key"]
    assert publish["items"][0]["declared_name"] == "review"
    assert any(
        node.get("output", {}).get(bridge_key, "").endswith("_review_result}")
        for node in parent["nodes"].values()
    )


def test_child_artifact_contract_rejects_a_writes_only_producer_match(
    tmp_path: Path,
) -> None:
    child_bytes = (
        b"version: '1.0'\nname: child\n"
        b"state: {other_result: dict, lockstep_outcome: str}\n"
        b"nodes:\n  other:\n    type: interrupt\n"
        b"    state_key: other_request\n    resume_key: other_result\n"
        b"    idempotent: false\n    message:\n"
        b"      lockstep_effect:\n        schema: lockstep.effect/v1\n"
        b"        kind: manual\n        logical_id: other\n        runner: null\n"
        b"        inputs: {}\n        writes: [review.md]\n        artifacts: []\n"
        b"        deadline_seconds: null\n        scope_state_keys: []\n"
        b"        result_schema: lockstep.effect-result/v1\n"
        b"  pass: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        b"edges: [{from: START, to: other}, {from: other, to: pass}, "
        b"{from: pass, to: END}]\n"
    )
    catalog = _artifact_child_catalog(child_bytes, _exact_child_export_contract())

    with pytest.raises(ValueError, match="producer"):
        _compile_artifact_child(tmp_path, catalog)


@pytest.mark.parametrize(
    "declaration",
    [
        "{name: other, source_path: review.md, media_type: text/markdown, required: true}",
        "{name: review, source_path: review.md, media_type: application/json, required: true}",
    ],
)
def test_child_artifact_contract_rejects_rewriting_a_mismatched_declaration(
    tmp_path: Path, declaration: str
) -> None:
    child_bytes = (
        "version: '1.0'\nname: child\n"
        "state: {review_result: dict, lockstep_outcome: str}\n"
        "nodes:\n  review:\n    type: interrupt\n"
        "    state_key: review_request\n    resume_key: review_result\n"
        "    idempotent: false\n    message:\n"
        "      lockstep_effect:\n        schema: lockstep.effect/v1\n"
        "        kind: manual\n        logical_id: review\n        runner: null\n"
        "        inputs: {}\n        writes: [review.md]\n"
        f"        artifacts: [{declaration}]\n"
        "        deadline_seconds: null\n        scope_state_keys: []\n"
        "        result_schema: lockstep.effect-result/v1\n"
        "  pass: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        "edges: [{from: START, to: review}, {from: review, to: pass}, "
        "{from: pass, to: END}]\n"
    ).encode()
    catalog = _artifact_child_catalog(child_bytes, _exact_child_export_contract())

    with pytest.raises(ValueError, match="artifact contract"):
        _compile_artifact_child(tmp_path, catalog)


def _multi_artifact_child_bytes(declarations: str) -> bytes:
    return (
        "version: '1.0'\nname: child\n"
        "state: {review_result: dict, lockstep_outcome: str}\n"
        "nodes:\n  review:\n    type: interrupt\n"
        "    state_key: review_request\n    resume_key: review_result\n"
        "    idempotent: false\n    message:\n"
        "      lockstep_effect:\n        schema: lockstep.effect/v1\n"
        "        kind: manual\n        logical_id: review\n        runner: null\n"
        "        inputs: {}\n        writes: [exports/]\n"
        f"        artifacts:\n{declarations}"
        "        deadline_seconds: null\n        scope_state_keys: []\n"
        "        result_schema: lockstep.effect-result/v1\n"
        "  pass: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        "edges: [{from: START, to: review}, {from: review, to: pass}, "
        "{from: pass, to: END}]\n"
    ).encode()


def _child_export(
    handle: str, source: str, *, declared_name: str | None = None
) -> ChildArtifactContract:
    return ChildArtifactContract(
        handle=handle,
        fixed_source=source,
        declared_name=declared_name or handle,
        media_type="text/markdown",
        producer_logical_id="review",
        producer_result_state_key="review_result",
    )


def _compile_multi_artifact_child(
    tmp_path: Path,
    catalog: ResolvedCatalog,
    *,
    mappings: str,
    accepts: tuple[str, ...],
):
    accept_flow = "".join(
        "  - accept:\n"
        f"      artifact_from: review-call.{handle}\n"
        "      verdict: PASS\n"
        for handle in accepts
    )
    workflow = _workflow(
        tmp_path,
        "parent",
        "  - call:\n"
        "      id: review-call\n"
        "      workflow: child\n"
        "      runner: codex\n"
        f"      artifacts: {mappings}\n"
        + accept_flow,
    )
    return compile_workflow(validate_semantics(workflow, catalog), catalog)


def _specialized_producer_artifacts(compiled) -> list[dict]:
    child = yaml.safe_load(compiled.generated_files[0].content)
    return next(
        node["message"]["lockstep_effect"]["artifacts"]
        for node in child["nodes"].values()
        if node.get("message", {}).get("lockstep_effect", {}).get("artifacts")
    )


def test_two_exports_from_one_producer_preserve_descriptor_artifact_order(
    tmp_path: Path,
) -> None:
    declarations = (
        "          - {name: alpha, source_path: exports/alpha.md, media_type: text/markdown, required: true}\n"
        "          - {name: beta, source_path: exports/beta.md, media_type: text/markdown, required: true}\n"
    )
    catalog = _artifact_child_catalog(
        _multi_artifact_child_bytes(declarations),
        {
            "alpha": _child_export("alpha", "exports/alpha.md"),
            "beta": _child_export("beta", "exports/beta.md"),
        },
    )

    compiled = _compile_multi_artifact_child(
        tmp_path,
        catalog,
        mappings="{beta: .lockstep/beta.md, alpha: .lockstep/alpha.md}",
        accepts=("beta", "alpha"),
    )

    assert _specialized_producer_artifacts(compiled) == [
        {
            "name": "alpha",
            "source_path": "exports/alpha.md",
            "media_type": "text/markdown",
            "required": True,
        },
        {
            "name": "beta",
            "source_path": "exports/beta.md",
            "media_type": "text/markdown",
            "required": True,
        },
    ]


def test_exporting_one_artifact_preserves_unexported_descriptor_artifacts(
    tmp_path: Path,
) -> None:
    declarations = (
        "          - {name: alpha, source_path: exports/alpha.md, media_type: text/markdown, required: true}\n"
        "          - {name: private, source_path: exports/private.md, media_type: text/markdown, required: true}\n"
    )
    catalog = _artifact_child_catalog(
        _multi_artifact_child_bytes(declarations),
        {"alpha": _child_export("alpha", "exports/alpha.md")},
    )

    compiled = _compile_multi_artifact_child(
        tmp_path,
        catalog,
        mappings="{alpha: .lockstep/alpha.md}",
        accepts=("alpha",),
    )

    assert _specialized_producer_artifacts(compiled) == [
        {
            "name": "alpha",
            "source_path": "exports/alpha.md",
            "media_type": "text/markdown",
            "required": True,
        },
        {
            "name": "private",
            "source_path": "exports/private.md",
            "media_type": "text/markdown",
            "required": True,
        },
    ]
