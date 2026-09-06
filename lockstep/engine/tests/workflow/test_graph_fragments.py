"""Task 9 public contracts for graph and include_graph escape hatches."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from lockstep.recipe import yamlgraph_adapter as yg
from lockstep.recipe.profile import check_recipe_full
from lockstep.workflow._lowering_contracts import _FragmentNames
from lockstep.workflow.compiler import compile_workflow
from lockstep.workflow.diagnostics import DiagnosticError
from lockstep.workflow.ir import FragmentIR
from lockstep.workflow.lowering import _Builder
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import (
    InMemoryWorkflowCatalog,
    ResolvedCatalog,
    ResolvedFragment,
    validate_semantics,
)


def _compile(tmp_path: Path, flow: str) -> dict:
    path = tmp_path / "fragments.workflow.yaml"
    path.write_text(
        "workflow_version: '1'\n"
        "name: fragments\n"
        "description: native fragments\n"
        "protect: ['**']\n"
        f"flow:\n{flow}"
    )
    catalog = InMemoryWorkflowCatalog({})
    return yaml.safe_load(
        compile_workflow(
            validate_semantics(parse_workflow(load_workflow(path)), catalog), catalog
        ).recipe_bytes
    )


def _state(namespace: str, key: str) -> str:
    digest = hashlib.sha256(
        b"lockstep.fragment-state-namespace/v1\0" + namespace.encode()
    ).hexdigest()
    return f"fragment_{digest}_{key}"


def test_fragment_interrupt_validates_descriptor_before_declaring_channels() -> None:
    builder = object.__new__(_Builder)
    builder.state = {}
    builder.generated_state_names = set()
    copied = {"state_key": "request", "message": {}}

    with pytest.raises(
        ValueError, match="fragment interrupts must carry a protected descriptor"
    ):
        builder._rewrite_fragment_interrupt(
            copied,
            {"resume_key": "result"},
            _FragmentNames("fragment", {}),
            set(),
        )

    assert builder.state == {}
    assert builder.generated_state_names == set()


_READ_ONLY_FRAGMENT = """\
      id: inspect
      fragment:
        entry: begin
        exits: {pass: finish}
        effects: {mode: read-only, writes: []}
      state: {note: str}
      nodes:
        begin: {type: passthrough, output: {note: checked}}
        finish: {type: passthrough}
      edges:
        - {from: begin, to: finish}
