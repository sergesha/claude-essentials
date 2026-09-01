"""Real compiled managed-child artifact fixture shared by restart tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lockstep.workflow.compiler import CompilationResult, compile_workflow
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


@dataclass(frozen=True, slots=True)
class NativeChildArtifactFixture:
    """Materialized executable bundle for one managed child artifact flow."""

    recipes_dir: Path
    root_recipe: Path
    compilation: CompilationResult


def materialize_managed_child_artifact(
    tmp_path: Path,
) -> NativeChildArtifactFixture:
    """Compile and materialize the real child producer plus parent acceptance."""

    child_bytes = (
        b"version: '1.0'\nname: child\n"
        b"state: {review_result: dict, lockstep_outcome: str}\n"
        b"nodes:\n  review:\n    type: interrupt\n"
        b"    state_key: review_request\n    resume_key: review_result\n"
        b"    idempotent: false\n    message:\n"
        b"      lockstep_effect:\n        schema: lockstep.effect/v1\n"
        b"        kind: managed\n        logical_id: review\n"
        b"        runner:\n          selector: codex\n"
        b"          required_capabilities: [workspace, bounded_result]\n"
        b"        inputs: {}\n        writes: [review.md]\n"
        b"        artifacts:\n"
        b"          - {name: review, source_path: review.md, media_type: text/markdown, required: true}\n"
        b"        deadline_seconds: 300\n        scope_state_keys: []\n"
        b"        result_schema: lockstep.effect-result/v1\n"
        b"  pass: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        b"edges: [{from: START, to: review}, {from: review, to: pass}, "
        b"{from: pass, to: END}]\n"
    )
    child_file = CatalogFile.build("child.recipe.yaml", child_bytes)
    catalog = ResolvedCatalog(
        children={
            "child": ResolvedChild(
                "child",
                ChildWorkflowContract(
                    ("pass", "fail", "error"),
                    exports={
                        "review": ChildArtifactContract(
                            "review",
                            "review.md",
                            "review",
                            "text/markdown",
                            "review",
                            "review_result",
                        )
                    },
                ),
                "6" * 64,
                CanonicalCompiledBundle.build(
                    root_relative_path="child.recipe.yaml",
                    files=(child_file,),
                    compiler_version="1",
                ),
            )
        }
    )
    source = tmp_path / "artifact-parent.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: artifact-parent\n"
        "description: artifact child restart\nprotect: ['**']\nflow:\n"
        "  - call:\n      id: review-call\n      workflow: child\n"
        "      runner: codex\n"
        "      artifacts: {review: .lockstep/review.md}\n"
        "  - accept:\n"
        "      artifact_from: review-call.review\n"
        "      verdict: PASS\n"
    )
    workflow = parse_workflow(load_workflow(source))
    compilation = compile_workflow(validate_semantics(workflow, catalog), catalog)
    recipes_dir = tmp_path / ".lockstep" / "recipes"
    for relative_path, content in compilation.executable_files.items():
        target = recipes_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return NativeChildArtifactFixture(
        recipes_dir=recipes_dir,
        root_recipe=recipes_dir / compilation.root_relative_path,
        compilation=compilation,
    )
