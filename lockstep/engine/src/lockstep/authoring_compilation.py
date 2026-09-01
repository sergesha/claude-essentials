"""Child-first captured-byte compilation and pure project planning."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

from lockstep.authoring_bundle import (
    AuthoredRecipe,
    AuthoringPlan,
    DirectoryIdentity,
    PlannedTarget,
    ProjectCompilation,
    SourceSnapshot,
    _workflow_project_and_source,
    canonical_recipe_bytes_for_children,
)
from lockstep.authoring_capture import (
    _AuthoringBudget,
    capture_directory,
    capture_optional_regular_file,
    capture_regular_file,
    validate_directory,
)
from lockstep.errors import AuthoringError
from lockstep.workflow.compiler import CompilationResult, compile_workflow_document
from lockstep.workflow.ir import BlockIR, CallIR, ChooseIR, ParallelIR, RepeatIR
from lockstep.workflow.schema import MarkedDocument, load_workflow_bytes, parse_workflow
from lockstep.workflow.semantics import (
    ChildWorkflowContract,
    ResolvedCatalog,
    ResolvedChild,
    ValidatedWorkflow,
)

_WORKFLOW_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_CompiledWorkflow = tuple[ValidatedWorkflow, CompilationResult]
_ProjectedRole = tuple[str, dict[Path, bytes]]


def validate_logical_name(name: str) -> str:
    if not isinstance(name, str) or not _WORKFLOW_NAME_RE.fullmatch(name):
        raise AuthoringError(
            f"invalid workflow name {name!r}; use lowercase letters, digits, and "
            "hyphens, beginning with a letter"
        )
    return name


def _nested_blocks(block: BlockIR) -> tuple[BlockIR, ...]:
    if isinstance(block, ChooseIR):
        branches = tuple(block.cases.values())
        if block.default is not None:
            branches += (block.default,)
        return tuple(child for branch in branches for child in branch)
    if isinstance(block, RepeatIR):
        return block.do
    if isinstance(block, ParallelIR):
        return tuple(child for branch in block.branches.values() for child in branch)
    return ()


def workflow_call_names(document: MarkedDocument) -> tuple[str, ...]:
    calls: list[str] = []
    seen: set[str] = set()
    pending = list(reversed(parse_workflow(document).flow))
    while pending:
        block = pending.pop()
        if isinstance(block, CallIR) and block.workflow not in seen:
            seen.add(block.workflow)
            calls.append(block.workflow)
        pending.extend(reversed(_nested_blocks(block)))
    return tuple(calls)


def _child_contract(validated: ValidatedWorkflow) -> ChildWorkflowContract:
    return ChildWorkflowContract(
        ("pass", "fail", "error"),
        exports=validated.exports,
        non_artifact_writes=validated.non_artifact_writes,
    )


def compile_captured_source(
    document: MarkedDocument,
    *,
    children: Mapping[str, tuple[ValidatedWorkflow, CompilationResult]] | None = None,
) -> tuple[ValidatedWorkflow, ResolvedCatalog, CompilationResult]:
    resolved = {
        name: ResolvedChild(
            name, _child_contract(validated), validated.workflow.source_sha256,
            compiled.as_catalog_bundle(),
        )
        for name, (validated, compiled) in (children or {}).items()
    }
    catalog = ResolvedCatalog(children=resolved)
    validated, compiled = compile_workflow_document(document, catalog)
    return validated, catalog, compiled


def _workflow_recipe(project: Path, name: str) -> AuthoredRecipe:
    validate_logical_name(name)
    workflow = project / ".lockstep" / "workflows" / f"{name}.workflow.yaml"
    recipe = project / ".lockstep" / "recipes" / f"{name}.recipe.yaml"
    return AuthoredRecipe(
        name, "workflow", workflow, recipe,
        recipe.with_name(f"{name}.dependencies.json"),
        recipe.with_name(f"{name}.source-map.json"),
    )


def _workflow_destinations(
    recipe: AuthoredRecipe, compiled: CompilationResult, children: tuple[str, ...]
) -> dict[Path, bytes]:
    if recipe.dependency_path is None or recipe.source_map_path is None:
        raise AuthoringError("workflow destinations are incomplete")
    destinations = {
        recipe.recipe_path: canonical_recipe_bytes_for_children(compiled.recipe_bytes, children),
        recipe.dependency_path: compiled.dependency_manifest_bytes,
        recipe.source_map_path: compiled.source_map_bytes,
    }
    for item in compiled.generated_files:
        path = recipe.recipe_path.parent / item.relative_path
        if path in destinations:
            raise AuthoringError("compiled workflow contains a duplicate destination")
        destinations[path] = item.content
    return destinations


def _cached_directory(
    identities: dict[Path, DirectoryIdentity], path: Path
) -> DirectoryIdentity:
    identity = identities.get(path)
    if identity is None:
        identity = capture_directory(path, label="destination ancestor")
        identities[path] = identity
    return identity


def _validate_parents(parents: tuple[DirectoryIdentity, ...]) -> None:
    for parent in parents:
        validate_directory(parent, label="destination ancestor")


def _source_snapshot(
    role: str,
    path: Path,
    project: Path,
    identities: dict[Path, DirectoryIdentity],
    *,
    max_bytes: int,
) -> SourceSnapshot:
    parents = tuple(
        _cached_directory(identities, parent)
        for parent in (project, project / ".lockstep", path.parent)
    )
    _validate_parents(parents)
    content, file = capture_regular_file(
        path, max_bytes=max_bytes, label="workflow source"
    )
    _validate_parents(parents)
    return SourceSnapshot(
        role, path, content, hashlib.sha256(content).hexdigest(), file, parents
    )


class _ClosureCompiler:
    def __init__(self, project: Path, identities: dict[Path, DirectoryIdentity]) -> None:
        self.project, self.identities = project, identities
        self.sources: list[SourceSnapshot] = []
        self.edges: list[tuple[str, tuple[str, ...]]] = []
        self.projected: list[_ProjectedRole] = []
        self.completed: dict[str, _CompiledWorkflow] = {}
        self.catalogs: dict[str, ResolvedCatalog] = {}
        self.active: set[str] = set()
        self.destinations: set[Path] = set()
        self.read_budget = _AuthoringBudget("authoring read set")

    def visit(self, recipe: AuthoredRecipe, path: Path) -> None:
        role = recipe.name
        if role in self.completed:
            return
        if role in self.active:
            raise AuthoringError("workflow source dependency graph is recursive")
        self.active.add(role)
        try:
            source = _source_snapshot(
                role, path, self.project, self.identities,
                max_bytes=self.read_budget.max_bytes_for_next,
            )
            self.read_budget.retain(source.content)
            document = load_workflow_bytes(source.path, source.content)
            children = workflow_call_names(document)
            for child in children:
                child_recipe = _workflow_recipe(self.project, child)
                if child_recipe.workflow_path is None:
                    raise AuthoringError("workflow source is required")
                self.visit(child_recipe, child_recipe.workflow_path)
            validated, catalog, compiled = compile_captured_source(
                document, children={child: self.completed[child] for child in children}
            )
            projected = _workflow_destinations(recipe, compiled, children)
            if any(path in self.destinations for path in projected):
                raise AuthoringError("compilation destinations must be unique")
            self.destinations.update(projected)
            self.completed[role], self.catalogs[role] = (validated, compiled), catalog
            self.sources.append(source)
            self.edges.append((role, children))
            self.projected.append((role, projected))
        finally:
            self.active.remove(role)


def _target_parents(
    project: Path, parent: Path, identities: dict[Path, DirectoryIdentity]
) -> tuple[DirectoryIdentity, ...]:
    try:
        relative = parent.relative_to(project)
    except ValueError as exc:
        raise AuthoringError("workflow destination is outside the project") from exc
    parents = [_cached_directory(identities, project)]
    current = project
    for part in relative.parts:
        current /= part
        try:
            parents.append(_cached_directory(identities, current))
        except FileNotFoundError:
            break
    return tuple(parents)


def _planned_targets(
    project: Path,
    projected: tuple[_ProjectedRole, ...],
    identities: dict[Path, DirectoryIdentity],
) -> tuple[PlannedTarget, ...]:
    destinations: dict[Path, tuple[str, bytes]] = {}
    after_budget, before_budget = (
        _AuthoringBudget("authoring after images"),
        _AuthoringBudget("authoring before images"),
    )
    for role, images in projected:
        for path, content in images.items():
            if path in destinations:
                raise AuthoringError("compilation destinations must be unique")
            after_budget.retain(content)
            destinations[path] = role, content
    parents = {
        parent: _target_parents(project, parent, identities)
        for parent in dict.fromkeys(path.parent for path in destinations)
    }
    targets = []
    for path, (role, after) in destinations.items():
        _validate_parents(parents[path.parent])
        captured = capture_optional_regular_file(
            path, max_bytes=before_budget.max_bytes_for_next,
            label="compilation destination",
        )
        before_budget.retain(None if captured is None else captured[0])
        _validate_parents(parents[path.parent])
        before, file = (None, None) if captured is None else captured
        targets.append(PlannedTarget(
            role, path, before,
            None if before is None else hashlib.sha256(before).hexdigest(), file,
            after, hashlib.sha256(after).hexdigest(), 0o644, parents[path.parent],
        ))
    return tuple(targets)


def _authoring_plan(
    project: Path,
    edges: tuple[tuple[str, tuple[str, ...]], ...],
    projected: tuple[_ProjectedRole, ...],
    identities: dict[Path, DirectoryIdentity],
    sources: tuple[SourceSnapshot, ...] = (),
) -> AuthoringPlan:
    project_identity = _cached_directory(identities, project)
    return AuthoringPlan(
        project, project_identity, sources, edges,
        _planned_targets(project, projected, identities),
    )


def _plan_destination_only(
    project: Path,
    edges: tuple[tuple[str, tuple[str, ...]], ...],
    projected: tuple[_ProjectedRole, ...],
) -> AuthoringPlan:
    root = Path(project).resolve()
    if tuple(role for role, _images in projected) != tuple(role for role, _ in edges):
        raise AuthoringError("destination-only projections must match dependency roles")
    return _authoring_plan(root, edges, projected, {})


def compile_project(recipe: AuthoredRecipe) -> ProjectCompilation:
    if recipe.kind != "workflow" or recipe.workflow_path is None:
        raise AuthoringError("only ordinary workflow sources can be planned")
    project, source = _workflow_project_and_source(recipe)
    identities: dict[Path, DirectoryIdentity] = {}
    closure = _ClosureCompiler(project, identities)
    closure.visit(recipe, source)
    validated, result = closure.completed[recipe.name]
    plan = _authoring_plan(
        project, tuple(closure.edges), tuple(closure.projected), identities,
        tuple(closure.sources),
    )
    return ProjectCompilation(plan, validated, closure.catalogs[recipe.name], result)


def plan_project_compilation(recipe: AuthoredRecipe) -> AuthoringPlan:
    return compile_project(recipe).plan
