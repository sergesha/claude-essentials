"""Exact canonical-match verification for checked-in generated recipes."""

from __future__ import annotations

from lockstep.recipe.profile import CompilerProvenance, _create_compiler_provenance

from .compiler import compile_workflow
from .semantics import ValidatedWorkflow, WorkflowCatalog


class FreshnessError(ValueError):
    pass


def verify_canonical_match(
    workflow: ValidatedWorkflow,
    catalog: WorkflowCatalog,
    candidate_recipe_bytes: bytes,
) -> CompilerProvenance:
    if not isinstance(candidate_recipe_bytes, bytes):
        raise TypeError("candidate recipe must be bytes")
    compiled = compile_workflow(workflow, catalog)
    if candidate_recipe_bytes != compiled.recipe_bytes:
        raise FreshnessError("generated recipe is not a byte-for-byte canonical match")
    return _create_compiler_provenance(
        candidate_recipe_bytes, context="canonical-match"
    )
