"""Public pure compiler for validated Workflow DSL input."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Literal

from lockstep.recipe.profile import _create_compiler_provenance, check_recipe_bytes

from .canonical import canonical_json, canonical_yaml
from .lowering import lower_workflow
from .semantics import ValidatedWorkflow, WorkflowCatalog


@dataclass(frozen=True)
class DependencyEntry:
    kind: Literal["workflow", "fragment"]
    logical_name: str
    definition_sha256: str
    compiled_sha256: str


@dataclass(frozen=True)
class DependencyManifest:
    schema: Literal["lockstep.workflow-dependencies/v1"]
    compiler_version: Literal["1"]
    root_name: str
    root_source_sha256: str
    entries: tuple[DependencyEntry, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "compiler_version": self.compiler_version,
            "root": {
                "name": self.root_name,
                "source_sha256": self.root_source_sha256,
            },
            "dependencies": [
                {
                    "kind": item.kind,
                    "logical_name": item.logical_name,
                    "definition_sha256": item.definition_sha256,
                    "compiled_sha256": item.compiled_sha256,
                }
                for item in self.entries
            ],
        }


@dataclass(frozen=True)
class CompilationResult:
    recipe_bytes: bytes
    source_map_bytes: bytes
    dependency_manifest_bytes: bytes
    dependency_manifest: DependencyManifest
    digest: str


def compile_workflow(
    workflow: ValidatedWorkflow, catalog: WorkflowCatalog
) -> CompilationResult:
    del catalog  # Task 9 supplies resolved child/fragment digest entries.
    if not isinstance(workflow, ValidatedWorkflow):
        raise TypeError("compile_workflow requires ValidatedWorkflow")
    document, source_map = lower_workflow(workflow)
    recipe_bytes = canonical_yaml(document)
    proof = _create_compiler_provenance(recipe_bytes, context="compiler-output")
    profile_errors, _warnings = check_recipe_bytes(recipe_bytes, proof)
    if profile_errors:
        raise ValueError(
            "compiler produced an invalid Lockstep recipe: "
            + "; ".join(profile_errors)
        )
    source_map_bytes = canonical_json(source_map)
    manifest = DependencyManifest(
        "lockstep.workflow-dependencies/v1",
        "1",
        workflow.workflow.name,
        workflow.workflow.source_sha256,
        (),
    )
    dependency_bytes = canonical_json(manifest.to_dict())
    return CompilationResult(
        recipe_bytes,
        source_map_bytes,
        dependency_bytes,
        manifest,
        hashlib.sha256(recipe_bytes).hexdigest(),
    )
