"""Frozen pre-ratchet structural metrics."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from architecture_source_index import SourceIndex


@dataclass(frozen=True, slots=True)
class LegacyMetrics:
    cyclomatic: int
    cognitive: int
    max_nesting: int
    legacy_syntactic_fanout: int


_NESTING_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.Match)
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _complexity(node: ast.AST) -> tuple[int, int, int]:
    cyclomatic = 1
    cognitive = 0
    max_nesting = 0

    def visit(member: ast.AST, nesting: int) -> None:
        nonlocal cyclomatic, cognitive, max_nesting
        if isinstance(member, _NESTED_SCOPES):
            return
        nested = nesting
        if isinstance(member, _NESTING_NODES):
            cyclomatic += 1
            cognitive += 1 + nesting
            nested += 1
            max_nesting = max(max_nesting, nested)
        elif isinstance(member, ast.ExceptHandler):
            cyclomatic += 1
            cognitive += 1 + nesting
            nested += 1
            max_nesting = max(max_nesting, nested)
        elif isinstance(member, ast.BoolOp):
            increment = max(0, len(member.values) - 1)
            cyclomatic += increment
            cognitive += increment
        elif isinstance(member, (ast.Break, ast.Continue)):
            cognitive += 1
        for child in ast.iter_child_nodes(member):
            visit(child, nested)

    for statement in getattr(node, "body", ()):
        visit(statement, 0)
    return cyclomatic, cognitive, max_nesting


def _fanout(node: ast.AST) -> int:
    targets: set[str] = set()

    def visit(member: ast.AST) -> None:
        if isinstance(member, _NESTED_SCOPES):
            return
        if isinstance(member, ast.Call):
            targets.add(ast.dump(member.func, include_attributes=False))
        for child in ast.iter_child_nodes(member):
            visit(child)

    for statement in getattr(node, "body", ()):
        visit(statement)
    return len(targets)


def _source_metrics(
    path: str, source: bytes, wanted: set[str]
) -> dict[str, LegacyMetrics]:
    measured: dict[str, LegacyMetrics] = {}

    def visit(node: ast.AST, parents: tuple[str, ...]) -> None:
        nested = parents
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nested = (*parents, node.name)
            identity = f"{path}::{'.'.join(nested)}"
            if identity in wanted and isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                cyclomatic, cognitive, nesting = _complexity(node)
                measured[identity] = LegacyMetrics(
                    cyclomatic,
                    cognitive,
                    nesting,
                    _fanout(node),
                )
        for child in ast.iter_child_nodes(node):
            visit(child, nested)

    visit(ast.parse(source.decode("utf-8"), filename=path), ())
    return measured


def measure_legacy_metrics(index: SourceIndex) -> Mapping[str, LegacyMetrics]:
    """Measure only indexed functions, pruning every nested lexical scope."""

    groups: dict[tuple[str, bytes], set[str]] = {}
    for entity in index.entities.values():
        stable_identity = entity.identity
        path = stable_identity.rpartition("::")[0]
        groups.setdefault((path, entity.source), set()).add(stable_identity)
    available: dict[str, LegacyMetrics] = {}
    for (path, source), identities in groups.items():
        available.update(_source_metrics(path, source, identities))
    measured = {
        identity: available[entity.identity]
        for identity, entity in index.entities.items()
        if entity.identity in available
    }
    return MappingProxyType(measured)
