from __future__ import annotations

from pathlib import Path

import yaml

from lockstep.runtime.effects.descriptors import parse_effect_descriptor
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
        "pinned",
    }
    assert document["loop_limits"]
    assert set(document["loop_limits"]) == set(document["loop_exits"])
    for target in document["loop_exits"].values():
        assert document["nodes"][target]["type"] == "passthrough"
        assert target not in interrupts
    verify = next(
        node
        for node in interrupts.values()
        if node["message"]["lockstep_effect"]["kind"] == "pinned"
    )
    descriptor = parse_effect_descriptor(verify["message"]["lockstep_effect"])
    assert descriptor.deadline_seconds == 30
    assert descriptor.runner is not None
    assert descriptor.runner.selector == "pinned"
    command_key = dict(descriptor.inputs)["command"].state_key
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


def test_generated_loop_exit_may_not_target_a_protected_interrupt_directly(
    tmp_path: Path,
) -> None:
    from lockstep.recipe.profile import CompilerProvenance, check_recipe_full

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
    provenance = CompilerProvenance.for_bytes("compiler-output", recipe.read_bytes())

    errors, _warnings = check_recipe_full(recipe, provenance=provenance)

    assert any("loop_exits" in error and "interrupt" in error for error in errors)
