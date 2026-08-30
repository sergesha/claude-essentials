"""Structural guardrail for methods confirmed as mixed-responsibility gods."""

from __future__ import annotations

import ast
from pathlib import Path
import warnings

import pytest

from architecture_candidate_policy import evaluate_candidates
from architecture_call_resolver import resolve_calls
from architecture_diagnostics import render_report
from architecture_domain_lifecycle import propagate_semantics
from architecture_legacy_metrics import measure_legacy_metrics
from architecture_manifest_verifier import verify_manifest
from architecture_source_index import build_source_index


SOURCE_ROOT = Path(__file__).parents[2] / "src" / "lockstep"
ARCHITECTURE_TEST_ROOT = Path(__file__).parent

ANALYZER_ROLE_MODULES = frozenset(
    {
        "architecture_source_index",
        "architecture_legacy_metrics",
        "architecture_call_resolver",
        "architecture_domain_lifecycle",
        "architecture_candidate_policy",
        "architecture_manifest_verifier",
        "architecture_diagnostics",
    }
)

ALLOWED_ANALYZER_INTERNAL_IMPORT_EDGES = frozenset(
    {
        ("architecture_legacy_metrics", "architecture_source_index"),
        ("architecture_call_resolver", "architecture_source_index"),
        ("architecture_domain_lifecycle", "architecture_source_index"),
        ("architecture_domain_lifecycle", "architecture_call_resolver"),
        ("architecture_candidate_policy", "architecture_source_index"),
        ("architecture_candidate_policy", "architecture_legacy_metrics"),
        ("architecture_candidate_policy", "architecture_domain_lifecycle"),
        ("architecture_manifest_verifier", "architecture_source_index"),
        ("architecture_manifest_verifier", "architecture_candidate_policy"),
        ("architecture_diagnostics", "architecture_source_index"),
        ("architecture_diagnostics", "architecture_candidate_policy"),
        ("architecture_diagnostics", "architecture_manifest_verifier"),
    }
)

ANALYZER_ROLE_ENTRYPOINTS = {
    "architecture_source_index": build_source_index,
    "architecture_legacy_metrics": measure_legacy_metrics,
    "architecture_call_resolver": resolve_calls,
    "architecture_domain_lifecycle": propagate_semantics,
    "architecture_candidate_policy": evaluate_candidates,
    "architecture_manifest_verifier": verify_manifest,
    "architecture_diagnostics": render_report,
}

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

AUTHORING_FILE_CAPS = {
    "authoring.py": 290,
    "authoring_bundle.py": 225,
    "authoring_capture.py": 175,
    "authoring_compilation.py": 325,
    "authoring_installation.py": 150,
    "authoring_project_tree.py": 225,
    "authoring_publisher.py": 350,
    "authoring_results.py": 200,
}


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


