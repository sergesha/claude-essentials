"""Pure Workflow-DSL authoring and checked-in source classification."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Mapping

import yaml

from lockstep.authoring_bundle import AuthoredRecipe, canonical_recipe_bytes_for_children
from lockstep.authoring_compilation import (
    compile_captured_source,
    validate_logical_name,
    workflow_call_names,
)
from lockstep.errors import AuthoringError
from lockstep.recipe.authority import StrictRecipeIngress, canonical_execution_bytes
from lockstep.recipe.profile import CompilerProvenance, _create_compiler_provenance
from lockstep.workflow.compiler import CompilationResult
from lockstep.workflow.estimate import estimate_manual_recipe, estimate_workflow
from lockstep.workflow.freshness import verify_canonical_match
from lockstep.workflow.schema import load_workflow
from lockstep.workflow.semantics import ResolvedCatalog, ValidatedWorkflow


def project_paths(project: Path, name: str) -> AuthoredRecipe:
    validate_logical_name(name)
    root = Path(project).resolve()
    workflow = root / ".lockstep" / "workflows" / f"{name}.workflow.yaml"
    recipe = root / ".lockstep" / "recipes" / f"{name}.recipe.yaml"
    if workflow.is_file():
        return AuthoredRecipe(
            name,
            "workflow",
            workflow,
            recipe,
            recipe.with_name(f"{name}.dependencies.json"),
            recipe.with_name(f"{name}.source-map.json"),
        )
    if recipe.is_file():
        return AuthoredRecipe(name, "manual", None, recipe, None, None)
    raise AuthoringError(f"recipe {name!r} has no workflow source or manual yamlgraph file")


def compile_source(
    source: Path,
    *,
    children: Mapping[str, tuple[ValidatedWorkflow, CompilationResult]] | None = None,
) -> tuple[ValidatedWorkflow, ResolvedCatalog, CompilationResult]:
    """Strictly parse, catalog, validate and compile one source in memory."""

    return compile_captured_source(load_workflow(source), children=children)


def compile_project_source(
    source: Path,
    *,
    _cache: dict[
        Path, tuple[ValidatedWorkflow, ResolvedCatalog, CompilationResult]
    ]
    | None = None,
    _active: set[Path] | None = None,
) -> tuple[ValidatedWorkflow, ResolvedCatalog, CompilationResult]:
    """Compile the conventional authored child DAG entirely in memory."""

    path = Path(source).resolve()
    cache = {} if _cache is None else _cache
    active = set() if _active is None else _active
    if path in cache:
        return cache[path]
    if path in active:
        raise AuthoringError("workflow source dependency graph is recursive")
    active.add(path)
    try:
        children = {}
        for child in workflow_call_names(load_workflow(path)):
            child_path = path.parent / f"{child}.workflow.yaml"
            if not child_path.is_file():
                raise AuthoringError(
                    f"workflow dependency source is missing: {child}.workflow.yaml"
                )
            child_validated, _child_catalog, child_compiled = compile_project_source(
                child_path, _cache=cache, _active=active
            )
            children[child] = (child_validated, child_compiled)
        result = compile_source(path, children=children)
        cache[path] = result
        return result
    finally:
        active.remove(path)


def link_recipe_dependencies(recipe_bytes: bytes, children: tuple[str, ...]) -> bytes:
    """Add inert strict-ingress links to separately installed child recipes."""

    return canonical_recipe_bytes_for_children(recipe_bytes, children)


def canonical_recipe_bytes(source: Path, compiled: CompilationResult) -> bytes:
    """Return the one canonical on-disk root, including child ingress links."""

    return canonical_recipe_bytes_for_children(
        compiled.recipe_bytes, workflow_call_names(load_workflow(source))
    )


def _generated_candidates(recipe: AuthoredRecipe, compiled: CompilationResult) -> dict[str, bytes]:
    root = recipe.recipe_path.parent
    candidates = {}
    for item in compiled.generated_files:
        path = root / item.relative_path
        try:
            candidates[item.relative_path] = path.read_bytes()
        except OSError as exc:
            raise AuthoringError(
                f"generated file {item.relative_path!r} is missing"
            ) from exc
    return candidates


def canonical_match(recipe: AuthoredRecipe) -> CompilerProvenance:
    if recipe.kind != "workflow" or recipe.workflow_path is None:
        raise AuthoringError(f"manual yamlgraph recipe {recipe.name!r} has no canonical source")
    validated, catalog, compiled = compile_project_source(recipe.workflow_path)
    try:
        root = recipe.recipe_path.read_bytes()
        dependency = recipe.dependency_path.read_bytes() if recipe.dependency_path else None
    except OSError as exc:
        raise AuthoringError("generated canonical files are missing") from exc
    generated = _generated_candidates(recipe, compiled)
    expected_root = canonical_recipe_bytes(recipe.workflow_path, compiled)
    if root != expected_root:
        raise AuthoringError(
            "generated recipe is not a byte-for-byte canonical match"
        )

    calls = workflow_call_names(load_workflow(recipe.workflow_path))
    if not calls:
        return verify_canonical_match(
            validated,
            catalog,
            root,
            candidate_generated_files=generated,
            candidate_dependency_manifest_bytes=dependency,
        )

    if dependency != compiled.dependency_manifest_bytes:
        raise AuthoringError(
            "dependency manifest is not a byte-for-byte canonical match"
        )
    for child in calls:
        canonical_match(project_paths(recipe.recipe_path.parent.parent.parent, child))
    candidate = StrictRecipeIngress(recipe.recipe_path.parent).inspect(
        recipe.recipe_path.name
    )
    observed = {item.path: item.bytes for item in candidate.files}
    for path, expected in generated.items():
        if path not in observed or observed[path] != canonical_execution_bytes(
            expected, logical_path=path
        ):
            raise AuthoringError(
                f"generated file {path!r} is not a byte-for-byte canonical match"
            )
    execution_root = observed.pop(candidate.root)
    return _create_compiler_provenance(
        root,
        context="canonical-match",
        root_relative_path=candidate.root,
        generated_files={item.path: item.bytes for item in candidate.files if item.path != candidate.root},
        execution_recipe_bytes=execution_root,
        execution_generated_files=observed,
        source_bundle_sha256=candidate.source_bundle_sha256,
    )


def classify_generated_recipe(
    recipes_dir: Path, name: str, recipe_path: Path
) -> CompilerProvenance | None:
    """Classify checked-in recipe bytes before any durable admission.

    A generated marker is a claim, never a fallback hint: it must identify the
    conventional in-project workflow source and every compiler artifact must
    still match byte-for-byte.  Recipes without the marker remain manual
    yamlgraph inputs.
    """

    try:
        raw = recipe_path.read_bytes()
    except OSError as exc:
        raise AuthoringError(f"recipe {name!r} is missing") from exc
    if len(raw) > 1024 * 1024:
        raise AuthoringError("recipe source exceeds the authoring classification limit")
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise AuthoringError(f"recipe {name!r} is not valid YAML") from exc
    marker = document.get("x-lockstep-generated") if isinstance(document, dict) else None
    if marker is None:
        return None
    if not isinstance(marker, dict):
        raise AuthoringError("generated recipe marker must be a mapping")
    expected_source = f"../workflows/{name}.workflow.yaml"
    if marker.get("source") != expected_source:
        raise AuthoringError(
            "generated recipe declares a non-canonical workflow source"
        )
    root = Path(recipes_dir).resolve().parent.parent
    recipe = project_paths(root, name)
    if recipe.kind != "workflow" or recipe.workflow_path is None:
        raise AuthoringError("generated recipe source is missing")
    if recipe.recipe_path.resolve() != recipe_path.resolve():
        raise AuthoringError("generated recipe is outside the canonical project layout")
    try:
        return canonical_match(recipe)
    except (OSError, ValueError) as exc:
        raise AuthoringError(
            f"generated recipe failed canonical match: {exc}"
        ) from exc


def write_compilation(recipe: AuthoredRecipe) -> CompilationResult:
    if recipe.kind != "workflow" or recipe.workflow_path is None:
        raise AuthoringError(
            f"manual yamlgraph recipe {recipe.name!r} has no generated output to compile"
        )
    _validated, _catalog, compiled = compile_project_source(recipe.workflow_path)
    destinations = {
        recipe.recipe_path: canonical_recipe_bytes(recipe.workflow_path, compiled),
        recipe.dependency_path: compiled.dependency_manifest_bytes,
        recipe.source_map_path: compiled.source_map_bytes,
        **{
            recipe.recipe_path.parent / item.relative_path: item.content
            for item in compiled.generated_files
        },
    }
    for path, content in destinations.items():
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return compiled


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


def diff_recipe(project: Path, name: str) -> str:
    recipe = project_paths(project, name)
    if recipe.kind == "manual" or recipe.workflow_path is None:
        return ""
    _validated, _catalog, compiled = compile_project_source(recipe.workflow_path)
    observed = recipe.recipe_path.read_text() if recipe.recipe_path.exists() else ""
    expected = canonical_recipe_bytes(recipe.workflow_path, compiled).decode("utf-8")
    return "".join(
        difflib.unified_diff(
            observed.splitlines(keepends=True),
            expected.splitlines(keepends=True),
            fromfile=str(recipe.recipe_path),
            tofile="canonical",
        )
    )


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
    assert recipe.workflow_path is not None
    validated, catalog, _compiled = compile_project_source(recipe.workflow_path)
    return estimate_workflow(validated.workflow, catalog).to_dict()


def initialize_minimal(project: Path, name: str) -> AuthoredRecipe:
    validate_logical_name(name)
    root = Path(project).resolve()
    workflow = root / ".lockstep" / "workflows" / f"{name}.workflow.yaml"
    recipe_path = root / ".lockstep" / "recipes" / f"{name}.recipe.yaml"
    for destination in (workflow, recipe_path):
        if destination.exists() or destination.is_symlink():
            raise AuthoringError(f"destination already exists: {destination.relative_to(root)}")
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(
        "workflow_version: '1'\n"
        f"name: {name}\n"
        "description: Native durable workflow\n"
        "protect: ['**']\n"
        "flow:\n"
        "  - escalate: {}\n"
    )
    recipe = project_paths(root, name)
    write_compilation(recipe)
    return recipe


def json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n"
