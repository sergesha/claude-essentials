"""Pure planning of captured workflow bytes for one authoring transaction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat

from lockstep.authoring_bundle import (
    AuthoringPlan,
    PlannedTarget,
    canonical_recipe_bytes_for_children,
)
from lockstep.authoring_compilation import (
    _plan_destination_only,
    compile_captured_source,
    workflow_call_names,
)
from lockstep.errors import AuthoringError
from lockstep.workflow.compiler import CompilationResult
from lockstep.workflow.schema import load_workflow_bytes
from lockstep.workflow.semantics import ValidatedWorkflow


@dataclass(frozen=True, slots=True)
class CapturedWorkflowSource:
    role: str
    content: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("workflow role must be non-empty")
        if not isinstance(self.content, bytes):
            raise TypeError("workflow source content must be bytes")


@dataclass(frozen=True, slots=True)
class PlannedWorkflowInstallation:
    plan: AuthoringPlan
    sources: tuple[Path, ...]
    recipes: tuple[Path, ...]
    compile_order: tuple[str, ...]


def installation_collision(plan: AuthoringPlan) -> PlannedTarget | None:
    """Return the first collision, allowing only an exact canonical prefix."""
    occupied = tuple(target.before is not None for target in plan.targets)
    if not any(occupied):
        return None
    first = plan.targets[occupied.index(True)]
    prefix = occupied.index(False) if False in occupied else len(occupied)
    if prefix == len(occupied) or any(occupied[prefix:]):
        return first
    for target in plan.targets[:prefix]:
        if (
            target.before != target.after
            or target.before_file is None
            or stat.S_IMODE(target.before_file.mode) != target.mode
        ):
            return first
    return None


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
            raise AuthoringError("compiled workflow contains a duplicate destination")
        destinations[destination] = item.content
    return destinations, workflow, recipe


def plan_captured_workflow_installation(
    project: Path,
    role_sources: tuple[CapturedWorkflowSource, ...],
    *,
    root_role: str,
) -> PlannedWorkflowInstallation:
    """Compile a closed captured source set into one destination-only plan."""

    root = Path(project).resolve()
    by_role = {item.role: item for item in role_sources}
    if len(by_role) != len(role_sources) or root_role not in by_role:
        raise AuthoringError("workflow role inventory is incomplete")
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
            raise AuthoringError("workflow role dependencies are recursive")
        active.add(role)
        try:
            source = by_role[role]
            source_path = (
                root / ".lockstep" / "workflows" / f"{role}.workflow.yaml"
            )
            document = load_workflow_bytes(source_path, source.content)
            children = workflow_call_names(document)
            if any(child not in by_role for child in children):
                raise AuthoringError("workflow role dependency is undeclared")
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
        raise AuthoringError("workflow role inventory contains unreachable roles")
    plan = _plan_destination_only(
        root, tuple(dependency_edges), tuple(projected_roles)
    )
    return PlannedWorkflowInstallation(
        plan,
        tuple(source_paths),
        tuple(recipe_paths),
        tuple(role for role, _children in dependency_edges),
    )
