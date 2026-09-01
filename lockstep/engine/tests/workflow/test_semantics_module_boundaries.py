"""Architecture boundary for the Workflow DSL semantic validator."""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from dataclasses import fields
from pathlib import Path
from typing import Mapping, get_type_hints  # noqa: UP035 - freezes public hints

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "lockstep" / "workflow"
STATE_FIELDS = {
    "workflow",
    "catalog",
    "outcomes",
    "artifacts",
    "exports",
    "export_paths",
    "export_producers",
    "ids",
}


def _is_frozen_dataclass(node: ast.ClassDef) -> bool:
    return any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "dataclass"
        and any(
            keyword.arg == "frozen"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in decorator.keywords
        )
        for decorator in node.decorator_list
    )


def test_semantics_facade_composes_single_state_owner_and_identity_reexports() -> None:
    facade = importlib.import_module("lockstep.workflow.semantics")
    catalog = importlib.import_module("lockstep.workflow._semantics_catalog")
    contracts = importlib.import_module("lockstep.workflow._semantics_contracts")
    validation = importlib.import_module("lockstep.workflow._semantics_validation")

    for name in (
        "ChildArtifactContract", "ChildWorkflowContract", "CatalogFile",
        "BundleDependency", "CanonicalCompiledBundle", "ResolvedChild",
        "ResolvedFragment", "ResolvedCatalog", "WorkflowCatalog",
        "InMemoryWorkflowCatalog",
    ):
        assert getattr(facade, name) is getattr(catalog, name)
    for name in (
        "OutcomeProvenance", "OutcomeSymbol", "EffectContract", "ArtifactContract",
        "RetryContract", "RepeatSimulation", "RepeatControlContract",
        "RepeatContract", "BlockContract", "FlowContract", "ValidatedWorkflow",
    ):
        assert getattr(facade, name) is getattr(contracts, name)

    assert [item.name for item in fields(validation._ValidationState)] == [
        "workflow", "catalog", "outcomes", "artifacts", "exports",
        "export_paths", "export_producers", "ids",
    ]
    for path in SOURCE_ROOT.glob("_semantics_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for owner in (
            node for node in tree.body if isinstance(node, ast.ClassDef)
        ):
            declared_state = {
                member.target.id
                for member in owner.body
                if isinstance(member, ast.AnnAssign)
                and isinstance(member.target, ast.Name)
                and member.target.id in STATE_FIELDS
            }
            if owner.name == "_ValidationState":
                assert declared_state == STATE_FIELDS
            elif not _is_frozen_dataclass(owner):
                assert not declared_state, (path.name, owner.name, declared_state)
        assert not any(
            isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            and any(
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr in STATE_FIELDS
                for target in (
                    node.targets if isinstance(node, ast.Assign) else (node.target,)
                )
            )
            for node in ast.walk(tree)
        )


def test_semantics_owner_modules_do_not_import_the_facade() -> None:
    for path in SOURCE_ROOT.glob("_semantics_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        from_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert "lockstep.workflow.semantics" not in imports | from_imports
        assert "semantics" not in imports | from_imports


def test_semantics_internal_import_graph_is_acyclic() -> None:
    paths = tuple(SOURCE_ROOT.glob("_semantics_*.py"))
    modules = {path.stem for path in paths}
    edges: dict[str, set[str]] = {module: set() for module in modules}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level < 1 or node.module is None:
                continue
            imported = node.module.split(".", 1)[0]
            if imported.startswith("_semantics_"):
                assert imported in modules, (path.name, imported)
                edges[path.stem].add(imported)

    remaining = {module: set(imports) for module, imports in edges.items()}
    while ready := {module for module, imports in remaining.items() if not imports}:
        for module in ready:
            remaining.pop(module)
        for imports in remaining.values():
            imports.difference_update(ready)
    assert not remaining, remaining


def test_semantics_public_mapping_type_hints_keep_typing_identity() -> None:
    facade = importlib.import_module("lockstep.workflow.semantics")

    child_hints = get_type_hints(facade.ChildWorkflowContract)
    assert child_hints["exports"] == Mapping[str, facade.ChildArtifactContract]
    assert child_hints["state_inputs"] == Mapping[str, facade.YamlgraphStateType]
    assert child_hints["state_exports"] == Mapping[str, facade.YamlgraphStateType]

    resolved_hints = get_type_hints(facade.ResolvedCatalog)
    assert resolved_hints["children"] == Mapping[str, facade.ResolvedChild]
    assert resolved_hints["fragments"] == Mapping[str, facade.ResolvedFragment]
    assert get_type_hints(facade.InMemoryWorkflowCatalog)["contracts"] == Mapping[
        str, facade.ChildWorkflowContract
    ]

    assert get_type_hints(facade.BlockContract)["branches"] == Mapping[
        str, facade.FlowContract
    ]
    validated_hints = get_type_hints(facade.ValidatedWorkflow)
    assert validated_hints["outcomes"] == Mapping[str, facade.OutcomeSymbol]
    assert validated_hints["artifacts"] == Mapping[str, facade.ArtifactContract]


def test_semantics_leaf_and_facade_import_orders_are_fresh_and_identity_safe() -> None:
    leaf_first = """
import importlib
catalog = importlib.import_module('lockstep.workflow._semantics_catalog')
contracts = importlib.import_module('lockstep.workflow._semantics_contracts')
semantics = importlib.import_module('lockstep.workflow.semantics')
assert semantics.ResolvedCatalog is catalog.ResolvedCatalog
assert semantics.ValidatedWorkflow is contracts.ValidatedWorkflow
"""
    facade_first = """
import importlib
semantics = importlib.import_module('lockstep.workflow.semantics')
catalog = importlib.import_module('lockstep.workflow._semantics_catalog')
contracts = importlib.import_module('lockstep.workflow._semantics_contracts')
assert semantics.ResolvedCatalog is catalog.ResolvedCatalog
assert semantics.ValidatedWorkflow is contracts.ValidatedWorkflow
"""
    for script in (leaf_first, facade_first):
        subprocess.run(
            (sys.executable, "-c", script),
            check=True,
            capture_output=True,
            text=True,
        )
