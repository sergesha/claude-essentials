"""Architecture contract for explicit workspace collaborator ownership."""

from __future__ import annotations

import ast
from dataclasses import is_dataclass
import inspect
from pathlib import Path
from typing import Any, get_type_hints

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.project_paths import ProjectTreeLimits
from lockstep.runtime.project_snapshots import ProjectSnapshotStore
from lockstep.runtime.providers import _workspace_core
from lockstep.runtime.providers._workspace_attestation import WorkspaceAttestor
from lockstep.runtime.providers._workspace_materialization import (
    WorkspaceMaterializationTransaction,
)
from lockstep.runtime.providers._workspace_records import WorkspaceRecordRepository
from lockstep.runtime.providers._workspace_rollover import WorkspaceRolloverTransaction
from lockstep.runtime.providers.workspaces import LocalGitWorkspaceProvider


_HELPER_CONTRACTS = {
    WorkspaceAttestor: (("context", "WorkspaceContext"),),
    WorkspaceRecordRepository: (("context", "WorkspaceContext"),),
    WorkspaceMaterializationTransaction: (
        ("context", "WorkspaceContext"),
        ("records", "WorkspaceRecordRepository"),
        ("attestor", "WorkspaceAttestor"),
    ),
    WorkspaceRolloverTransaction: (
        ("context", "WorkspaceContext"),
        ("records", "WorkspaceRecordRepository"),
        ("attestor", "WorkspaceAttestor"),
    ),
}


def _annotation_name(annotation: object) -> str:
    if annotation is inspect.Parameter.empty:
        return "<missing>"
    if isinstance(annotation, str):
        return annotation
    return getattr(annotation, "__name__", repr(annotation))


def _class_node(subject: type) -> ast.ClassDef:
    source_path = inspect.getsourcefile(subject)
    assert source_path is not None
    module = ast.parse(Path(source_path).read_text(encoding="utf-8"))
    return next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == subject.__name__
    )


def _dynamic_resolution_sites(subject: type) -> tuple[str, ...]:
    sites: list[str] = []
    for node in ast.walk(_class_node(subject)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "__getattr__",
            "__getattribute__",
        }:
            sites.append(f"{node.name}@{node.lineno}")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in {"_owner", "_provider", "_facade"}
        ):
            sites.append(f"facade-reference@{node.lineno}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and not (
                isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            )
        ):
            sites.append(f"dynamic-getattr@{node.lineno}")
    return tuple(sorted(sites))


def _instance_references(instance: object) -> tuple[object, ...]:
    references = list(getattr(instance, "__dict__", {}).values())
    for owner in type(instance).__mro__:
        slots = owner.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            if name not in {"__dict__", "__weakref__"} and hasattr(instance, name):
                references.append(getattr(instance, name))
    return tuple(references)


def test_workspace_collaborators_have_an_explicit_immutable_dependency_graph(
    tmp_path: Path,
) -> None:
    """Reject a facade back-reference or dynamic service-locator extraction."""

    violations: list[str] = []
    context_type = getattr(_workspace_core, "WorkspaceContext", None)
    if not inspect.isclass(context_type):
        violations.append("_workspace_core.WorkspaceContext is missing")
    else:
        parameters = getattr(context_type, "__dataclass_params__", None)
        if not is_dataclass(context_type) or parameters is None or not parameters.frozen:
            violations.append("WorkspaceContext must be a frozen dataclass")
        context_hints = get_type_hints(context_type)
        expected_resources = {BlobStore, ProjectSnapshotStore, ProjectTreeLimits}
        missing_resources = expected_resources - set(context_hints.values())
        if missing_resources:
            violations.append(
                "WorkspaceContext lacks typed resources: "
                + ", ".join(sorted(item.__name__ for item in missing_resources))
            )
        path_fields = sum(annotation is Path for annotation in context_hints.values())
        if path_fields < 4:
            violations.append(
                "WorkspaceContext must explicitly carry record, checkout, staging, "
                "and quarantine paths"
            )
        allowed_context_types = expected_resources | {Path}
        unsupported_context = {
            name: annotation
            for name, annotation in context_hints.items()
            if annotation not in allowed_context_types
        }
        if unsupported_context:
            violations.append(
                "WorkspaceContext contains non-data dependencies: "
                + ", ".join(
                    f"{name}: {_annotation_name(annotation)}"
                    for name, annotation in sorted(unsupported_context.items())
                )
            )

    for helper_type, expected in _HELPER_CONTRACTS.items():
        parameters = tuple(inspect.signature(helper_type.__init__).parameters.values())
        actual = tuple(
            (parameter.name, _annotation_name(parameter.annotation))
            for parameter in parameters
            if parameter.name != "self"
        )
        if actual != expected:
            violations.append(
                f"{helper_type.__name__} constructor is {actual!r}, expected {expected!r}"
            )
        if any(
            parameter.annotation is Any
            or _annotation_name(parameter.annotation) == "Any"
            for parameter in parameters
        ):
            violations.append(f"{helper_type.__name__} constructor accepts Any")
        dynamic_sites = _dynamic_resolution_sites(helper_type)
        if dynamic_sites:
            violations.append(
                f"{helper_type.__name__} dynamically resolves collaborators at "
                + ", ".join(dynamic_sites)
            )

    owner = tmp_path / "owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    provider = LocalGitWorkspaceProvider(owner, snapshots, blobs)
    helpers = (
        provider._attestor,
        provider._record_repository,
        provider._materializer,
        provider._rollover,
    )
    for helper in helpers:
        if any(reference is provider for reference in _instance_references(helper)):
            violations.append(
                f"{type(helper).__name__} retains a reverse reference to the facade"
            )

    if inspect.isclass(context_type):
        context = getattr(provider, "_context", None)
        if not isinstance(context, context_type):
            violations.append("LocalGitWorkspaceProvider does not own one WorkspaceContext")
        else:
            for helper in helpers:
                references = _instance_references(helper)
                if not any(reference is context for reference in references):
                    violations.append(
                        f"{type(helper).__name__} does not retain the shared context"
                    )
            for transaction in (provider._materializer, provider._rollover):
                references = _instance_references(transaction)
                if not any(
                    reference is provider._record_repository for reference in references
                ):
                    violations.append(
                        f"{type(transaction).__name__} lacks the explicit record repository"
                    )
                if not any(reference is provider._attestor for reference in references):
                    violations.append(
                        f"{type(transaction).__name__} lacks the explicit attestor"
                    )

    assert not violations, "workspace collaborator boundary violations:\n- " + "\n- ".join(
        violations
    )
