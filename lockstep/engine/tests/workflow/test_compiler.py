from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.effects.models import EffectDescriptor
from lockstep.workflow.compiler import compile_workflow
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import InMemoryWorkflowCatalog, validate_semantics


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

    validated = validate_semantics(workflow, catalog)
    first = compile_workflow(validated, catalog)
    second = compile_workflow(validated, catalog)

    assert first == second
    assert first.digest == hashlib.sha256(first.recipe_bytes).hexdigest()
    assert hashlib.sha256(first.source_map_bytes).hexdigest()
    assert hashlib.sha256(first.dependency_manifest_bytes).hexdigest()
    assert first.recipe_bytes.endswith(b"\n")
    assert first.source_map_bytes.endswith(b"\n")
    manifest = json.loads(first.dependency_manifest_bytes)
    assert manifest == {
        "schema": "lockstep.workflow-dependencies/v1",
        "compiler_version": "1",
        "root": {"name": "review", "source_sha256": workflow.source_sha256},
        "dependencies": [],
    }
    assert first.dependency_manifest.root_name == "review"
    assert first.dependency_manifest.root_source_sha256 == workflow.source_sha256
    assert first.dependency_manifest.entries == ()
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
    assert all(set(item) == {"pointer", "line", "column"} for item in source_map["nodes"].values())


def test_control_flow_compilation_matches_checked_in_byte_exact_goldens() -> None:
    golden = Path(__file__).with_name("golden")
    source = golden / "control-flow.workflow.yaml"
    catalog = InMemoryWorkflowCatalog({})

    result = compile_workflow(
        validate_semantics(parse_workflow(load_workflow(source)), catalog), catalog
    )

    assert result.recipe_bytes == (golden / "control-flow.recipe.yaml").read_bytes()
    assert result.source_map_bytes == (
        golden / "control-flow.source-map.json"
    ).read_bytes()
    assert result.dependency_manifest_bytes == (
        golden / "control-flow.dependencies.json"
    ).read_bytes()


def test_stable_node_ids_ignore_nonsemantic_source_formatting(tmp_path: Path) -> None:
    workflow = _parse(tmp_path, "  - escalate: {}\n")
    catalog = InMemoryWorkflowCatalog({})
    first = compile_workflow(validate_semantics(workflow, catalog), catalog)
    source = tmp_path / "review.workflow.yaml"
    source.write_text(source.read_text().replace("  - escalate", "  -  escalate"))
    reformatted = parse_workflow(load_workflow(source))
    second = compile_workflow(validate_semantics(reformatted, catalog), catalog)

    first_nodes = yaml.safe_load(first.source_map_bytes)["nodes"]
    second_nodes = yaml.safe_load(second.source_map_bytes)["nodes"]
    assert set(first_nodes) == set(second_nodes)
    assert first.recipe_bytes != second.recipe_bytes  # freshness still binds raw source


def test_parse_binds_the_same_source_bytes_that_were_loaded(tmp_path: Path) -> None:
    source = tmp_path / "review.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: review\ndescription: first\n"
        "protect: ['**']\nflow: [{escalate: {}}]\n"
    )
    loaded = load_workflow(source)
    original_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    source.write_text(source.read_text().replace("first", "second"))

    parsed = parse_workflow(loaded)

    assert parsed.description == "first"
    assert parsed.source_sha256 == original_digest
    assert parsed.source_sha256 != hashlib.sha256(source.read_bytes()).hexdigest()


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
        compile_workflow(
            validate_semantics(workflow, InMemoryWorkflowCatalog({})),
            InMemoryWorkflowCatalog({}),
        ).recipe_bytes
    )

    interrupts = {
        name: node
        for name, node in document["nodes"].items()
        if node["type"] == "interrupt"
    }
    assert len(interrupts) == 1
    node = next(iter(interrupts.values()))
    assert set(node) == {"type", "message", "state_key", "resume_key", "idempotent"}
    assert set(node["message"]) == {
        "lockstep_effect",
        "step",
        "task",
        "exit_criterion",
        "evidence_schema",
        "artifact_contract",
    }
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
    catalog = InMemoryWorkflowCatalog({})
    first_result = compile_workflow(validate_semantics(first, catalog), catalog)
    source = tmp_path / "review.workflow.yaml"
    source.write_text(source.read_text().replace("description:", "description:  "))
    second = parse_workflow(load_workflow(source))

    second_result = compile_workflow(validate_semantics(second, catalog), catalog)

    assert first.source_sha256 != second.source_sha256
    assert first_result.recipe_bytes != second_result.recipe_bytes
    assert first_result.digest != second_result.digest
