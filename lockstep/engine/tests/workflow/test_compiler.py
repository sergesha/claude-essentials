from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.effects.models import EffectDescriptor
from lockstep.workflow.compiler import compile_workflow
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import InMemoryWorkflowCatalog


def _parse(tmp_path: Path, flow: str):
    source = tmp_path / "review.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\n"
        "name: review\n"
        "description: deterministic review\n"
        "protect: ['**']\n"
        f"flow:\n{flow}"
    )
    return parse_workflow(load_workflow(source))


def test_compile_is_byte_identical_and_binds_the_exact_source(tmp_path: Path) -> None:
    workflow = _parse(
        tmp_path,
        "  - step: edit\n"
        "    task: Make the requested change\n"
        "    exit: Tests demonstrate the change\n"
        "    writes: [src/]\n",
    )
    catalog = InMemoryWorkflowCatalog({})

    first = compile_workflow(workflow, catalog)
    second = compile_workflow(workflow, catalog)

    assert first == second
    assert first.digest == hashlib.sha256(first.recipe_bytes).hexdigest()
    assert first.recipe_bytes.endswith(b"\n")
    assert first.source_map_bytes.endswith(b"\n")
    document = yaml.safe_load(first.recipe_bytes)
    assert document["x-lockstep-generated"] == {
        "schema": "lockstep.generated/v1",
        "compiler_version": "1",
        "workflow_version": "1",
        "source": "../workflows/review.workflow.yaml",
        "source_sha256": workflow.source_sha256,
    }
    source_map = yaml.safe_load(first.source_map_bytes)
    assert set(source_map) == {"schema", "compiler_version", "source", "nodes"}
    assert any(item["pointer"] == "/flow/0" for item in source_map["nodes"].values())


def test_step_lowers_to_an_exact_native_manual_effect_and_graph_terminal(
    tmp_path: Path,
) -> None:
    workflow = _parse(
        tmp_path,
        "  - step: edit\n"
        "    task: Make the requested change\n"
        "    exit: Tests demonstrate the change\n"
        "    writes: [src/, README.md]\n",
    )

    document = yaml.safe_load(
        compile_workflow(workflow, InMemoryWorkflowCatalog({})).recipe_bytes
    )

    interrupts = {
        name: node
        for name, node in document["nodes"].items()
        if node["type"] == "interrupt"
    }
    assert len(interrupts) == 1
    node = next(iter(interrupts.values()))
    assert set(node) == {"type", "message", "state_key", "resume_key", "idempotent"}
    assert set(node["message"]) == {"lockstep_effect", "step", "task", "exit"}
    parsed = parse_effect_descriptor(node["message"]["lockstep_effect"])
    assert isinstance(parsed, EffectDescriptor)
    assert parsed.kind == "manual"
    assert parsed.logical_id == "edit"
    assert parsed.runner is None
    assert parsed.writes == ("src/", "README.md")
    assert parsed.deadline_seconds is None
    assert parsed.scope_state_keys == ()
    assert document["state"][node["resume_key"]] == "dict"
    assert document["state"]["lockstep_outcome"] == "str"
    assert any(
        n.get("type") == "passthrough"
        and n.get("output") == {"lockstep_outcome": "PASS"}
        for n in document["nodes"].values()
    )
    assert {n["type"] for n in document["nodes"].values()} <= {
        "interrupt",
        "passthrough",
    }


def test_source_bytes_not_only_parsed_values_participate_in_freshness(
    tmp_path: Path,
) -> None:
    first = _parse(
        tmp_path,
        "  - escalate: {}\n",
    )
    first_result = compile_workflow(first, InMemoryWorkflowCatalog({}))
    source = tmp_path / "review.workflow.yaml"
    source.write_text(source.read_text().replace("description:", "description:  "))
    second = parse_workflow(load_workflow(source))

    second_result = compile_workflow(second, InMemoryWorkflowCatalog({}))

    assert first.source_sha256 != second.source_sha256
    assert first_result.recipe_bytes != second_result.recipe_bytes
    assert first_result.digest != second_result.digest
