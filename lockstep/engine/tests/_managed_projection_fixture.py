from __future__ import annotations

from pathlib import Path

import yaml

from lockstep.workflow.canonical import canonical_yaml
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


def managed_projection_compile(tmp_path: Path):
    child_source = tmp_path / "review-child.workflow.yaml"
    child_source.write_text(
        "workflow_version: '1'\n"
        "name: review-child\n"
        "description: exact managed projection child\n"
        "protect: ['**']\n"
        "flow:\n"
        "  - step: review\n"
        "    id: review\n"
        "    task: Review the implementation for correctness.\n"
        "    exit: Record findings and a final verdict.\n"
        "    writes: [review.md]\n"
        "    artifact:\n"
        "      handle: review\n"
        "      path: review.md\n"
        "      markdown:\n"
        "        sections: [Findings, Verdict]\n"
        "    retry: {limit: 2, exhausted: escalate}\n"
    )
    child_workflow = parse_workflow(load_workflow(child_source))
    child_compiled = compile_workflow(
        validate_semantics(child_workflow, ResolvedCatalog()), ResolvedCatalog()
    )
    # Artifact grammar has its own RED owner. Seed the already-defined runtime
    # declaration so managed projection fails only at its own boundary.
    child_document = yaml.safe_load(child_compiled.recipe_bytes)
    child_effect = next(
        node["message"]["lockstep_effect"]
        for node in child_document["nodes"].values()
        if node.get("message", {}).get("lockstep_effect", {}).get("logical_id")
        == "review"
    )
    child_effect["artifacts"] = [
        {
            "name": "review",
            "source_path": "review.md",
            "media_type": "text/markdown",
            "required": True,
        }
    ]
    child_bytes = canonical_yaml(child_document)
    child_file = CatalogFile.build("review-child.recipe.yaml", child_bytes)
    child_contract = ChildWorkflowContract(
        outcomes=("pass", "fail", "error"),
        exports={
            "review": ChildArtifactContract(
                handle="review",
                fixed_source="review.md",
                declared_name="review",
                media_type="text/markdown",
                producer_logical_id="review",
                producer_result_state_key="review_result",
            )
        },
    )
    catalog = ResolvedCatalog(
        children={
            "review-child": ResolvedChild(
                logical_name="review-child",
                contract=child_contract,
                source_definition_sha256=child_workflow.source_sha256,
                standalone=CanonicalCompiledBundle.build(
                    root_relative_path="review-child.recipe.yaml",
                    files=(child_file,),
                    compiler_version="1",
                ),
            )
        }
    )
    parent_source = tmp_path / "managed-parent.workflow.yaml"
    parent_source.write_text(
        "workflow_version: '1'\n"
        "name: managed-parent\n"
        "description: exact managed projection parent\n"
        "protect: ['**']\n"
        "flow:\n"
        "  - call:\n"
        "      id: review-call\n"
        "      workflow: review-child\n"
        "      runner: codex\n"
        "      timeout_minutes: 5\n"
        "      artifacts: {review: .lockstep/review.md}\n"
        "  - accept:\n"
        "      artifact_from: review-call.review\n"
        "      verdict: PASS\n"
    )
    parent_workflow = parse_workflow(load_workflow(parent_source))
    compiled = compile_workflow(
        validate_semantics(parent_workflow, catalog), catalog
    )
    return child_document, compiled
