"""Structural guardrail for methods confirmed as mixed-responsibility gods."""

from __future__ import annotations

import ast
from pathlib import Path
import warnings

import pytest


SOURCE_ROOT = Path(__file__).parents[2] / "src" / "lockstep"

CONFIRMED_GOD_METHODS = (
    ("runtime/effects/coordinator.py", "EffectCoordinator.reconcile"),
    ("workflow/lowering.py", "_Builder.graph"),
    ("workflow/lowering.py", "_Builder.call"),
    ("workflow/lowering.py", "_Builder._specialize_child"),
    ("runtime/effects/coordinator.py", "EffectCoordinator._reconcile_publication"),
    ("runtime/effects/ledger.py", "EffectLedger._transition"),
    ("runtime/effects/coordinator.py", "EffectCoordinator._context"),
    ("runtime/effects/coordinator.py", "EffectCoordinator._publication_intent"),
    ("runtime/providers/_codex_supervisor.py", "run"),
    ("runtime/providers/workspaces.py", "LocalGitWorkspaceProvider.quarantine_and_rollover"),
    ("runtime/providers/workspaces.py", "LocalGitWorkspaceProvider.materialize"),
    ("workflow/lowering.py", "_Builder.block"),
    ("workflow/lowering.py", "_Builder.parallel"),
    ("runtime/providers/codex.py", "_CodexAttemptDriver.prepare"),
    ("runtime/effects/coordinator.py", "EffectCoordinator.submit_acceptance"),
    ("runtime/effects/coordinator.py", "EffectCoordinator.deliver_ready"),
    ("runtime/service.py", "LockstepCommandService._drive_engine_owned"),
    ("runtime/status.py", "project_status"),
    ("runtime/service.py", "LockstepCommandService.start_authorized"),
    ("runtime/effects/coordinator.py", "EffectCoordinator.submit_manual"),
)


class ComplexityReviewWarning(UserWarning):
    """A long boundary needs semantic review but is not rejected by length."""


def _function_node(relative_file: str, qualified_name: str) -> ast.AST:
    source = (SOURCE_ROOT / relative_file).read_text(encoding="utf-8")
    current: ast.AST = ast.parse(source)
    for member_name in qualified_name.split("."):
        body = getattr(current, "body", ())
        current = next(
            member
            for member in body
            if isinstance(member, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name == member_name
        )
    return current


_NESTING_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.Match,
)

_NESTED_SCOPES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
)


def _complexity(node: ast.AST) -> tuple[int, int, int]:
    """Return deterministic cyclomatic, cognitive, and nesting metrics."""

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


def _call_targets(node: ast.AST) -> set[str]:
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
    return targets


@pytest.mark.parametrize(
    ("relative_file", "qualified_name"),
    CONFIRMED_GOD_METHODS,
    ids=[qualified_name for _relative_file, qualified_name in CONFIRMED_GOD_METHODS],
)
def test_confirmed_god_method_is_reduced_to_a_thin_boundary(
    relative_file: str,
    qualified_name: str,
) -> None:
    node = _function_node(relative_file, qualified_name)
    line_count = node.end_lineno - node.lineno + 1
    cyclomatic, cognitive, max_nesting = _complexity(node)
    call_targets = _call_targets(node)

    if line_count >= 80:
        warnings.warn(
            f"{qualified_name} is {line_count} lines and requires cohesion review",
            ComplexityReviewWarning,
            stacklevel=1,
        )
    violations = []
    if cyclomatic >= 16:
        violations.append(f"cyclomatic={cyclomatic} (limit 15)")
    if cognitive >= 26:
        violations.append(f"cognitive={cognitive} (limit 25)")
    if max_nesting >= 5:
        violations.append(f"nesting={max_nesting} (limit 4)")
    if len(call_targets) >= 25:
        violations.append(f"fan_out={len(call_targets)} (limit 24)")
    assert not violations, (
        f"{qualified_name} remains a structurally overloaded boundary: "
        + ", ".join(violations)
    )
