"""Pure Workflow-DSL authoring and checked-in source classification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from lockstep.authoring_bundle import AuthoredRecipe, _workflow_project_and_source, canonical_recipe_bytes_for_children
from lockstep.authoring_compilation import (
    compile_project, compile_captured_source, validate_logical_name, workflow_call_names,
)
from lockstep.authoring_capture import capture_optional_regular_file
from lockstep.authoring_installation import (
    CapturedWorkflowSource,
    installation_collision,
    plan_captured_workflow_installation,
)
from lockstep.authoring_results import CanonicalObservation, canonical_observation, diff_planned_compilation
from lockstep.errors import AuthoringError
from lockstep.recipe.authority import RecipeLimits, decode_recipe_document
from lockstep.recipe.profile import CompilerProvenance
from lockstep.workflow.compiler import CompilationResult
from lockstep.workflow.estimate import estimate_manual_recipe, estimate_workflow
from lockstep.workflow.schema import load_workflow
from lockstep.workflow.semantics import ResolvedCatalog, ValidatedWorkflow

if TYPE_CHECKING:
    from lockstep.authoring_publisher import AuthoringPublisher


def project_paths(project: Path, name: str) -> AuthoredRecipe:
    validate_logical_name(name)
    root = Path(project).resolve()
    workflow = root / ".lockstep" / "workflows" / f"{name}.workflow.yaml"
    recipe = root / ".lockstep" / "recipes" / f"{name}.recipe.yaml"
    if workflow.is_file():
        return AuthoredRecipe(name, "workflow", workflow, recipe,
            recipe.with_name(f"{name}.dependencies.json"),
            recipe.with_name(f"{name}.source-map.json"))
    if recipe.is_file():
        return AuthoredRecipe(name, "manual", None, recipe, None, None)
    raise AuthoringError(f"recipe {name!r} has no workflow source or manual yamlgraph file")


def compile_source(source: Path, *, children: Mapping[str, tuple[ValidatedWorkflow, CompilationResult]] | None = None,
) -> tuple[ValidatedWorkflow, ResolvedCatalog, CompilationResult]:
    return compile_captured_source(load_workflow(source), children=children)


def compile_project_source(source: Path) -> tuple[ValidatedWorkflow, ResolvedCatalog, CompilationResult]:
    path = Path(source).resolve()
    suffix = ".workflow.yaml"
    if not path.name.endswith(suffix):
        raise AuthoringError("workflow source is outside the canonical project layout")
    recipe = project_paths(path.parent.parent.parent, path.name.removesuffix(suffix))
    _project, canonical_source = _workflow_project_and_source(recipe)
    if canonical_source != path:
        raise AuthoringError("workflow source is outside the canonical project layout")
    compilation = compile_project(recipe)
    return compilation.root_validated, compilation.root_catalog, compilation.root_result


def link_recipe_dependencies(recipe_bytes: bytes, children: tuple[str, ...]) -> bytes:
    return canonical_recipe_bytes_for_children(recipe_bytes, children)


def canonical_recipe_bytes(source: Path, compiled: CompilationResult) -> bytes:
    return canonical_recipe_bytes_for_children(
        compiled.recipe_bytes, workflow_call_names(load_workflow(source))
    )


def canonical_match(recipe: AuthoredRecipe) -> CompilerProvenance:
    if recipe.kind != "workflow" or recipe.workflow_path is None:
        raise AuthoringError(f"manual yamlgraph recipe {recipe.name!r} has no canonical source")
    return canonical_observation(compile_project(recipe)).proof


def classify_generated_recipe_observation(
    recipes_dir: Path, name: str, recipe_path: Path
) -> CanonicalObservation | None:
    recipe = _classify_generated_recipe_path(recipes_dir, name, recipe_path)
    if recipe is None:
        return None
    try:
        return canonical_observation(compile_project(recipe))
    except (OSError, ValueError) as exc:
        raise AuthoringError(
            f"generated recipe failed canonical match: {exc}"
        ) from exc


def classify_generated_recipe(
    recipes_dir: Path, name: str, recipe_path: Path
) -> CompilerProvenance | None:
    observation = classify_generated_recipe_observation(recipes_dir, name, recipe_path)
    return None if observation is None else observation.proof


def _classify_generated_recipe_path(
    recipes_dir: Path, name: str, recipe_path: Path
) -> AuthoredRecipe | None:
    limits = RecipeLimits()
    captured = capture_optional_regular_file(
        recipe_path, max_bytes=limits.max_file_bytes, label="recipe source")
    if captured is None:
        raise AuthoringError(f"recipe {name!r} is missing")
    raw, _identity = captured
    document = decode_recipe_document(raw, logical=recipe_path.name, limits=limits)
    marker = document.get("x-lockstep-generated") if isinstance(document, dict) else None
    if marker is None:
        return None
    if not isinstance(marker, dict):
        raise AuthoringError("generated recipe marker must be a mapping")
    expected_source = f"../workflows/{name}.workflow.yaml"
    if marker.get("source") != expected_source:
        raise AuthoringError("generated recipe declares a non-canonical workflow source")
    root = Path(recipes_dir).resolve().parent.parent
    recipe = project_paths(root, name)
    if recipe.kind != "workflow" or recipe.workflow_path is None:
        raise AuthoringError("generated recipe source is missing")
    if recipe.recipe_path.resolve() != recipe_path.resolve():
        raise AuthoringError("generated recipe is outside the canonical project layout")
    return recipe


def _require_workflow_compilation(recipe: AuthoredRecipe) -> Path:
    if recipe.kind != "workflow" or recipe.workflow_path is None:
        raise AuthoringError(f"manual yamlgraph recipe {recipe.name!r} has no generated output to compile")
    return recipe.workflow_path


def _publish_planned_compilation(publisher: AuthoringPublisher, recipe: AuthoredRecipe) -> CompilationResult:
    compilation = compile_project(recipe)
    publisher.publish(compilation.plan)
    return compilation.root_result


def write_compilation(recipe: AuthoredRecipe, *, state_dir: Path) -> CompilationResult:
    project, _source = _workflow_project_and_source(recipe)
    from lockstep.authoring_publisher import AuthoringPublisher

    publisher = AuthoringPublisher(state_dir)
    publisher.require_ready(project)
    return _publish_planned_compilation(publisher, recipe)


def publish_project_compilation(project: Path, name: str, *, state_dir: Path) -> CompilationResult:
    validate_logical_name(name)
    root = Path(project).resolve()
    from lockstep.authoring_publisher import AuthoringPublisher

    publisher = AuthoringPublisher(state_dir)
    publisher.require_ready(root)
    recipe = project_paths(root, name)
    _require_workflow_compilation(recipe)
    return _publish_planned_compilation(publisher, recipe)


def check_recipe(project: Path, name: str) -> dict[str, object]:
    recipe = project_paths(project, name)
    if recipe.kind == "manual":
        from lockstep.recipe import profile

        errors, warnings = profile.check_recipe_full(recipe.recipe_path)
        return {"ok": not errors, "kind": "manual", "errors": errors, "warnings": warnings}
    proof = canonical_match(recipe)
    return {
        "ok": True,
        "kind": "workflow",
        "canonical_match": proof.context,
        "source_bundle_sha256": proof.source_bundle_sha256,
    }


def check_recovered_recipe(project: Path, name: str, *, state_dir: Path) -> dict[str, object]:
    validate_logical_name(name)
    root = Path(project).resolve()
    from lockstep.authoring_publisher import observe_authoring_project

    return observe_authoring_project(state_dir, root, lambda: check_recipe(root, name))


def check_all_recovered_recipes(project: Path, *, state_dir: Path) -> tuple[tuple[str, dict[str, object]], ...]:
    root = Path(project).resolve()
    from lockstep.authoring_publisher import observe_authoring_project

    def observe() -> tuple[tuple[str, dict[str, object]], ...]:
        names = tuple(sorted(path.name.removesuffix(".recipe.yaml")
            for path in (root / ".lockstep" / "recipes").glob("*.recipe.yaml")))
        if not names:
            raise AuthoringError("no recipes found")
        return tuple((name, check_recipe(root, name)) for name in names)

    return observe_authoring_project(state_dir, root, observe)


def diff_recipe(project: Path, name: str) -> str:
    recipe = project_paths(project, name)
    if recipe.kind == "manual" or recipe.workflow_path is None:
        return ""
    return diff_planned_compilation(compile_project(recipe))


def diff_recovered_recipe(project: Path, name: str, *, state_dir: Path) -> str:
    validate_logical_name(name)
    root = Path(project).resolve()
    from lockstep.authoring_publisher import observe_authoring_project

    return observe_authoring_project(state_dir, root, lambda: diff_recipe(root, name))


def render_recipe(project: Path, name: str, view: str) -> str:
    recipe = project_paths(project, name)
    if view == "workflow":
        if recipe.workflow_path is None:
            raise AuthoringError(
                f"manual yamlgraph recipe {name!r} has no Workflow DSL view"
            )
        return recipe.workflow_path.read_text()
    if view != "generated":
        raise AuthoringError("recipe render view must be workflow or generated")
    return recipe.recipe_path.read_text()


def estimate_recipe(project: Path, name: str) -> dict[str, object]:
    recipe = project_paths(project, name)
    if recipe.kind == "manual":
        return estimate_manual_recipe(recipe.recipe_path).to_dict()
    compilation = compile_project(recipe)
    return estimate_workflow(
        compilation.root_validated.workflow, compilation.root_catalog
    ).to_dict()


def _minimal_workflow_source(name: str) -> bytes:
    return (
        "workflow_version: '1'\n"
        f"name: {name}\n"
        "description: Native durable workflow\n"
        "protect: ['**']\n"
        "flow:\n"
        "  - escalate: {}\n"
    ).encode()


def initialize_minimal(project: Path, name: str, *, state_dir: Path) -> AuthoredRecipe:
    validate_logical_name(name)
    root = Path(project).resolve()
    from lockstep.authoring_publisher import AuthoringPublisher

    publisher = AuthoringPublisher(state_dir)
    publisher.require_ready(root)
    planned = plan_captured_workflow_installation(root,
        (CapturedWorkflowSource(name, _minimal_workflow_source(name)),), root_role=name)
    collision = installation_collision(planned.plan)
    if collision is not None:
        relative = collision.path.relative_to(root)
        raise AuthoringError(f"destination already exists: {relative}")
    publisher.publish(planned.plan)
    return project_paths(root, name)


def json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n"