def _authoring_functions() -> tuple[tuple[str, str, ast.AST], ...]:
    found: list[tuple[str, str, ast.AST]] = []
    for path in sorted(SOURCE_ROOT.glob("authoring*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        class LexicalFunctions(ast.NodeVisitor):
            def __init__(self) -> None:
                self.prefix: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.prefix.append(node.name)
                self.generic_visit(node)
                self.prefix.pop()

            def _visit_function(
                self, node: ast.FunctionDef | ast.AsyncFunctionDef
            ) -> None:
                qualified = ".".join((*self.prefix, node.name))
                found.append((str(path.relative_to(SOURCE_ROOT)), qualified, node))
                self.prefix.append(node.name)
                self.generic_visit(node)
                self.prefix.pop()

            visit_FunctionDef = _visit_function
            visit_AsyncFunctionDef = _visit_function

        LexicalFunctions().visit(tree)
    return tuple(found)


def _assert_structural_limits(relative_file: str, qualified_name: str, node: ast.AST) -> None:
    line_count = node.end_lineno - node.lineno + 1
    cyclomatic, cognitive, max_nesting = _complexity(node)
    call_targets = _call_targets(node)
    if line_count >= 80:
        warnings.warn(
            f"{relative_file}:{qualified_name} is {line_count} lines and requires cohesion review",
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
        f"{relative_file}:{qualified_name} remains a structurally overloaded boundary: "
        + ", ".join(violations)
    )


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
    _assert_structural_limits(relative_file, qualified_name, node)


@pytest.mark.parametrize(
    ("relative_file", "qualified_name", "node"),
    _authoring_functions(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_every_authoring_function_has_bounded_structural_complexity(
    relative_file: str, qualified_name: str, node: ast.AST
) -> None:
    _assert_structural_limits(relative_file, qualified_name, node)


def test_authoring_module_population_and_physical_lines_are_bounded() -> None:
    paths = tuple(sorted(SOURCE_ROOT.glob("authoring*.py")))
    assert {path.name for path in paths} == set(AUTHORING_FILE_CAPS)
    counts = {
        path.name: len(path.read_text(encoding="utf-8").splitlines()) for path in paths
    }
    assert {
        name: (count, AUTHORING_FILE_CAPS[name])
        for name, count in counts.items()
        if count > AUTHORING_FILE_CAPS[name]
    } == {}
    assert sum(counts.values()) <= 1_940


def test_authoring_capture_is_the_single_descriptor_observation_owner() -> None:
    """The policy-free descriptor kernel must not retain satellite modules."""

    assert not (SOURCE_ROOT / "authoring_file_observation.py").exists()
    assert not (SOURCE_ROOT / "authoring_limits.py").exists()
    capture = ast.parse(
        (SOURCE_ROOT / "authoring_capture.py").read_text(encoding="utf-8")
    )
    owned_names = {
        member.name
        for member in capture.body
        if isinstance(member, (ast.ClassDef, ast.FunctionDef))
    }
    assert {
        "_DescriptorObservationError",
        "_RegularFileObservation",
        "_observe_regular_descriptor",
    } <= owned_names


def test_authoring_project_tree_has_no_retired_lifecycle_responsibility() -> None:
    """Task 5's lifecycle deletion remains an explicit structural contract."""

    tree = ast.parse(
        (SOURCE_ROOT / "authoring_project_tree.py").read_text(encoding="utf-8")
    )
    methods = {
        member.name
        for owner in tree.body
        if isinstance(owner, ast.ClassDef) and owner.name == "AuthoringProjectTree"
        for member in owner.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert methods.isdisjoint(
        {
            "reserve_path",
            "recover",
            "remove_directory",
            "remove_created_directories",
            "restore",
            "rollback",
        }
    )
    owner = next(
        member
        for member in tree.body
        if isinstance(member, ast.ClassDef) and member.name == "AuthoringProjectTree"
    )
    assert {
        member.name
        for member in owner.body
        if isinstance(member, ast.FunctionDef)
        and (member.name == "__init__" or not member.name.startswith("_"))
    } == {"__init__", "preflight", "ensure_parent", "open_parent"}


def test_analyzer_role_modules_are_the_complete_test_owned_role_set() -> None:
    role_paths = tuple(sorted(ARCHITECTURE_TEST_ROOT.glob("architecture_*.py")))
    assert {path.stem for path in role_paths} == ANALYZER_ROLE_MODULES
    assert {
        role: (entrypoint.__module__, entrypoint.__name__)
        for role, entrypoint in ANALYZER_ROLE_ENTRYPOINTS.items()
    } == {
        "architecture_source_index": ("architecture_source_index", "build_source_index"),
        "architecture_legacy_metrics": (
            "architecture_legacy_metrics",
            "measure_legacy_metrics",
        ),
        "architecture_call_resolver": ("architecture_call_resolver", "resolve_calls"),
        "architecture_domain_lifecycle": (
            "architecture_domain_lifecycle",
            "propagate_semantics",
        ),
        "architecture_candidate_policy": (
            "architecture_candidate_policy",
            "evaluate_candidates",
        ),
        "architecture_manifest_verifier": (
            "architecture_manifest_verifier",
            "verify_manifest",
        ),
        "architecture_diagnostics": ("architecture_diagnostics", "render_report"),
    }


def _public_top_level_function_names(path: Path) -> set[str]:
    module = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }


def test_analyzer_role_modules_have_exactly_one_public_entrypoint() -> None:
    assert {
        role: _public_top_level_function_names(ARCHITECTURE_TEST_ROOT / f"{role}.py")
        for role in ANALYZER_ROLE_MODULES
    } == {
        role: {entrypoint.__name__}
        for role, entrypoint in ANALYZER_ROLE_ENTRYPOINTS.items()
    }


def _analyzer_internal_import_edges(path: Path) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        targets: set[str] = set()
        if isinstance(node, ast.Import):
            targets = {alias.name.split(".", 1)[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                targets = {node.module.split(".", 1)[0]}
            else:
                targets = {alias.name.split(".", 1)[0] for alias in node.names}
        edges.update(
            (path.stem, target) for target in targets if target in ANALYZER_ROLE_MODULES
        )
    return edges


def test_analyzer_import_direction_is_frozen_to_the_specified_role_edges() -> None:
    actual_edges = set().union(
        *(
            _analyzer_internal_import_edges(ARCHITECTURE_TEST_ROOT / f"{role}.py")
            for role in ANALYZER_ROLE_MODULES
        )
    )
    assert actual_edges <= ALLOWED_ANALYZER_INTERNAL_IMPORT_EDGES
