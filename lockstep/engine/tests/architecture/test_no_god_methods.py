"""Compact structural guardrail for previously confirmed god methods."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "lockstep"

# These boundaries previously mixed multiple responsibilities.  Their current
# decomposed forms must remain structurally thin.
KNOWN_HOTSPOTS = (
    (
        "runtime/effects/_coordinator_reconciliation.py",
        "_EffectCoordinatorReconciliation.reconcile",
    ),
    ("workflow/_lowering_graph_driver.py", "_LoweringGraphDriver.graph"),
    ("workflow/_lowering_call.py", "_LoweringCall.call"),
    (
        "workflow/_lowering_call_bundle.py",
        "_LoweringCallBundle._specialize_child",
    ),
    (
        "runtime/effects/_coordinator_publication.py",
        "_EffectCoordinatorPublication._reconcile_publication",
    ),
    ("runtime/effects/ledger.py", "EffectLedger._transition"),
    (
        "runtime/effects/_coordinator_context.py",
        "_EffectCoordinatorContext._context",
    ),
    (
        "runtime/effects/_coordinator_publication_planning.py",
        "_EffectCoordinatorPublicationPlanning._publication_intent",
    ),
    ("runtime/providers/_codex_supervisor.py", "run"),
    (
        "runtime/providers/workspaces.py",
        "LocalGitWorkspaceProvider.quarantine_and_rollover",
    ),
    ("runtime/providers/workspaces.py", "LocalGitWorkspaceProvider.materialize"),
    ("workflow/_lowering_block_dispatch.py", "_LoweringBlockDispatch.block"),
    ("workflow/_lowering_parallel.py", "_LoweringParallel.parallel"),
    ("runtime/providers/_codex_attempt.py", "_CodexAttemptDriver.prepare"),
    (
        "runtime/effects/_coordinator_admission.py",
        "_EffectCoordinatorAdmission.submit_acceptance",
    ),
    (
        "runtime/effects/_coordinator_delivery.py",
        "_EffectCoordinatorDelivery.deliver_ready",
    ),
    ("runtime/_service_effect_drive.py", "_ServiceEffectDrive._drive_engine_owned"),
    ("runtime/status.py", "project_status"),
    ("runtime/_service_start.py", "_ServiceStart.start_authorized"),
    (
        "runtime/effects/_coordinator_admission.py",
        "_EffectCoordinatorAdmission.submit_manual",
    ),
)

MAX_CYCLOMATIC = 15
MAX_COGNITIVE = 25
MAX_NESTING = 4
MAX_FAN_OUT = 24

_NESTING_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.Match)
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _function_node(relative_file: str, qualified_name: str) -> ast.AST:
    current: ast.AST = ast.parse(
        (SOURCE_ROOT / relative_file).read_text(encoding="utf-8")
    )
    for member_name in qualified_name.split("."):
        current = next(
            member
            for member in getattr(current, "body", ())
            if isinstance(member, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name == member_name
        )
    return current


def _metrics(node: ast.AST) -> tuple[int, int, int, int]:
    cyclomatic = 1
    cognitive = 0
    max_nesting = 0
    call_targets: set[str] = set()

    def visit(member: ast.AST, nesting: int) -> None:
        nonlocal cyclomatic, cognitive, max_nesting
        if isinstance(member, _NESTED_SCOPES):
            return
        nested = nesting
        if isinstance(member, (_NESTING_NODES, ast.ExceptHandler)):
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
        if isinstance(member, ast.Call):
            call_targets.add(ast.dump(member.func, include_attributes=False))
        for child in ast.iter_child_nodes(member):
            visit(child, nested)

    for statement in getattr(node, "body", ()):
        visit(statement, 0)
    return cyclomatic, cognitive, max_nesting, len(call_targets)


def test_known_god_methods_remain_decomposed() -> None:
    violations: list[str] = []
    for relative_file, qualified_name in KNOWN_HOTSPOTS:
        cyclomatic, cognitive, nesting, fan_out = _metrics(
            _function_node(relative_file, qualified_name)
        )
        observed = {
            "cyclomatic": (cyclomatic, MAX_CYCLOMATIC),
            "cognitive": (cognitive, MAX_COGNITIVE),
            "nesting": (nesting, MAX_NESTING),
            "fan_out": (fan_out, MAX_FAN_OUT),
        }
        exceeded = [
            f"{name}={value} (limit {limit})"
            for name, (value, limit) in observed.items()
            if value > limit
        ]
        if exceeded:
            violations.append(f"{qualified_name}: {', '.join(exceeded)}")

    assert not violations, "god-object diagnostics:\n" + "\n".join(violations)
