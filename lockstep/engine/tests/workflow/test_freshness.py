from __future__ import annotations

from pathlib import Path

import pytest

from lockstep.workflow.compiler import compile_workflow
from lockstep.workflow.freshness import FreshnessError, verify_canonical_match
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.ir import FragmentIR
from lockstep.workflow.semantics import (
    InMemoryWorkflowCatalog,
    ResolvedCatalog,
    ResolvedFragment,
    validate_semantics,
)
from lockstep.recipe.profile import check_recipe_full
from lockstep.recipe.authority import StrictRecipeIngress


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
    validated = validate_semantics(workflow, catalog)
    compiled = compile_workflow(validated, catalog)

    provenance = verify_canonical_match(
        validated,
        catalog,
        compiled.recipe_bytes,
        candidate_dependency_manifest_bytes=compiled.dependency_manifest_bytes,
    )

    assert provenance.context == "canonical-match"
    assert provenance.recipe_sha256 == provenance.files[0].sha256
    assert provenance.source_bundle_sha256 == compiled.bundle_sha256


def test_valid_yaml_edit_with_unchanged_generated_marker_is_stale(tmp_path: Path) -> None:
    source = _source(tmp_path)
    workflow = parse_workflow(load_workflow(source))
    catalog = InMemoryWorkflowCatalog({})
    validated = validate_semantics(workflow, catalog)
    compiled = compile_workflow(validated, catalog)
    edited = compiled.recipe_bytes.replace(b"description: fresh", b"description: edited")
    assert edited != compiled.recipe_bytes

    with pytest.raises(FreshnessError, match="byte-for-byte"):
        verify_canonical_match(
            validated,
            catalog,
            edited,
            candidate_dependency_manifest_bytes=compiled.dependency_manifest_bytes,
        )


def test_canonical_provenance_is_exact_byte_bound_at_profile_boundary(tmp_path: Path) -> None:
    source = _source(tmp_path)
    catalog = InMemoryWorkflowCatalog({})
    validated = validate_semantics(parse_workflow(load_workflow(source)), catalog)
    compiled = compile_workflow(validated, catalog)
    provenance = verify_canonical_match(
        validated,
        catalog,
        compiled.recipe_bytes,
        candidate_dependency_manifest_bytes=compiled.dependency_manifest_bytes,
    )
    recipe = tmp_path / "fresh.recipe.yaml"
    recipe.write_bytes(compiled.recipe_bytes)
    canonical = StrictRecipeIngress(tmp_path).inspect(recipe.name).files[0].bytes
    recipe.write_bytes(canonical)

    errors, _warnings = check_recipe_full(recipe, provenance=provenance)
    assert errors == []

    recipe.write_bytes(compiled.recipe_bytes.replace(b"description: fresh", b"description: stale"))
    stale = StrictRecipeIngress(tmp_path).inspect(recipe.name).files[0].bytes
    recipe.write_bytes(stale)
    errors, _warnings = check_recipe_full(recipe, provenance=provenance)
    assert any("provenance" in error and "bytes" in error for error in errors)

    other_source = tmp_path / "other.workflow.yaml"
    other_source.write_text(source.read_text().replace("name: fresh", "name: other"))
    other_validated = validate_semantics(
        parse_workflow(load_workflow(other_source)), catalog
    )
    other = compile_workflow(other_validated, catalog)
    recipe.write_bytes(other.recipe_bytes)
    other_canonical = StrictRecipeIngress(tmp_path).inspect(recipe.name).files[0].bytes
    recipe.write_bytes(other_canonical)
    errors, _warnings = check_recipe_full(recipe, provenance=provenance)
    assert any("provenance" in error and "bytes" in error for error in errors)


def test_freshness_binds_fragment_source_digest_even_when_expansion_is_unchanged(
    tmp_path: Path,
) -> None:
    source = tmp_path / "include.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: include\ndescription: include\n"
        "protect: ['**']\nflow:\n"
        "  - include_graph: {id: inspect, path: fragments/inspect.yaml}\n"
    )
    fragment = FragmentIR.parse({
        "fragment": {
            "entry": "done",
            "exits": {"pass": "done"},
            "effects": {"mode": "read-only", "writes": []},
        },
        "nodes": {"done": {"type": "passthrough"}},
        "edges": [],
    })

    def catalog(source_digest: str) -> ResolvedCatalog:
        return ResolvedCatalog(fragments={
            "fragments/inspect.yaml": ResolvedFragment(
                "fragments/inspect.yaml", source_digest, fragment
            )
        })

    workflow = parse_workflow(load_workflow(source))
    old_catalog = catalog("1" * 64)
    old_validated = validate_semantics(workflow, old_catalog)
    old = compile_workflow(old_validated, old_catalog)
    new_catalog = catalog("2" * 64)
    new_validated = validate_semantics(workflow, new_catalog)
    new = compile_workflow(new_validated, new_catalog)
    assert old.recipe_bytes == new.recipe_bytes
    assert old.dependency_manifest_bytes != new.dependency_manifest_bytes

    with pytest.raises(FreshnessError, match="dependency manifest"):
        verify_canonical_match(
            new_validated,
            new_catalog,
            old.recipe_bytes,
            candidate_dependency_manifest_bytes=old.dependency_manifest_bytes,
        )
