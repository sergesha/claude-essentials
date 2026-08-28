"""Pure planning of package template bytes for one authoring transaction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lockstep.authoring_bundle import (
    ProjectCompilationBundle,
    _plan_destination_only_bundle,
    canonical_recipe_bytes_for_children,
)
from lockstep.authoring_compilation import (
    compile_captured_source,
    workflow_call_names,
)
from lockstep.errors import AuthoringError
from lockstep.workflow.compiler import CompilationResult
from lockstep.workflow.schema import load_workflow_bytes
from lockstep.workflow.semantics import ValidatedWorkflow


@dataclass(frozen=True, slots=True)
class TemplateRoleSource:
    role: str
    content: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("template role must be non-empty")
        if not isinstance(self.content, bytes):
            raise TypeError("template source content must be bytes")


@dataclass(frozen=True, slots=True)
class PlannedTemplateInstallation:
    bundle: ProjectCompilationBundle
    sources: tuple[Path, ...]
    recipes: tuple[Path, ...]
    compile_order: tuple[str, ...]


def _role_destinations(
    project: Path,
    role: str,
    source_content: bytes,
    children: tuple[str, ...],
    compiled: CompilationResult,
) -> tuple[dict[Path, bytes], Path, Path]:
    workflow = project / ".lockstep" / "workflows" / f"{role}.workflow.yaml"
    recipe_root = project / ".lockstep" / "recipes"
    recipe = recipe_root / f"{role}.recipe.yaml"
    destinations = {
        workflow: source_content,
        recipe: canonical_recipe_bytes_for_children(compiled.recipe_bytes, children),
        recipe_root / f"{role}.dependencies.json": compiled.dependency_manifest_bytes,
        recipe_root / f"{role}.source-map.json": compiled.source_map_bytes,
    }
    for item in compiled.generated_files:
        destination = recipe_root / item.relative_path
        if destination in destinations:
            raise AuthoringError("compiled template contains a duplicate destination")
        destinations[destination] = item.content
    return destinations, workflow, recipe


def plan_template_installation(
    project: Path,
    role_sources: tuple[TemplateRoleSource, ...],
    *,
    root_role: str,
) -> PlannedTemplateInstallation:
    """Compile captured template bytes into one destination-only bundle."""

    root = Path(project).resolve()
    by_role = {item.role: item for item in role_sources}
    if len(by_role) != len(role_sources) or root_role not in by_role:
        raise AuthoringError("template role inventory is incomplete")
    completed: dict[str, tuple[ValidatedWorkflow, CompilationResult]] = {}
    active: set[str] = set()
    dependency_edges: list[tuple[str, tuple[str, ...]]] = []
    projected_roles: list[tuple[str, dict[Path, bytes]]] = []
    source_paths: list[Path] = []
    recipe_paths: list[Path] = []

    def visit(role: str) -> None:
        if role in completed:
            return
        if role in active:
            raise AuthoringError("template role dependencies are recursive")
        active.add(role)
        try:
            source = by_role[role]
            source_path = (
                root / ".lockstep" / "workflows" / f"{role}.workflow.yaml"
            )
            document = load_workflow_bytes(source_path, source.content)
            children = workflow_call_names(document)
            if any(child not in by_role for child in children):
                raise AuthoringError("template role dependency is undeclared")
            for child in children:
                visit(child)
            validated, _catalog, compiled = compile_captured_source(
                document,
                children={child: completed[child] for child in children},
            )
            projected, workflow, recipe = _role_destinations(
                root, role, source.content, children, compiled
            )
            completed[role] = (validated, compiled)
            dependency_edges.append((role, children))
            projected_roles.append((role, projected))
            source_paths.append(workflow)
            recipe_paths.append(recipe)
        finally:
            active.remove(role)

    visit(root_role)
    if len(completed) != len(by_role):
        raise AuthoringError("template role inventory contains unreachable roles")
    bundle = _plan_destination_only_bundle(
        root, tuple(dependency_edges), tuple(projected_roles)
    )
    return PlannedTemplateInstallation(
        bundle,
        tuple(source_paths),
        tuple(recipe_paths),
        tuple(role for role, _children in dependency_edges),
    )
