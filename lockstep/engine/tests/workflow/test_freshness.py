from __future__ import annotations

from pathlib import Path

import pytest

from lockstep.workflow.compiler import compile_workflow
from lockstep.workflow.freshness import FreshnessError, verify_canonical_match
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import InMemoryWorkflowCatalog


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "fresh.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\n"
        "name: fresh\n"
        "description: fresh\n"
        "protect: ['**']\n"
        "flow:\n"
        "  - escalate: {}\n"
    )
    return source


def test_canonical_match_is_bound_to_the_complete_compiled_bytes(tmp_path: Path) -> None:
    source = _source(tmp_path)
    workflow = parse_workflow(load_workflow(source))
    catalog = InMemoryWorkflowCatalog({})
    compiled = compile_workflow(workflow, catalog)

    provenance = verify_canonical_match(workflow, catalog, compiled.recipe_bytes)

    assert provenance.context == "canonical-match"
    assert provenance.recipe_sha256 == compiled.digest


def test_valid_yaml_edit_with_unchanged_generated_marker_is_stale(tmp_path: Path) -> None:
    source = _source(tmp_path)
    workflow = parse_workflow(load_workflow(source))
    catalog = InMemoryWorkflowCatalog({})
    compiled = compile_workflow(workflow, catalog)
    edited = compiled.recipe_bytes.replace(b"description: fresh", b"description: edited")
    assert edited != compiled.recipe_bytes

    with pytest.raises(FreshnessError, match="byte-for-byte"):
        verify_canonical_match(workflow, catalog, edited)