"""


def test_fragment_native_join_waits_for_unequal_branches(tmp_path: Path) -> None:
    document = _compile(tmp_path,
        "  - graph:\n      id: joined\n"
        "      fragment:\n        entry: begin\n        exits: {pass: finish}\n"
        "        effects: {mode: read-only, writes: []}\n"
        "      state: {left: int, right: int, total: int}\n"
        "      nodes:\n"
        "        begin: {type: passthrough, output: {total: 0}}\n"
        "        left: {type: passthrough, output: {left: 1}}\n"
        "        middle: {type: passthrough}\n"
        "        right: {type: passthrough, output: {right: 2}}\n"
        "        finish: {type: passthrough, output: {total: '{state.total + 1}'}}\n"
        "      edges:\n"
        "        - {from: begin, to: left}\n"
        "        - {from: begin, to: middle}\n"
        "        - {from: middle, to: right}\n"
        "        - {from: [left, right], to: finish}\n"
    )
    recipe = tmp_path / "joined.recipe.yaml"
    recipe.write_text(yaml.safe_dump(document, sort_keys=False))
    app = yg._open_native_path(recipe)
    try:
        completed = app.invoke({}, thread_id="fragment-join")
    finally:
        app.close()
    assert completed.values[_state("joined", "total")] == 1
    assert completed.values[_state("joined", "left")] == 1
    assert completed.values[_state("joined", "right")] == 2


def test_inline_graph_fragment_is_namespaced_and_connected_through_declared_entry_exits(
    tmp_path: Path,
) -> None:
    """Catches a fragment node collision or a bypass around its declared entry/exit."""
    document = _compile(
        tmp_path,
        "  - graph:\n" + _READ_ONLY_FRAGMENT + "  - escalate: {}\n",
    )

    assert "begin" not in document["nodes"]
    assert "finish" not in document["nodes"]
    fragment_nodes = [
        (name, node)
        for name, node in document["nodes"].items()
        if node.get("output", {}).get(_state("inspect", "note")) == "checked"
    ]
    assert len(fragment_nodes) == 1
    entry, _node = fragment_nodes[0]
    inbound = [edge for edge in document["edges"] if edge["to"] == entry]
    assert inbound and all(edge["from"] != "START" for edge in inbound)
    assert document["state"][_state("inspect", "note")] == "str"


def test_two_fragments_with_the_same_local_names_are_isolated(tmp_path: Path) -> None:
    """Catches merging two independently authored fragments into one node namespace."""
    first = _READ_ONLY_FRAGMENT.replace("id: inspect", "id: first")
    second = _READ_ONLY_FRAGMENT.replace("id: inspect", "id: second").replace(
        "note: str", "other_note: str"
    ).replace("note: checked", "other_note: checked")
    document = _compile(
        tmp_path,
        "  - graph:\n" + first + "  - graph:\n" + second + "  - escalate: {}\n",
    )

    generated = [
        node for node in document["nodes"].values() if node.get("type") == "passthrough"
    ]
    assert sum(node.get("output", {}).get(_state("first", "note")) == "checked" for node in generated) == 1
    assert sum(node.get("output", {}).get(_state("second", "other_note")) == "checked" for node in generated) == 1
    assert document["state"][_state("first", "note")] == "str"
    assert document["state"][_state("second", "other_note")] == "str"


def test_fragment_identity_namespace_is_injective_across_component_boundaries(
    tmp_path: Path,
) -> None:
    """`a-b`+`c` and `a`+`b-c` must never alias an effect identity."""
    def fragment(fragment_id: str, logical_id: str) -> str:
        return (
            "  - graph:\n"
            f"      id: {fragment_id}\n"
            "      fragment:\n"
            "        entry: work\n"
            "        exits: {pass: passed, fail: failed, error: errored}\n"
            "        effects: {mode: read-only, writes: []}\n"
            "      state: {request: dict, result: dict}\n"
            "      nodes:\n"
            "        work:\n"
            "          type: interrupt\n"
            "          state_key: request\n"
            "          resume_key: result\n"
            "          idempotent: false\n"
            "          message:\n"
            f"            step: {logical_id}\n"
            "            lockstep_effect:\n"
            "              schema: lockstep.effect/v1\n"
            "              kind: manual\n"
            f"              logical_id: {logical_id}\n"
            "              runner: null\n"
            "              inputs: {}\n"
            "              writes: []\n"
            "              artifacts: []\n"
            "              deadline_seconds: null\n"
            "              scope_state_keys: []\n"
            "              result_schema: lockstep.effect-result/v1\n"
            "        passed: {type: passthrough}\n"
            "        failed: {type: passthrough}\n"
            "        errored: {type: passthrough}\n"
            "      edges:\n"
            "        - {from: work, to: passed, condition: \"result.outcome == 'PASS'\"}\n"
            "        - {from: work, to: failed, condition: \"result.outcome == 'FAIL'\"}\n"
            "        - {from: work, to: errored, condition: \"result.outcome == 'ERROR'\"}\n"
        )

    document = _compile(tmp_path, fragment("a-b", "c") + fragment("a", "b-c"))
    descriptors = [
        node["message"]["lockstep_effect"]
        for node in document["nodes"].values()
        if isinstance(node.get("message"), dict)
        and "lockstep_effect" in node["message"]
    ]
    logical_ids = {descriptor["logical_id"] for descriptor in descriptors}
    step_ids = {
        node["message"]["step"]
        for node in document["nodes"].values()
        if isinstance(node.get("message"), dict)
        and "lockstep_effect" in node["message"]
    }
    assert len(logical_ids) == 2
    assert len(step_ids) == 2
    assert "fragment-a-b-c" not in logical_ids | step_ids


def test_fragment_arithmetic_template_executes_against_safe_flat_state_key(
    tmp_path: Path,
) -> None:
    document = _compile(
        tmp_path,
        "  - graph:\n"
        "      id: counter\n"
        "      fragment:\n"
        "        entry: seed\n"
        "        exits: {pass: done}\n"
        "        effects: {mode: read-only, writes: []}\n"
        "      state: {count: int}\n"
        "      nodes:\n"
        "        seed: {type: passthrough, output: {count: 1}}\n"
        "        increment: {type: passthrough, output: {count: '{state.count + 1}'}}\n"
        "        done: {type: passthrough}\n"
        "      edges:\n"
        "        - {from: seed, to: increment}\n"
        "        - {from: increment, to: done}\n",
    )
    recipe = tmp_path / "counter.recipe.yaml"
    recipe.write_text(yaml.safe_dump(document, sort_keys=False))

    app = yg._open_native_path(recipe)  # noqa: SLF001 - real yamlgraph oracle
    completed = app.invoke({}, thread_id="fragment-counter")
    app.close()

    assert completed.values[_state("counter", "count")] == 2


def test_fragment_bounded_cycle_preserves_native_loop_limit_and_exit(
    tmp_path: Path,
) -> None:
    document = _compile(
        tmp_path,
        "  - graph:\n"
        "      id: bounded\n"
        "      fragment:\n"
        "        entry: spin\n"
        "        exits: {pass: done, error: invalid}\n"
        "        effects: {mode: read-only, writes: []}\n"
        "      state: {count: int, keep_going: bool}\n"
        "      nodes:\n"
        "        spin: {type: passthrough, output: {count: '{state.count + 1}', keep_going: true}}\n"
        "        done: {type: passthrough}\n"
        "        invalid: {type: passthrough}\n"
        "      edges:\n"
        "        - {from: spin, to: spin, condition: 'keep_going == true'}\n"
        "        - {from: spin, to: invalid, condition: 'keep_going != true'}\n"
        "      loop_limits: {spin: 2}\n"
        "      loop_exits: {spin: done}\n",
    )
    loop_node = next(iter(document["loop_limits"]))
    exit_node = document["loop_exits"][loop_node]
    assert document["loop_limits"][loop_node] == 2
    assert exit_node in document["nodes"]
    recipe = tmp_path / "bounded.recipe.yaml"
    recipe.write_text(yaml.safe_dump(document, sort_keys=False))

    app = yg._open_native_path(recipe)  # noqa: SLF001 - real yamlgraph oracle
    completed = app.invoke({_state("bounded", "count"): 0}, thread_id="bounded")
    app.close()

    assert completed.values[_state("bounded", "count")] == 2
    assert completed.values["lockstep_outcome"] == "PASS"


def test_fragment_condition_rewrite_preserves_literal_and_routes_natively(
    tmp_path: Path,
) -> None:
    document = _compile(
        tmp_path,
        "  - graph:\n"
        "      id: literal\n"
        "      fragment:\n"
        "        entry: choose\n"
        "        exits: {pass: matched}\n"
        "        effects: {mode: read-only, writes: []}\n"
        "      state: {status: str, result: str}\n"
        "      nodes:\n"
        "        choose: {type: passthrough, output: {status: status}}\n"
        "        matched: {type: passthrough, output: {result: matched}}\n"
        "        missed: {type: passthrough, output: {result: missed}}\n"
        "      edges:\n"
        "        - {from: choose, to: matched, condition: \"status == 'status'\"}\n"
        "        - {from: choose, to: missed, condition: \"status != 'status'\"}\n"
        "        - {from: missed, to: matched}\n",
    )
    conditions = [edge["condition"] for edge in document["edges"] if "condition" in edge]
    assert all("'status'" in condition for condition in conditions)
    assert all("'fragment_" not in condition for condition in conditions)
    recipe = tmp_path / "literal.recipe.yaml"
    recipe.write_text(yaml.safe_dump(document, sort_keys=False))

    app = yg._open_native_path(recipe)  # noqa: SLF001 - real routing oracle
    completed = app.invoke({}, thread_id="fragment-literal")
    app.close()

    assert completed.values[_state("literal", "result")] == "matched"


@pytest.mark.parametrize(
    "document",
    [
        {"fragment": {"entry": "done", "exits": {"pass": "done"}, "effects": {"mode": "read-only", "writes": []}}, "state": [], "nodes": {"done": {"type": "passthrough"}}, "edges": []},
        {"fragment": {"entry": "done", "exits": {"pass": "done"}, "effects": {"mode": "read-only", "writes": []}}, "nodes": {1: {"type": "passthrough"}}, "edges": []},
        {"fragment": {"entry": "done", "exits": {"pass": "done"}, "effects": {"mode": "read-only", "writes": []}}, "nodes": {"done": {"type": "passthrough", "output": []}}, "edges": []},
        {"fragment": {"entry": "done", "exits": {"pass": "done"}, "effects": {"mode": "read-only", "writes": []}}, "nodes": {"done": {"type": "passthrough"}}, "edges": [], "loop_limits": []},
        {"fragment": {"entry": "ask", "exits": {"pass": "ask"}, "effects": {"mode": "read-only", "writes": []}}, "nodes": {"ask": {"type": "interrupt", "message": {}, "state_key": "request"}}, "edges": []},
        {"fragment": {"entry": "done", "exits": {"pass": "done", "fail": "done"}, "effects": {"mode": "read-only", "writes": []}}, "nodes": {"done": {"type": "passthrough"}}, "edges": []},
        {"fragment": {"entry": "done", "exits": {"pass": "done"}, "effects": {"mode": "declared-writes", "writes": []}}, "nodes": {"done": {"type": "passthrough"}}, "edges": []},
    ],
)
def test_fragment_ir_rejects_malformed_closed_shapes_at_construction(
    document: dict,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        FragmentIR.parse(document)


def test_fragment_scope_result_channel_must_equal_interrupt_resume_channel() -> None:
    document = {
        "fragment": {
            "entry": "scope",
            "exits": {"pass": "passed", "error": "errored"},
            "effects": {"mode": "read-only", "writes": []},
        },
        "state": {"request": "dict", "actual": "dict", "claimed": "dict"},
        "nodes": {
            "scope": {
                "type": "interrupt",
                "state_key": "request",
                "resume_key": "actual",
                "idempotent": False,
                "message": {
                    "lockstep_effect": {
                        "schema": "lockstep.effect/v1",
                        "kind": "scope",
                        "logical_id": "scope",
                        "scope_kind": "call",
                        "duration_seconds": None,
                        "runner_selector": "codex",
                        "ancestor_deadline_state_keys": [],
                        "result_state_key": "claimed",
                        "result_schema": "lockstep.scope-result/v1",
                    }
                },
            },
            "passed": {"type": "passthrough"},
            "errored": {"type": "passthrough"},
        },
        "edges": [
            {"from": "scope", "to": "passed", "condition": "actual.outcome == 'PASS'"},
            {"from": "scope", "to": "errored", "condition": "actual.outcome == 'ERROR'"},
        ],
    }
    with pytest.raises(ValueError, match="result_state_key must equal"):
        FragmentIR.parse(document)


def test_fragment_scope_selectors_require_declared_dict_state() -> None:
    document = {
        "fragment": {
            "entry": "work",
            "exits": {"pass": "work", "fail": "failed", "error": "errored"},
            "effects": {"mode": "read-only", "writes": []},
        },
        "state": {"request": "dict", "result": "dict", "scope": "bool"},
        "nodes": {
            "work": {
                "type": "interrupt",
                "state_key": "request",
                "resume_key": "result",
                "idempotent": False,
                "message": {
                    "lockstep_effect": {
                        "schema": "lockstep.effect/v1",
                        "kind": "manual",
                        "logical_id": "work",
                        "runner": None,
                        "inputs": {},
                        "writes": [],
                        "artifacts": [],
                        "deadline_seconds": None,
                        "scope_state_keys": ["scope"],
                        "result_schema": "lockstep.effect-result/v1",
                    }
                },
            },
            "failed": {"type": "passthrough"},
            "errored": {"type": "passthrough"},
        },
        "edges": [],
    }
    with pytest.raises(ValueError, match="scope selectors"):
        FragmentIR.parse(document)


def test_include_graph_rejects_noncontained_paths_before_loading(tmp_path: Path) -> None:
    """Catches include_graph reading a sibling or absolute file outside the sealed DAG."""
    source = tmp_path / "fragments.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: fragments\ndescription: native fragments\n"
        "protect: ['**']\nflow:\n"
        "  - include_graph: {id: inspect, path: ../fragment.yaml}\n"
    )

    with pytest.raises(DiagnosticError, match="safe relative"):
        parse_workflow(load_workflow(source))


def test_include_graph_uses_resolved_effects_manifest_and_runs_natively(
    tmp_path: Path,
) -> None:
    fragment_document = {
        "fragment": {
            "entry": "work",
            "exits": {"pass": "passed", "fail": "failed", "error": "errored"},
            "effects": {"mode": "declared-writes", "writes": ["src/"]},
        },
        "state": {"request": "dict", "result": "dict"},
        "nodes": {
            "work": {
                "type": "interrupt",
                "state_key": "request",
                "resume_key": "result",
                "idempotent": False,
                "message": {
                    "lockstep_effect": {
                        "schema": "lockstep.effect/v1",
                        "kind": "manual",
                        "logical_id": "included-work",
                        "runner": None,
                        "inputs": {},
                            "writes": ["src/"],
                        "artifacts": [],
                        "deadline_seconds": None,
                        "scope_state_keys": [],
                        "result_schema": "lockstep.effect-result/v1",
                    }
                },
            },
            "passed": {"type": "passthrough"},
            "failed": {"type": "passthrough"},
            "errored": {"type": "passthrough"},
        },
        "edges": [
            {"from": "work", "to": "passed", "condition": "result.outcome == 'PASS'"},
            {"from": "work", "to": "failed", "condition": "result.outcome == 'FAIL'"},
            {"from": "work", "to": "errored", "condition": "result.outcome == 'ERROR'"},
        ],
    }
    logical_path = "fragments/review.yaml"
    catalog = ResolvedCatalog(fragments={
        logical_path: ResolvedFragment(
            logical_path,
            hashlib.sha256(b"review-fragment-source").hexdigest(),
            FragmentIR.parse(fragment_document),
        )
    })
    source = tmp_path / "included.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: included\ndescription: included\n"
        "protect: ['**']\nflow:\n"
        "  - include_graph:\n"
        "      id: gate\n"
        f"      path: {logical_path}\n"
        "      on: {pass: next, fail: escalate, error: escalate}\n"
    )
    validated = validate_semantics(parse_workflow(load_workflow(source)), catalog)
    assert validated.flow.effects.writes == ("src/",)

    result = compile_workflow(validated, catalog)
    assert [(entry.kind, entry.logical_name) for entry in result.dependency_manifest.entries] == [
        ("fragment", logical_path)
    ]
    recipe = tmp_path / result.root_relative_path
    recipe.write_bytes(result.recipe_bytes)
    app = yg._open_native_path(recipe)  # noqa: SLF001 - positive include oracle
    parked = app.invoke({}, thread_id="included-fragment")
    completed = app.resume(
        thread_id="included-fragment",
        results_by_interrupt_id={
            parked.pending[0].coordinate.interrupt_id: {
                "schema": "lockstep.effect-result/v1",
                "outcome": "PASS",
            }
        },
    )
    app.close()
    assert completed.values["lockstep_outcome"] == "PASS"


def test_include_graph_rejects_an_authored_handler_for_undeclared_exit(
    tmp_path: Path,
) -> None:
    logical_path = "fragments/pass-only.yaml"
    fragment = FragmentIR.parse({
        "fragment": {
            "entry": "done",
            "exits": {"pass": "done"},
            "effects": {"mode": "read-only", "writes": []},
        },
        "nodes": {"done": {"type": "passthrough"}},
        "edges": [],
    })
    catalog = ResolvedCatalog(fragments={
        logical_path: ResolvedFragment(logical_path, "b" * 64, fragment)
    })
    source = tmp_path / "bad-on.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: bad-on\ndescription: bad on\n"
        "protect: ['**']\nflow:\n"
        "  - include_graph:\n"
        "      id: inspect\n"
        f"      path: {logical_path}\n"
        "      on: {pass: next, fail: escalate}\n"
    )
    workflow = parse_workflow(load_workflow(source))

    with pytest.raises(DiagnosticError, match="undeclared fragment exit"):
        validate_semantics(workflow, catalog)


def test_manual_parent_cannot_smuggle_a_compiler_only_scope_through_a_child(
    tmp_path: Path,
) -> None:
    """Catches profile validation checking only a manual root and trusting its child."""
    child = tmp_path / "child.recipe.yaml"
    child.write_text(
        "version: '1.0'\nname: child\nstate: {call_scope: dict}\nnodes:\n"
        "  scope:\n    type: interrupt\n    state_key: call_scope_request\n"
        "    resume_key: call_scope\n    idempotent: false\n"
        "    message:\n      lockstep_effect:\n"
        "        schema: lockstep.effect/v1\n        kind: scope\n        logical_id: child-scope\n"
        "        scope_kind: call\n        duration_seconds: 60\n        runner_selector: codex\n"
        "        ancestor_deadline_state_keys: []\n        result_state_key: call_scope\n"
        "        result_schema: lockstep.scope-result/v1\n"
        "edges: [{from: START, to: scope}, {from: scope, to: END}]\n"
    )
    root = tmp_path / "root.recipe.yaml"
    root.write_text(
        "version: '1.0'\nname: root\nnodes:\n"
        "  child: {type: subgraph, graph: child.recipe.yaml, mode: direct}\n"
        "edges: [{from: START, to: child}, {from: child, to: END}]\n"
    )

    errors, _warnings = check_recipe_full(root)

    assert any("scope descriptor" in error and "child" in error for error in errors)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "      nodes: {done: {type: python, tool: escape}}\n"
            "      edges: []\n",
            "allowlist",
        ),
        (
            "      nodes: {done: {type: passthrough}}\n"
            "      edges: []\n",
            "declared writes",
        ),
        (
            "      state: {known: int}\n"
            "      nodes:\n"
            "        spin: {type: passthrough, output: {known: '{state.unknown + 1}'}}\n"
            "        done: {type: passthrough}\n"
            "      edges: [{from: spin, to: spin}]\n",
            "unknown state",
        ),
    ],
)
def test_fragment_negative_profile_is_fail_closed(
    tmp_path: Path, body: str, message: str
) -> None:
    effects = (
        "declared-writes, writes: [src/, src/]"
        if message == "declared writes"
        else "read-only, writes: []"
    )
    flow = (
        "  - graph:\n"
        "      id: rejected\n"
        "      fragment:\n"
        "        entry: " + ("spin" if "spin:" in body else "done") + "\n"
        "        exits: {pass: done}\n"
        f"        effects: {{mode: {effects}}}\n"
        + body
    )
    with pytest.raises(ValueError, match=message):
        _compile(tmp_path, flow)


def test_fallible_fragment_effect_cannot_launder_failure_into_pass(
    tmp_path: Path,
) -> None:
    flow = (
        "  - graph:\n"
        "      id: unsafe\n"
        "      fragment:\n"
        "        entry: work\n"
        "        exits: {pass: done}\n"
        "        effects: {mode: read-only, writes: []}\n"
        "      state: {request: dict, result: dict}\n"
        "      nodes:\n"
        "        work:\n"
        "          type: interrupt\n"
        "          state_key: request\n"
        "          resume_key: result\n"
        "          idempotent: false\n"
        "          message:\n"
        "            lockstep_effect:\n"
        "              schema: lockstep.effect/v1\n"
        "              kind: manual\n"
        "              logical_id: work\n"
        "              runner: null\n"
        "              inputs: {}\n"
        "              writes: []\n"
        "              artifacts: []\n"
        "              deadline_seconds: null\n"
        "              scope_state_keys: []\n"
        "              result_schema: lockstep.effect-result/v1\n"
        "        done: {type: passthrough}\n"
        "      edges: [{from: work, to: done}]\n"
    )

    with pytest.raises(ValueError, match="fragment effect"):
        _compile(tmp_path, flow)


def test_fragment_declared_writes_equal_the_exact_effect_union(
    tmp_path: Path,
) -> None:
    flow = (
        "  - graph:\n"
        "      id: mismatched-writes\n"
        "      fragment:\n"
        "        entry: work\n"
        "        exits: {pass: passed, fail: failed, error: errored}\n"
        "        effects: {mode: declared-writes, writes: [other/]}\n"
        "      state: {request: dict, result: dict}\n"
        "      nodes:\n"
        "        work:\n"
        "          type: interrupt\n"
        "          state_key: request\n"
        "          resume_key: result\n"
        "          idempotent: false\n"
        "          message:\n"
        "            lockstep_effect:\n"
        "              schema: lockstep.effect/v1\n"
        "              kind: manual\n"
        "              logical_id: work\n"
        "              runner: null\n"
        "              inputs: {}\n"
        "              writes: [src/]\n"
        "              artifacts: []\n"
        "              deadline_seconds: null\n"
        "              scope_state_keys: []\n"
        "              result_schema: lockstep.effect-result/v1\n"
        "        passed: {type: passthrough}\n"
        "        failed: {type: passthrough}\n"
        "        errored: {type: passthrough}\n"
        "      edges:\n"
        "        - {from: work, to: passed, condition: \"result.outcome == 'PASS'\"}\n"
        "        - {from: work, to: failed, condition: \"result.outcome == 'FAIL'\"}\n"
        "        - {from: work, to: errored, condition: \"result.outcome == 'ERROR'\"}\n"
    )
    with pytest.raises(ValueError, match="exactly equal protected descriptor writes"):
        _compile(tmp_path, flow)


def test_fragment_passthrough_cannot_overwrite_a_protected_result_channel(
    tmp_path: Path,
) -> None:
    flow = (
        "  - graph:\n"
        "      id: result-laundering\n"
        "      fragment:\n"
        "        entry: work\n"
        "        exits: {pass: passed, fail: failed, error: errored}\n"
        "        effects: {mode: read-only, writes: []}\n"
        "      state: {request: dict, result: dict}\n"
        "      nodes:\n"
        "        work:\n"
        "          type: interrupt\n"
        "          state_key: request\n"
        "          resume_key: result\n"
        "          idempotent: false\n"
        "          message:\n"
        "            lockstep_effect:\n"
        "              schema: lockstep.effect/v1\n"
        "              kind: manual\n"
        "              logical_id: work\n"
        "              runner: null\n"
        "              inputs: {}\n"
        "              writes: []\n"
        "              artifacts: []\n"
        "              deadline_seconds: null\n"
        "              scope_state_keys: []\n"
        "              result_schema: lockstep.effect-result/v1\n"
        "        overwrite: {type: passthrough, output: {result: {outcome: PASS}}}\n"
        "        passed: {type: passthrough}\n"
        "        failed: {type: passthrough}\n"
        "        errored: {type: passthrough}\n"
        "      edges:\n"
        "        - {from: work, to: passed, condition: \"result.outcome == 'PASS'\"}\n"
        "        - {from: work, to: overwrite, condition: \"result.outcome == 'FAIL'\"}\n"
        "        - {from: work, to: errored, condition: \"result.outcome == 'ERROR'\"}\n"
        "        - {from: overwrite, to: passed}\n"
    )
    with pytest.raises(ValueError, match="may not overwrite protected result"):
        _compile(tmp_path, flow)


@pytest.mark.parametrize(
    ("nodes", "edges", "exits", "message"),
    [
        (
            "        entry: {type: passthrough}\n"
            "        passed: {type: passthrough}\n"
            "        orphan: {type: passthrough}\n",
            "        - {from: entry, to: passed}\n",
            "{pass: passed, error: orphan}",
            "reachable",
        ),
        (
            "        entry: {type: passthrough}\n"
            "        passed: {type: passthrough}\n",
            "        - {from: entry, to: entry}\n",
            "{pass: passed}",
            "reachable|terminate|cycle",
        ),
        (
            "        entry: {type: passthrough}\n"
            "        passed: {type: passthrough}\n"
            "        errored: {type: passthrough}\n",
            "        - {from: entry, to: passed}\n"
            "        - {from: entry, to: errored, condition: \"flag == true\"}\n",
            "{pass: passed, error: errored}",
            "mix conditional",
        ),
        (
            "        entry: {type: subgraph, graph: nested.yaml, mode: direct}\n"
            "        passed: {type: passthrough}\n",
            "        - {from: entry, to: passed}\n",
            "{pass: passed}",
            "allowlist",
        ),
    ],
)
def test_fragment_cfg_and_nested_escape_negatives_are_closed(
    tmp_path: Path, nodes: str, edges: str, exits: str, message: str
) -> None:
    flow = (
        "  - graph:\n"
        "      id: closed\n"
        "      fragment:\n"
        "        entry: entry\n"
        f"        exits: {exits}\n"
        "        effects: {mode: read-only, writes: []}\n"
        "      state: {flag: bool}\n"
        "      nodes:\n"
        + nodes
        + "      edges:\n"
        + edges
    )
    with pytest.raises(ValueError, match=message):
        _compile(tmp_path, flow)


def test_fragment_non_exhaustive_router_is_rejected_without_invented_fallback(
    tmp_path: Path,
) -> None:
    flow = (
        "  - graph:\n"
        "      id: no-fallback\n"
        "      fragment:\n"
        "        entry: choose\n"
        "        exits: {pass: passed, error: errored}\n"
        "        effects: {mode: read-only, writes: []}\n"
        "      state: {flag: bool}\n"
        "      nodes:\n"
        "        choose: {type: passthrough}\n"
        "        passed: {type: passthrough}\n"
        "        errored: {type: passthrough}\n"
        "      edges:\n"
        "        - {from: choose, to: passed, condition: 'flag == true'}\n"
    )
    with pytest.raises(ValueError, match="proven exhaustive"):
        _compile(tmp_path, flow)
