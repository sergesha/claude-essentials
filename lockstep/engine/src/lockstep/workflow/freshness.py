"""Exact canonical-match verification for checked-in generated recipes."""

from __future__ import annotations

from typing import Mapping

from lockstep.recipe.profile import CompilerProvenance, _create_compiler_provenance
from lockstep.recipe.authority import canonical_execution_bytes

from .compiler import compile_workflow
from .semantics import ValidatedWorkflow, WorkflowCatalog


class FreshnessError(ValueError):
    pass


def verify_canonical_match(
    workflow: ValidatedWorkflow,
    catalog: WorkflowCatalog,
    candidate_recipe_bytes: bytes,
    *,
    candidate_generated_files: Mapping[str, bytes] | None = None,
    candidate_dependency_manifest_bytes: bytes | None = None,
) -> CompilerProvenance:
    if not isinstance(candidate_recipe_bytes, bytes):
        raise TypeError("candidate recipe must be bytes")
    compiled = compile_workflow(workflow, catalog)
    if not isinstance(candidate_dependency_manifest_bytes, bytes):
        raise FreshnessError("dependency manifest is required for canonical freshness")
    if candidate_dependency_manifest_bytes != compiled.dependency_manifest_bytes:
        raise FreshnessError(
            "dependency manifest is not a byte-for-byte canonical match"
        )
    if candidate_recipe_bytes != compiled.recipe_bytes:
        raise FreshnessError("generated recipe is not a byte-for-byte canonical match")
    candidates = dict(candidate_generated_files or {})
    expected = {
        item.relative_path: item.content for item in compiled.generated_files
    }
    if set(candidates) != set(expected):
        raise FreshnessError("generated file set is not a complete canonical match")
    for relative_path, expected_bytes in expected.items():
        candidate = candidates[relative_path]
        if not isinstance(candidate, bytes):
            raise TypeError("candidate generated file content must be bytes")
        if candidate != expected_bytes:
            raise FreshnessError(
                f"generated file {relative_path!r} is not a byte-for-byte canonical match"
            )
    return _create_compiler_provenance(
        candidate_recipe_bytes,
        context="canonical-match",
        root_relative_path=compiled.root_relative_path,
        generated_files=candidates,
        execution_recipe_bytes=canonical_execution_bytes(
            candidate_recipe_bytes, logical_path=compiled.root_relative_path
        ),
        execution_generated_files={
            path: canonical_execution_bytes(content, logical_path=path)
            for path, content in candidates.items()
        },
        source_bundle_sha256=compiled.bundle_sha256,
    )
