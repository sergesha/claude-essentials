"""Structural guardrail for methods confirmed as mixed-responsibility gods."""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
import hashlib
import json
import operator
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import textwrap
from types import MappingProxyType

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import architecture_call_resolver as call_resolver
import architecture_candidate_policy as candidate_policy
import architecture_domain_lifecycle as domain_lifecycle
import architecture_manifest_verifier as manifest_verifier
from architecture_candidate_policy import evaluate_candidates
from architecture_call_resolver import resolve_calls
from architecture_diagnostics import render_report
from architecture_domain_lifecycle import propagate_semantics
from architecture_legacy_metrics import measure_legacy_metrics
from architecture_manifest_verifier import verify_manifest
from architecture_source_index import build_source_index


SOURCE_ROOT = Path(__file__).parents[2] / "src" / "lockstep"
ENGINE_ROOT = SOURCE_ROOT.parents[1]
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
    ("runtime/effects/_coordinator_reconciliation.py", "_EffectCoordinatorReconciliation.reconcile"),
    ("workflow/_lowering_graph_driver.py", "_LoweringGraphDriver.graph"),
    ("workflow/_lowering_call.py", "_LoweringCall.call"),
    ("workflow/_lowering_call_bundle.py", "_LoweringCallBundle._specialize_child"),
    ("runtime/effects/_coordinator_publication.py", "_EffectCoordinatorPublication._reconcile_publication"),
    ("runtime/effects/ledger.py", "EffectLedger._transition"),
    ("runtime/effects/_coordinator_context.py", "_EffectCoordinatorContext._context"),
    ("runtime/effects/_coordinator_publication_planning.py", "_EffectCoordinatorPublicationPlanning._publication_intent"),
    ("runtime/providers/_codex_supervisor.py", "run"),
    ("runtime/providers/workspaces.py", "LocalGitWorkspaceProvider.quarantine_and_rollover"),
    ("runtime/providers/workspaces.py", "LocalGitWorkspaceProvider.materialize"),
    ("workflow/_lowering_block_dispatch.py", "_LoweringBlockDispatch.block"),
    ("workflow/_lowering_parallel.py", "_LoweringParallel.parallel"),
    ("runtime/providers/_codex_attempt.py", "_CodexAttemptDriver.prepare"),
    ("runtime/effects/_coordinator_admission.py", "_EffectCoordinatorAdmission.submit_acceptance"),
    ("runtime/effects/_coordinator_delivery.py", "_EffectCoordinatorDelivery.deliver_ready"),
    (
        "runtime/_service_effect_drive.py",
        "_ServiceEffectDrive._drive_engine_owned",
    ),
    ("runtime/status.py", "project_status"),
    ("runtime/_service_start.py", "_ServiceStart.start_authorized"),
    ("runtime/effects/_coordinator_admission.py", "_EffectCoordinatorAdmission.submit_manual"),
)

AUTHORING_MODULES = frozenset(
    {
        "authoring.py",
        "authoring_bundle.py",
        "authoring_capture.py",
        "authoring_compilation.py",
        "authoring_installation.py",
        "authoring_project_tree.py",
        "authoring_publisher.py",
        "authoring_results.py",
    }
)


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
    cyclomatic, cognitive, max_nesting = _complexity(node)
    call_targets = _call_targets(node)
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


def _resolver_module_tree() -> ast.Module:
    return ast.parse(
        (ARCHITECTURE_TEST_ROOT / "architecture_call_resolver.py").read_text(
            encoding="utf-8"
        )
    )


def _direct_named_member(owner: ast.AST, name: str) -> ast.AST:
    return next(
        member
        for member in getattr(owner, "body", ())
        if isinstance(member, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and member.name == name
    )


def _resolver_functions() -> tuple[tuple[str, ast.AST], ...]:
    found: list[tuple[str, ast.AST]] = []

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
            qualified_name = ".".join((*self.prefix, node.name))
            found.append((qualified_name, node))
            self.prefix.append(node.name)
            self.generic_visit(node)
            self.prefix.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

    LexicalFunctions().visit(_resolver_module_tree())
    return tuple(found)


_RESOLVER_FUNCTION_CASES = _resolver_functions()


_MUTATOR_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "clear",
        "sort",
        "reverse",
        "update",
        "setdefault",
        "add",
        "discard",
        "difference_update",
        "intersection_update",
        "symmetric_difference_update",
    }
)


def _resolver_class() -> ast.ClassDef:
    owner = _direct_named_member(_resolver_module_tree(), "_Resolver")
    assert isinstance(owner, ast.ClassDef)
    return owner


def _resolver_mutable_fields() -> set[str]:
    fields = {
        node.attr
        for node in ast.walk(_resolver_class())
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and isinstance(node.ctx, (ast.Store, ast.Del))
    }
    fields.update(
        call.func.value.attr
        for call in ast.walk(_resolver_class())
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in _MUTATOR_METHODS
        and isinstance(call.func.value, ast.Attribute)
        and isinstance(call.func.value.value, ast.Name)
        and call.func.value.value.id == "self"
    )
    return fields


def _resolver_one_hop_counts() -> tuple[tuple[str, int], ...]:
    functions = dict(_RESOLVER_FUNCTION_CASES)
    direct: dict[str, set[str]] = {identity: set() for identity in functions}
    for identity, node in functions.items():
        owner = identity.rsplit(".", 1)[0] if "." in identity else ""
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            candidate: str | None = None
            if isinstance(call.func, ast.Name):
                candidate = call.func.id
            elif (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in {"self", "cls"}
            ):
                candidate = f"{owner}.{call.func.attr}"
            if candidate in functions:
                final_name = candidate.rsplit(".", 1)[-1]
                true_dunder = final_name.startswith("__") and final_name.endswith("__")
                if final_name.startswith("_") and not true_dunder:
                    direct[identity].add(candidate)

    counts = []
    for root in functions:
        closure: set[str] = set()
        pending = list(direct[root])
        while pending:
            helper = pending.pop()
            if helper in closure:
                continue
            closure.add(helper)
            pending.extend(direct[helper] - closure)
        changed = True
        while changed:
            changed = False
            for helper in tuple(closure):
                callers = {
                    caller
                    for caller, callees in direct.items()
                    if helper in callees
                }
                if any(caller != root and caller not in closure for caller in callers):
                    closure.remove(helper)
                    changed = True
        counts.append((root, len(closure)))
    return tuple(counts)


_RESOLVER_ONE_HOP_CASES = _resolver_one_hop_counts()


def test_resolver_class_does_not_trigger_method_count_gt_24() -> None:
    """Freezes the class method-count hard adjudication trigger."""

    method_count = sum(
        isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        for member in _resolver_class().body
    )

    assert method_count <= 24, "_Resolver triggers method_count_gt_24"


def test_resolver_class_does_not_trigger_mutable_field_count_gt_24() -> None:
    """Freezes the class mutable-field hard adjudication trigger."""

    assert len(_resolver_mutable_fields()) <= 24, (
        "_Resolver triggers mutable_field_count_gt_24"
    )


def test_resolver_file_does_not_trigger_definition_count_gt_50() -> None:
    """Freezes the file definition-count hard adjudication trigger."""

    tree = _resolver_module_tree()
    definition_count = sum(
        isinstance(member, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for member in ast.walk(tree)
    )

    assert definition_count <= 50, (
        "architecture_call_resolver.py triggers definition_count_gt_50"
    )


@pytest.mark.parametrize(
    ("root", "helper_count"),
    _RESOLVER_ONE_HOP_CASES,
    ids=[root for root, _helper_count in _RESOLVER_ONE_HOP_CASES],
)
def test_resolver_one_hop_does_not_trigger_helper_count_gt_12(
    root: str,
    helper_count: int,
) -> None:
    """Freezes the one-hop helper-reach hard adjudication trigger."""

    assert helper_count <= 12, f"{root} triggers helper_count_gt_12"


@pytest.mark.parametrize(
    ("qualified_name", "node"),
    _RESOLVER_FUNCTION_CASES,
    ids=[qualified_name for qualified_name, _node in _RESOLVER_FUNCTION_CASES],
)
def test_resolver_every_boundary_has_no_function_hard_adjudication_trigger(
    qualified_name: str,
    node: ast.AST,
) -> None:
    """Catches any resolver boundary that requires a hard exception."""

    cyclomatic, cognitive, max_nesting = _complexity(node)
    hard_breaches = {
        "cyclomatic_gt_15": cyclomatic,
        "cognitive_gt_25": cognitive,
        "nesting_gt_4": max_nesting,
        "legacy_syntactic_fanout_gt_24": len(_call_targets(node)),
    }
    limits = {
        "cyclomatic_gt_15": 15,
        "cognitive_gt_25": 25,
        "nesting_gt_4": 4,
        "legacy_syntactic_fanout_gt_24": 24,
    }

    assert {
        trigger: value
        for trigger, value in hard_breaches.items()
        if value > limits[trigger]
    } == {}, qualified_name


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


def test_authoring_module_population_is_exact() -> None:
    paths = tuple(sorted(SOURCE_ROOT.glob("authoring*.py")))
    assert {path.name for path in paths} == AUTHORING_MODULES


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


def _records_named(root: object, record_name: str) -> tuple[object, ...]:
    """Find immutable analyzer records without importing unimplemented classes."""

    found: list[object] = []
    seen: set[int] = set()

    def visit(value: object) -> None:
        if isinstance(value, (str, bytes, int, float, bool, type(None), ast.AST)):
            return
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        if is_dataclass(value) and not isinstance(value, type):
            if type(value).__name__ == record_name:
                found.append(value)
            for field in fields(value):
                visit(getattr(value, field.name))
        elif isinstance(value, Mapping):
            for key, item in value.items():
                visit(key)
                visit(item)
        elif isinstance(value, (tuple, list, set, frozenset)):
            for item in value:
                visit(item)

    visit(root)
    return tuple(found)


def _fixture_index(files: Mapping[str, bytes], tmp_path: Path):
    return build_source_index(tmp_path, tuple(files), files)


def _assert_deeply_immutable(root: object) -> None:
    """Prove the complete public index graph cannot expose mutable state."""

    seen: set[int] = set()

    def visit(value: object) -> None:
        assert not isinstance(value, ast.AST), (
            "public analyzer records must not expose mutable ast.AST"
        )
        if isinstance(value, (type(None), bool, int, float, str, bytes, Path)):
            return
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        if is_dataclass(value) and not isinstance(value, type):
            record_fields = fields(value)
            assert type(value).__dataclass_params__.frozen
            with pytest.raises((AttributeError, TypeError)):
                setattr(
                    value,
                    record_fields[0].name if record_fields else "_immutability_probe",
                    object(),
                )
            for field in record_fields:
                visit(getattr(value, field.name))
            return
        if isinstance(value, Mapping):
            probe_key = next(iter(value), object())
            probe_value = value[probe_key] if probe_key in value else object()
            with pytest.raises(TypeError):
                operator.setitem(value, probe_key, probe_value)
            for key, item in value.items():
                visit(key)
                visit(item)
            return
        if isinstance(value, frozenset):
            for item in value:
                visit(item)
            return
        assert isinstance(value, tuple), (
            f"ordered index collections must be tuples, got {type(value).__name__}"
        )
        for item in value:
            visit(item)

    visit(root)


@dataclass(frozen=True, slots=True)
class _LegacyEntityFixture:
    identity: str
    source: bytes


@dataclass(frozen=True, slots=True)
class _LegacyIndexFixture:
    entities: Mapping[str, _LegacyEntityFixture]


def test_source_index_covers_every_tracked_python_file() -> None:
    """Catches filesystem scans that omit a tracked package file or add an untracked one."""

    completed = subprocess.run(
        ["git", "ls-files", "src/lockstep/**/*.py", "src/lockstep/*.py"],
        cwd=ENGINE_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    tracked_paths = tuple(sorted(filter(None, completed.stdout.splitlines())))
    assert tracked_paths

    index = build_source_index(ENGINE_ROOT, tracked_paths)

    assert tuple(index.files) == tracked_paths
    assert all(index.files[path] == (ENGINE_ROOT / path).read_bytes() for path in tracked_paths)
    assert set(index.file_sha256) == set(tracked_paths)
    _assert_deeply_immutable(index)


@pytest.mark.parametrize("first_key", ("src/lockstep/one.py", r"src\lockstep\one.py"))
def test_source_index_accepts_an_exact_supplied_snapshot(
    tmp_path: Path, first_key: str
) -> None:
    paths = ("src/lockstep/one.py", "src/lockstep/two.py")
    files = {first_key: b"one = 1\n", paths[1]: b"two = 2\n"}

    index = build_source_index(tmp_path, paths, files)

    assert dict(index.files) == {paths[0]: b"one = 1\n", paths[1]: b"two = 2\n"}


@pytest.mark.parametrize(
    "kind",
    ("normalized_collision", "missing", "extra", "extra_non_bytes", "substitution"),
)
def test_source_index_rejects_an_inexact_supplied_snapshot(
    tmp_path: Path, kind: str
) -> None:
    paths = ("src/lockstep/one.py", "src/lockstep/two.py")
    files: dict[str, object] = {path: b"pass\n" for path in paths}
    if kind == "normalized_collision":
        files[r"src\lockstep\one.py"] = b"one = 2\n"
        error, message = ValueError, "duplicate normalized supplied path: src/lockstep/one.py"
    elif kind in {"missing", "substitution"}:
        del files[paths[1]]
        if kind == "substitution":
            files["src/lockstep/extra.py"] = b"extra = 3\n"
            message = (
                "supplied files mismatch: missing src/lockstep/two.py; "
                "extra src/lockstep/extra.py"
            )
        else:
            message = "supplied files missing tracked paths: src/lockstep/two.py"
        error = ValueError
    else:
        files["src/lockstep/extra.py"] = (
            b"extra = 3\n" if kind == "extra" else "not bytes"
        )
        error, message = (
            (ValueError, "supplied files contain untracked paths: src/lockstep/extra.py")
            if kind == "extra"
            else (TypeError, "source bytes required for src/lockstep/extra.py")
        )

    with pytest.raises(error) as caught:
        build_source_index(tmp_path, paths, files)

    assert str(caught.value) == message


def test_source_index_identity_and_containment_follow_lexical_ast_order(
    tmp_path: Path,
) -> None:
    """Catches basename identities, flattened nesting, and source-order sorting."""

    files = {
        "src/lockstep/zeta.py": b"def last():\n    pass\n",
        "src/lockstep/alpha.py": (
            b"def outer():\n"
            b"    class Inner:\n"
            b"        def method(self):\n"
            b"            pass\n"
            b"    def nested():\n"
            b"        pass\n"
            b"    async def asynchronous():\n"
            b"        pass\n"
            b"class Top:\n"
            b"    def method(self):\n"
            b"        pass\n"
        ),
    }

    index = build_source_index(tmp_path, tuple(files), files)
    entities = _records_named(index, "Entity")

    assert [(entity.identity, entity.parent) for entity in entities] == [
        ("src/lockstep/alpha.py::outer", "src/lockstep/alpha.py::@file"),
        ("src/lockstep/alpha.py::outer.Inner", "src/lockstep/alpha.py::outer"),
        (
            "src/lockstep/alpha.py::outer.Inner.method",
            "src/lockstep/alpha.py::outer.Inner",
        ),
        ("src/lockstep/alpha.py::outer.nested", "src/lockstep/alpha.py::outer"),
        (
            "src/lockstep/alpha.py::outer.asynchronous",
            "src/lockstep/alpha.py::outer",
        ),
        ("src/lockstep/alpha.py::Top", "src/lockstep/alpha.py::@file"),
        ("src/lockstep/alpha.py::Top.method", "src/lockstep/alpha.py::Top"),
        ("src/lockstep/zeta.py::last", "src/lockstep/zeta.py::@file"),
    ]
    assert all(type(entity).__name__ == "Entity" for entity in entities)
    _assert_deeply_immutable(index)


def test_source_index_rejects_duplicate_stable_identity(tmp_path: Path) -> None:
    """Catches occurrence suffixes that hide ambiguous runtime shadowing."""

    path = "src/lockstep/duplicate.py"
    source = b"def repeated():\n    pass\ndef repeated():\n    pass\n"

    with pytest.raises(ValueError, match="duplicate.*identity"):
        _fixture_index({path: source}, tmp_path)


def test_source_span_includes_decorators_and_hashes_exact_crlf_bytes(
    tmp_path: Path,
) -> None:
    """Catches def-line spans and newline-normalized source digests."""

    path = "src/lockstep/decorated.py"
    source = (
        b"# header\r\n"
        b"@first\r\n"
        b"@second('x')\r\n"
        b"def decorated(value):\r\n"
        b"    return value\r\n"
        b"@class_decorator\r\n"
        b"class Decorated:\r\n"
        b"    pass\r\n"
        b"@async_decorator\r\n"
        b"async def async_decorated(value):\r\n"
        b"    return value\r\n"
        b"tail = 1\r\n"
    )
    lines = source.splitlines(keepends=True)
    expected_spans = (
        ((2, 5), b"".join(lines[1:5])),
        ((6, 8), b"".join(lines[5:8])),
        ((9, 11), b"".join(lines[8:11])),
    )

    index = _fixture_index({path: source}, tmp_path)
    spans = tuple(entity.span for entity in _records_named(index, "Entity"))

    assert all(type(span).__name__ == "SourceSpan" for span in spans)
    assert [
        ((span.start_line, span.end_line), span.sha256) for span in spans
    ] == [
        (coordinates, hashlib.sha256(span_bytes).hexdigest())
        for coordinates, span_bytes in expected_spans
    ]
    assert index.file_sha256[path] == hashlib.sha256(source).hexdigest()
    assert index.files[path] == source
    assert index.files[path].count(b"\r\n") == 12
    _assert_deeply_immutable(index)


def _alias_pairs(record: object) -> tuple[tuple[str, str | None], ...]:
    return tuple(
        (
            alias["name"] if isinstance(alias, Mapping) else alias.name,
            alias["asname"] if isinstance(alias, Mapping) else alias.asname,
        )
        for alias in record.aliases
    )


def test_source_index_import_records_follow_complete_file_ast_order(
    tmp_path: Path,
) -> None:
    """Catches top-level-only import scans and owner-local ordinal resets."""

    path = "src/lockstep/imports.py"
    source = (
        b"import zed as z, alpha\r\n"
        b"def outer():\r\n"
        b"    from . import local as alias\r\n"
        b"    def nested():\r\n"
        b"        import deeply.nested\r\n"
        b"class Box:\r\n"
        b"    from package import thing as renamed, other\r\n"
    )
    lines = source.splitlines(keepends=True)
    expected_import_bytes = (lines[0], lines[2], lines[4], lines[6])

    index = _fixture_index({path: source}, tmp_path)
    imports = _records_named(index, "ImportRecord")

    assert [record.identity for record in imports] == [
        f"{path}::import:0001",
        f"{path}::import:0002",
        f"{path}::import:0003",
        f"{path}::import:0004",
    ]
    assert [record.owner for record in imports] == [
        f"{path}::@file",
        f"{path}::outer",
        f"{path}::outer.nested",
        f"{path}::Box",
    ]
    assert [(record.kind, record.module, record.level) for record in imports] == [
        ("import", None, 0),
        ("from", None, 1),
        ("import", None, 0),
        ("from", "package", 0),
    ]
    assert [_alias_pairs(record) for record in imports] == [
        (("zed", "z"), ("alpha", None)),
        (("local", "alias"),),
        (("deeply.nested", None),),
        (("thing", "renamed"), ("other", None)),
    ]
    expected_targets = (
        ("zed", "alpha"),
        (".local",),
        ("deeply.nested",),
        ("package.thing", "package.other"),
    )
    assert tuple(record.targets for record in imports) == expected_targets
    assert [record.span_sha256 for record in imports] == [
        hashlib.sha256(statement).hexdigest() for statement in expected_import_bytes
    ]
    assert {field.name for field in fields(imports[0])} == {
        "identity",
        "owner",
        "kind",
        "module",
        "level",
        "aliases",
        "targets",
        "span_sha256",
        "import_semantic_sha256",
    }
    for record, targets in zip(imports, expected_targets, strict=True):
        payload = {
            "identity": record.identity,
            "owner": record.owner,
            "kind": record.kind,
            "module": record.module,
            "level": record.level,
            "aliases": [
                {"name": name, "asname": asname}
                for name, asname in _alias_pairs(record)
            ],
            "targets": list(targets),
            "span_sha256": record.span_sha256,
        }
        expected_digest = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        assert record.import_semantic_sha256 == expected_digest
    _assert_deeply_immutable(index)


def test_lambda_attribution_is_function_then_class_then_file_and_class_evidence(
    tmp_path: Path,
) -> None:
    """Catches lambdas becoming entities or leaking to a broader lexical owner."""

    path = "src/lockstep/lambdas.py"
    source = (
        b"def function_owner():\n"
        b"    first = lambda: function_call()\n"
        b"    nested = lambda: (lambda: nested_call())\n"
        b"class Box:\n"
        b"    class_owned = lambda self: self.class_call()\n"
        b"    def method(self):\n"
        b"        method_owned = lambda: self.method_call()\n"
        b"    second_class_owned = lambda self: self.other_call()\n"
        b"file_owned = lambda: file_call()\n"
    )

    index = _fixture_index({path: source}, tmp_path)
    owner_values = tuple(index.lambda_owners.values())

    assert owner_values == (
        f"{path}::function_owner",
        f"{path}::function_owner",
        f"{path}::function_owner",
        f"{path}::Box",
        f"{path}::Box.method",
        f"{path}::Box",
        f"{path}::@file",
    )
    assert index.class_lambda_evidence == {
        f"{path}::Box": ("@lambda:0001", "@lambda:0002")
    }
    assert tuple(
        entity.identity for entity in _records_named(index, "Entity")
    ) == (
        f"{path}::function_owner",
        f"{path}::Box",
        f"{path}::Box.method",
    )
    _assert_deeply_immutable(index)


def test_source_index_accepts_9999_imports_and_rejects_import_10000(
    tmp_path: Path,
) -> None:
    """Catches off-by-one or five-digit file-global import identities."""

    path = "src/lockstep/import_overflow.py"
    accepted_source = ("import os\n" * 9_999).encode("utf-8")
    accepted = _fixture_index({path: accepted_source}, tmp_path)
    assert _records_named(accepted, "ImportRecord")[-1].identity == (
        f"{path}::import:9999"
    )
    _assert_deeply_immutable(accepted)

    with pytest.raises(ValueError, match=r"import.*9,?999|9,?999.*import"):
        _fixture_index({path: accepted_source + b"import os\n"}, tmp_path)


def test_source_index_legacy_metrics_split_identity_at_final_separator(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/a::b.py"
    identity = f"{path}::f"

    metrics = measure_legacy_metrics(
        _fixture_index({path: b"def f():\n    return helper()\n"}, tmp_path)
    )

    assert tuple(metrics) == (identity,)
    metric = metrics[identity]
    assert (
        metric.cyclomatic,
        metric.cognitive,
        metric.max_nesting,
        metric.legacy_syntactic_fanout,
    ) == (1, 0, 0, 1)


def test_legacy_metrics_characterize_current_complexity_and_pruned_fanout() -> None:
    """Catches metric drift and nested-scope complexity/fan-out inflation."""

    path = "src/lockstep/legacy_fixture.py"
    source = (
        b"def parent(flag, items):\n"
        b"    if flag and ready():\n"
        b"        for item in items:\n"
        b"            if check(item):\n"
        b"                act(item)\n"
        b"            else:\n"
        b"                skip(item)\n"
        b"        else:\n"
        b"            finish()\n"
        b"    try:\n"
        b"        work()\n"
        b"    except ValueError:\n"
        b"        recover()\n"
        b"    def duplicate():\n"
        b"        while condition():\n"
        b"            one()\n"
        b"            if deeper():\n"
        b"                two()\n"
        b"    class Nested:\n"
        b"        def duplicate(self):\n"
        b"            if gate():\n"
        b"                inside()\n"
        b"    hidden = lambda: (lambda_call(), lambda_other())\n"
        b"    return helper()\n"
        b"async def branch_forms(flag, items, async_items):\n"
        b"    if flag:\n"
        b"        pass\n"
        b"    for item in items:\n"
        b"        continue\n"
        b"    async for item in async_items:\n"
        b"        break\n"
        b"    while flag:\n"
        b"        break\n"
        b"    try:\n"
        b"        pass\n"
        b"    except ValueError:\n"
        b"        pass\n"
        b"    match flag:\n"
        b"        case True:\n"
        b"            pass\n"
        b"    if flag and ready() and other():\n"
        b"        pass\n"
    )
    identities = (
        f"{path}::branch_forms",
        f"{path}::parent.Nested.duplicate",
        f"{path}::parent",
        f"{path}::parent.duplicate",
    )
    index = _LegacyIndexFixture(
        entities=MappingProxyType({
            identity: _LegacyEntityFixture(identity, source) for identity in identities
        })
    )

    metrics = measure_legacy_metrics(index)

    metric_fields = (
        "cyclomatic",
        "cognitive",
        "max_nesting",
        "legacy_syntactic_fanout",
    )
    assert {
        identity: tuple(getattr(metric, name) for name in metric_fields)
        for identity, metric in metrics.items()
    } == {
        f"{path}::parent": (7, 10, 3, 8),
        f"{path}::parent.duplicate": (3, 3, 2, 4),
        f"{path}::parent.Nested.duplicate": (2, 1, 1, 2),
        f"{path}::branch_forms": (11, 14, 2, 2),
    }
    assert {field.name for field in fields(next(iter(metrics.values())))} == set(
        metric_fields
    )
    assert all(type(metric).__name__ == "LegacyMetrics" for metric in metrics.values())
    _assert_deeply_immutable(next(iter(metrics.values())))
    with pytest.raises(TypeError):
        metrics[f"{path}::parent"] = next(iter(metrics.values()))


def _resolver_source(source: str) -> bytes:
    return textwrap.dedent(source).lstrip("\n").encode("utf-8")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolver_owner_node(index: object, owner: str) -> ast.AST:
    path, separator, qualified = owner.rpartition("::")
    assert separator
    current: ast.AST = ast.parse(index.files[path], filename=path)
    if qualified == "@file":
        return current
    for name in qualified.split("."):
        current = next(
            member
            for member in getattr(current, "body", ())
            if isinstance(member, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name == name
        )
    return current


def _resolver_owner_calls(index: object, owner: str) -> tuple[ast.Call, ...]:
    root = _resolver_owner_node(index, owner)
    calls: list[ast.Call] = []

    class OwnerPreorder(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            calls.append(node)
            self.generic_visit(node)

        def _visit_named(self, node: ast.AST) -> None:
            if node is not root:
                return
            if isinstance(node, ast.ClassDef):
                for decorator in node.decorator_list:
                    self.visit(decorator)
                for base in node.bases:
                    self.visit(base)
                for keyword in node.keywords:
                    self.visit(keyword)
                for statement in node.body:
                    self.visit(statement)
                return
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in node.decorator_list:
                self.visit(decorator)
            self.visit(node.args)
            if node.returns is not None:
                self.visit(node.returns)
            for statement in node.body:
                self.visit(statement)

        visit_FunctionDef = _visit_named
        visit_AsyncFunctionDef = _visit_named
        visit_ClassDef = _visit_named

    OwnerPreorder().visit(root)
    return tuple(calls)


def _primitive_table(index: object, rows: tuple[Mapping[str, object], ...]) -> Mapping[str, object]:
    population = [
        {"path": path, "source_sha256": index.file_sha256[path]}
        for path in sorted(index.files)
    ]
    evidence = []
    for row in rows:
        if row.get("selector_kind") != "callsite":
            continue
        selector = row["selector"]
        assert isinstance(selector, str)
        owner, ordinal_text = selector.rsplit("::call:", 1)
        call = _resolver_owner_calls(index, owner)[int(ordinal_text) - 1]
        if owner.endswith("::@file"):
            path, separator, _qualified = owner.rpartition("::")
            assert separator
            owner_source_sha256 = index.file_sha256[path]
        else:
            owner_source_sha256 = index.entities[owner].span.sha256
        evidence.append(
            {
                "selector": selector,
                "owner_source_sha256": owner_source_sha256,
                "call_ast_sha256": hashlib.sha256(
                    ast.dump(call, include_attributes=False).encode("utf-8")
                ).hexdigest(),
            }
        )
    return {
        "schema_version": 1,
        "reference_source_sha256": _canonical_sha256(population),
        "callsite_evidence": evidence,
        "rows": [dict(row) for row in rows],
    }


def _resolver_fixture(
    tmp_path: Path,
    source: str,
    *,
    path: str = "src/lockstep/resolver_fixture.py",
    extra_files: Mapping[str, str] | None = None,
    allowlist: object = (),
    primitives: object = (),
):
    files = {path: _resolver_source(source)}
    files.update(
        {
            extra_path: _resolver_source(extra_source)
            for extra_path, extra_source in (extra_files or {}).items()
        }
    )
    index = _fixture_index(files, tmp_path)
    return resolve_calls(index, allowlist, primitives)


def _resolver_fixture_with_primitive_rows(
    tmp_path: Path,
    source: str,
    rows: tuple[Mapping[str, object], ...],
    *,
    path: str = "src/lockstep/resolver_fixture.py",
    extra_files: Mapping[str, str] | None = None,
    allowlist: object = (),
):
    files = {path: _resolver_source(source)}
    files.update(
        {
            extra_path: _resolver_source(extra_source)
            for extra_path, extra_source in (extra_files or {}).items()
        }
    )
    index = _fixture_index(files, tmp_path)
    return resolve_calls(index, allowlist, _primitive_table(index, rows))


def _resolver_calls(result: object) -> Mapping[str, object]:
    calls = result.calls
    assert isinstance(calls, Mapping)
    assert all(key == record.callsite for key, record in calls.items())
    return calls


def _resolver_target(result: object, callsite: str) -> str:
    record = _resolver_calls(result)[callsite]
    assert type(record).__name__ == "ResolvedCall"
    return record.target


def _assert_unresolved_call(result: object, callsite: str) -> object:
    record = _resolver_calls(result)[callsite]
    assert type(record).__name__ == "UnresolvedCall"
    return record


def _resolver_dependencies(result: object) -> Mapping[str, object]:
    dependencies = result.dependencies
    assert isinstance(dependencies, Mapping)
    assert all(
        key == record.reference for key, record in dependencies.items()
    )
    return dependencies


def _resolver_dependency_target(result: object, reference: str) -> str:
    record = _resolver_dependencies(result)[reference]
    assert type(record).__name__ == "ResolvedDependency"
    return record.target


def _assert_unresolved_dependency(result: object, reference: str) -> object:
    record = _resolver_dependencies(result)[reference]
    assert type(record).__name__ == "UnresolvedDependency"
    return record


def _primitive_callsite_row(callsite: str, semantic_target: str) -> Mapping[str, object]:
    return {
        "selector_kind": "callsite",
        "selector": callsite,
        "semantic_target": semantic_target,
        "domains": ["external-process/provider"],
    }


_EFFECT_DOMAINS = (
    "decode/validate",
    "planning/transformation",
    "filesystem-read",
    "filesystem-write",
    "durable-state",
    "synchronization",
    "external-process/provider",
    "authority/commitment",
    "lifecycle-control",
    "projection/output",
)


def _primitive_entity_row(
    selector: str,
    domains: tuple[str, ...] = ("external-process/provider",),
) -> Mapping[str, object]:
    return {
        "selector_kind": "entity",
        "selector": selector,
        "semantic_target": selector,
        "domains": list(domains),
    }


def test_resolver_callsite_owners_follow_preorder_pruning_and_lambda_attribution(
    tmp_path: Path,
) -> None:
    """Catches body-first traversal, nested-owner leakage, and dropped lambdas."""

    path = "src/lockstep/owners.py"
    result = _resolver_fixture(
        tmp_path,
        """
        @decorate(decorator_argument())
        def owner(value: annotation() = default()):
            body()
            local_lambda = lambda: lambda_body()
            def nested():
                nested_body()
            class Nested:
                class_owned = lambda: nested_class_lambda()
            return final()

        class Box:
            class_owned = lambda: class_lambda()

        file_owned = lambda: file_lambda()
        file_call()
        """,
        path=path,
    )

    calls = _resolver_calls(result)
    expected_by_owner = {
        f"{path}::owner": (
            "Call(func=Name(id='decorate', ctx=Load()), args=[Call(func=Name(id='decorator_argument', ctx=Load()), args=[], keywords=[])], keywords=[])",
            "Call(func=Name(id='decorator_argument', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='annotation', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='default', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='body', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='lambda_body', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='final', ctx=Load()), args=[], keywords=[])",
        ),
        f"{path}::owner.nested": (
            "Call(func=Name(id='nested_body', ctx=Load()), args=[], keywords=[])",
        ),
        f"{path}::owner.Nested": (
            "Call(func=Name(id='nested_class_lambda', ctx=Load()), args=[], keywords=[])",
        ),
        f"{path}::Box": (
            "Call(func=Name(id='class_lambda', ctx=Load()), args=[], keywords=[])",
        ),
        f"{path}::@file": (
            "Call(func=Name(id='file_lambda', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='file_call', ctx=Load()), args=[], keywords=[])",
        ),
    }
    expected_calls = {
        f"{owner}::call:{ordinal:04d}"
        for owner, dumps in expected_by_owner.items()
        for ordinal in range(1, len(dumps) + 1)
    }
    assert set(calls) == expected_calls
    for owner, expected_dumps in expected_by_owner.items():
        assert tuple(
            calls[f"{owner}::call:{ordinal:04d}"].ast_dump
            for ordinal in range(1, len(expected_dumps) + 1)
        ) == expected_dumps
def test_resolver_async_and_class_owner_preorder_covers_every_indexed_root(
    tmp_path: Path,
) -> None:
    """Freezes decorators/signatures/bases/body order and named-owner pruning."""

    path = "src/lockstep/indexed_owner_roots.py"
    result = _resolver_fixture(
        tmp_path,
        """
        @async_decorator(async_decorator_argument())
        async def async_owner(
            positional: positional_annotation() = positional_default(),
            /,
            regular: regular_annotation() = regular_default(),
            *values: vararg_annotation(),
            keyword: keyword_annotation() = keyword_default(),
            **options: kwarg_annotation(),
        ) -> return_annotation():
            async_body()
            def nested_function():
                nested_function_body()
            class NestedClass:
                nested_class_body()

        @class_decorator(class_decorator_argument())
        class ClassOwner(
            base_factory(base_argument()),
            metaclass=metaclass_factory(metaclass_argument()),
        ):
            class_body()
            def nested_method(self):
                nested_method_body()
        """,
        path=path,
    )

    calls = _resolver_calls(result)
    expected_by_owner = {
        f"{path}::async_owner": (
            "Call(func=Name(id='async_decorator', ctx=Load()), args=[Call(func=Name(id='async_decorator_argument', ctx=Load()), args=[], keywords=[])], keywords=[])",
            "Call(func=Name(id='async_decorator_argument', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='positional_annotation', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='regular_annotation', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='vararg_annotation', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='keyword_annotation', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='keyword_default', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='kwarg_annotation', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='positional_default', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='regular_default', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='return_annotation', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='async_body', ctx=Load()), args=[], keywords=[])",
        ),
        f"{path}::async_owner.nested_function": (
            "Call(func=Name(id='nested_function_body', ctx=Load()), args=[], keywords=[])",
        ),
        f"{path}::async_owner.NestedClass": (
            "Call(func=Name(id='nested_class_body', ctx=Load()), args=[], keywords=[])",
        ),
        f"{path}::ClassOwner": (
            "Call(func=Name(id='class_decorator', ctx=Load()), args=[Call(func=Name(id='class_decorator_argument', ctx=Load()), args=[], keywords=[])], keywords=[])",
            "Call(func=Name(id='class_decorator_argument', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='base_factory', ctx=Load()), args=[Call(func=Name(id='base_argument', ctx=Load()), args=[], keywords=[])], keywords=[])",
            "Call(func=Name(id='base_argument', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='metaclass_factory', ctx=Load()), args=[Call(func=Name(id='metaclass_argument', ctx=Load()), args=[], keywords=[])], keywords=[])",
            "Call(func=Name(id='metaclass_argument', ctx=Load()), args=[], keywords=[])",
            "Call(func=Name(id='class_body', ctx=Load()), args=[], keywords=[])",
        ),
        f"{path}::ClassOwner.nested_method": (
            "Call(func=Name(id='nested_method_body', ctx=Load()), args=[], keywords=[])",
        ),
    }
    assert set(calls) == {
        f"{owner}::call:{ordinal:04d}"
        for owner, dumps in expected_by_owner.items()
        for ordinal in range(1, len(dumps) + 1)
    }
    for owner, expected_dumps in expected_by_owner.items():
        assert tuple(
            calls[f"{owner}::call:{ordinal:04d}"].ast_dump
            for ordinal in range(1, len(expected_dumps) + 1)
        ) == expected_dumps


def test_resolver_accepts_9999_calls_and_rejects_call_10000_per_owner(
    tmp_path: Path,
) -> None:
    """Catches off-by-one and five-digit per-owner callsite ordinals."""

    path = "src/lockstep/call_limit.py"
    accepted_source = "def owner():\n" + "    unknown()\n" * 9_999
    accepted = _resolver_fixture(tmp_path, accepted_source, path=path)
    assert tuple(_resolver_calls(accepted))[-1] == f"{path}::owner::call:9999"

    with pytest.raises(ValueError, match=r"call.*9,?999|9,?999.*call"):
        _resolver_fixture(
            tmp_path,
            accepted_source + "    unknown()\n",
            path=path,
        )


def test_resolver_callsite_limit_is_per_owner_not_file_or_index(
    tmp_path: Path,
) -> None:
    """Catches a shared counter that rejects more than 9,999 calls in total."""

    path = "src/lockstep/multi_owner_limit.py"
    source = (
        "def first():\n"
        + "    unknown()\n" * 5_000
        + "def second():\n"
        + "    unknown()\n" * 5_000
    )
    result = _resolver_fixture(tmp_path, source, path=path)
    calls = _resolver_calls(result)

    assert len(calls) == 10_000
    assert f"{path}::first::call:5000" in calls
    assert f"{path}::second::call:5000" in calls


def test_resolver_exact_name_import_module_class_decorator_and_base_binding(
    tmp_path: Path,
) -> None:
    """Catches fuzzy names, import-label drift, and ignored decorator/base scopes."""

    path = "src/lockstep/exact_bindings.py"
    result = _resolver_fixture_with_primitive_rows(
        tmp_path,
        """
        from package import external as renamed
        from package import decorate as dec
        import package.module as module

        def local():
            pass

        class Base:
            def inherited(self):
                pass

        @dec()
        class Worker(Base):
            def run(self, items):
                local()
                renamed()
                module.work()
                Worker.run()
                len(items)

            def inherited_call(self):
                self.inherited()
        """,
        (
            _primitive_entity_row("package.decorate", ("planning/transformation",)),
            _primitive_entity_row("package.external"),
            _primitive_entity_row("package.module.work"),
        ),
        path=path,
        allowlist=frozenset({"builtins.len"}),
    )

    assert _resolver_target(result, f"{path}::Worker::call:0001") == "package.decorate"
    assert [
        _resolver_target(result, f"{path}::Worker.run::call:{ordinal:04d}")
        for ordinal in range(1, 6)
    ] == [
        f"{path}::local",
        "package.external",
        "package.module.work",
        f"{path}::Worker.run",
        "builtins.len",
    ]
    assert _resolver_target(
        result, f"{path}::Worker.inherited_call::call:0001"
    ) == f"{path}::Base.inherited"


_RELATIVE_IMPORT_CASES = (
    (
        "current_package_symbol",
        "src/lockstep/pkg/sub/consumer.py",
        "from .dependency import target",
        "target()",
        "src/lockstep/pkg/sub/dependency.py",
        "src/lockstep/pkg/sub/dependency.py::target",
    ),
    (
        "parent_package_symbol",
        "src/lockstep/pkg/sub/consumer.py",
        "from ..dependency import target",
        "target()",
        "src/lockstep/pkg/dependency.py",
        "src/lockstep/pkg/dependency.py::target",
    ),
    (
        "relative_only_module",
        "src/lockstep/pkg/sub/consumer.py",
        "from . import dependency",
        "dependency.target()",
        "src/lockstep/pkg/sub/dependency.py",
        "src/lockstep/pkg/sub/dependency.py::target",
    ),
)


@pytest.mark.parametrize(
    ("case", "path", "statement", "expression", "dependency_path", "expected"),
    _RELATIVE_IMPORT_CASES,
    ids=[case for case, *_rest in _RELATIVE_IMPORT_CASES],
)
def test_resolver_binding_normalizes_relative_import_from_package_and_level(
    tmp_path: Path,
    case: str,
    path: str,
    statement: str,
    expression: str,
    dependency_path: str,
    expected: str,
) -> None:
    """Catches external-label drift from discarding ImportFrom.level."""

    result = _resolver_fixture(
        tmp_path,
        f"{statement}\ndef owner():\n    {expression}\n",
        path=path,
        extra_files={dependency_path: "def target():\n    pass\n"},
    )

    assert _resolver_target(result, f"{path}::owner::call:0001") == expected, case


def test_resolver_binding_requires_an_actual_indexed_imported_member(
    tmp_path: Path,
) -> None:
    """Catches synthesizing an internal entity merely from an imported name."""

    path = "src/lockstep/imported_member.py"
    result = _resolver_fixture(
        tmp_path,
        """
        from lockstep.peer import missing, actual
        def owner():
            missing()
            actual()
        """,
        path=path,
        extra_files={
            "src/lockstep/peer.py": "def actual():\n    pass\n",
        },
    )

    assert _resolver_target(result, f"{path}::owner::call:0002") == (
        "src/lockstep/peer.py::actual"
    )
    _assert_unresolved_call(result, f"{path}::owner::call:0001")


def test_resolver_binding_evaluates_lambda_defaults_in_the_enclosing_frame(
    tmp_path: Path,
) -> None:
    """Catches dropped defaults or defaults evaluated in the lambda frame."""

    path = "src/lockstep/lambda_defaults.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def target():
            pass
        def owner():
            callback = lambda target=target(), *, keyword=target(): target()
        """,
        path=path,
    )

    assert [
        _resolver_target(result, f"{path}::owner::call:{ordinal:04d}")
        for ordinal in (1, 2)
    ] == [f"{path}::target", f"{path}::target"]
    _assert_unresolved_call(result, f"{path}::owner::call:0003")


def test_resolver_lambda_callsite_preorder_matches_owner_preorder_at_the_limit(
    tmp_path: Path,
) -> None:
    """Catches positional-default-first lambda traversal and ordinal drift."""

    path = "src/lockstep/lambda_owner_preorder.py"
    prefix = _resolver_source(
        """
        def keyword_default():
            pass
        def positional_default():
            pass
        def lambda_body():
            pass
        def owner():
            callback = lambda value=positional_default(), *, named=keyword_default(): lambda_body()
        """
    ).decode("utf-8")
    accepted_source = prefix + "    unknown()\n" * 9_996
    accepted = _resolver_fixture(tmp_path, accepted_source, path=path)
    calls = _resolver_calls(accepted)

    assert tuple(calls)[:3] == (
        f"{path}::owner::call:0001",
        f"{path}::owner::call:0002",
        f"{path}::owner::call:0003",
    )
    assert tuple(calls)[-1] == f"{path}::owner::call:9999"
    with pytest.raises(ValueError, match=r"^owner exceeds 9,999 callsites: "):
        _resolver_fixture(
            tmp_path,
            accepted_source + "    unknown()\n",
            path=path,
        )
    assert [
        _resolver_target(accepted, f"{path}::owner::call:{ordinal:04d}")
        for ordinal in (1, 2, 3)
    ] == [
        f"{path}::keyword_default",
        f"{path}::positional_default",
        f"{path}::lambda_body",
    ]


def test_resolver_binding_treats_match_pattern_capture_as_conditional_local(
    tmp_path: Path,
) -> None:
    """Catches a MatchAs string capture falling through to an outer binding."""

    path = "src/lockstep/match_capture.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def captured():
            pass
        def owner(subject):
            match subject:
                case {"value": captured}:
                    pass
            captured()
        """,
        path=path,
    )

    _assert_unresolved_call(result, f"{path}::owner::call:0001")


_MATCH_STRING_CAPTURE_CASES = (
    ("star", "[*captured]"),
    ("mapping_rest", "{**captured}"),
    ("nested_as", "(1 | 2) as captured"),
)


@pytest.mark.parametrize(
    ("case", "pattern"),
    _MATCH_STRING_CAPTURE_CASES,
    ids=[case for case, *_rest in _MATCH_STRING_CAPTURE_CASES],
)
def test_resolver_binding_treats_every_string_pattern_capture_as_local(
    tmp_path: Path,
    case: str,
    pattern: str,
) -> None:
    """Catches MatchStar, MatchMapping.rest, and nested MatchAs omissions."""

    path = f"src/lockstep/match_string_capture_{case}.py"
    result = _resolver_fixture(
        tmp_path,
        (
            "def captured():\n"
            "    pass\n"
            "def owner(subject):\n"
            "    match subject:\n"
            f"        case {pattern}:\n"
            "            pass\n"
            "    captured()\n"
        ),
        path=path,
    )

    _assert_unresolved_call(result, f"{path}::owner::call:0001")


_COMPREHENSION_SCOPE_CASES = (
    ("list", "[target() for target in values]"),
    ("set", "{target() for target in values}"),
    ("dict", "{target(): value for target, value in values}"),
    ("generator", "(target() for target in values)"),
)


@pytest.mark.parametrize(
    ("case", "expression"),
    _COMPREHENSION_SCOPE_CASES,
    ids=[case for case, *_rest in _COMPREHENSION_SCOPE_CASES],
)
def test_resolver_binding_isolates_comprehension_target_frame(
    tmp_path: Path,
    case: str,
    expression: str,
) -> None:
    """Catches both target fall-through inside and target leakage outside."""

    path = f"src/lockstep/comprehension_{case}.py"
    result = _resolver_fixture(
        tmp_path,
        (
            "def target():\n"
            "    pass\n"
            "def owner(values):\n"
            f"    {expression}\n"
            "    target()\n"
        ),
        path=path,
    )

    _assert_unresolved_call(result, f"{path}::owner::call:0001")
    assert _resolver_target(result, f"{path}::owner::call:0002") == f"{path}::target"


def test_resolver_binding_evaluates_comprehension_outer_iterable_in_enclosing_frame(
    tmp_path: Path,
) -> None:
    """Catches applying the comprehension target to its outermost iterable."""

    path = "src/lockstep/comprehension_outer_iterable.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def target():
            pass
        def owner():
            [target() for target in target()]
        """,
        path=path,
    )

    _assert_unresolved_call(result, f"{path}::owner::call:0001")
    assert _resolver_target(result, f"{path}::owner::call:0002") == f"{path}::target"


_CLASS_COMPREHENSION_CASES = (
    (
        "without_outer_binding",
        "",
        None,
    ),
    (
        "with_outer_module_binding",
        "def target():\n    pass\n",
        "src/lockstep/class_comprehension_with_outer_module_binding.py::target",
    ),
)


@pytest.mark.parametrize(
    ("case", "module_prefix", "expected"),
    _CLASS_COMPREHENSION_CASES,
    ids=[case for case, *_rest in _CLASS_COMPREHENSION_CASES],
)
def test_resolver_binding_class_comprehension_skips_containing_class_namespace(
    tmp_path: Path,
    case: str,
    module_prefix: str,
    expected: str | None,
) -> None:
    """Catches resolving a comprehension body through its containing class."""

    path = f"src/lockstep/class_comprehension_{case}.py"
    result = _resolver_fixture(
        tmp_path,
        module_prefix
        + "class Box:\n"
        + "    def target(self):\n"
        + "        pass\n"
        + "    values = [target() for item in ()]\n",
        path=path,
    )
    callsite = f"{path}::Box::call:0001"

    if expected is None:
        _assert_unresolved_call(result, callsite)
    else:
        assert _resolver_target(result, callsite) == expected


def test_resolver_binding_class_body_cannot_see_its_own_pending_binding(
    tmp_path: Path,
) -> None:
    """Catches publishing a class name before its body has completed."""

    path = "src/lockstep/class_pending_binding.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def outer():
            pass
        class C:
            inherited = outer()
            recursive = C()
        """,
        path=path,
    )

    assert _resolver_target(result, f"{path}::C::call:0001") == f"{path}::outer"
    _assert_unresolved_call(result, f"{path}::C::call:0002")


def test_resolver_binding_comprehension_walrus_is_conditional_in_containing_function(
    tmp_path: Path,
) -> None:
    """Catches a conditional walrus target falling through to a module binding."""

    path = "src/lockstep/comprehension_walrus.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def alias():
            pass
        def owner(values):
            [(alias := value) for value in values]
            alias()
        """,
        path=path,
    )

    _assert_unresolved_call(result, f"{path}::owner::call:0001")


def test_resolver_binding_function_default_cannot_see_new_function_binding(
    tmp_path: Path,
) -> None:
    """Catches registering a function before evaluating its defaults."""

    path = "src/lockstep/function_default_binding.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def target(value=target()):
            pass
        """,
        path=path,
    )

    _assert_unresolved_call(result, f"{path}::target::call:0001")


_LEXICAL_BINDING_CASES = (
        (
            "parameter_is_local",
            """
            def target():
                pass
            def owner(target):
                target()
            """,
            "owner",
            None,
        ),
        (
            "named_function_binding",
            """
            def target():
                pass
            def owner():
                target()
            """,
            "owner",
            "target",
        ),
        (
            "named_class_binding",
            """
            class Target:
                pass
            def owner():
                Target()
            """,
            "owner",
            "Target",
        ),
        (
            "store_shadows_outer_for_complete_scope",
            """
            def target():
                pass
            def replacement():
                pass
            def owner():
                target()
                target = replacement
            """,
            "owner",
            None,
        ),
        (
            "delete_shadows_outer_for_complete_scope",
            """
            def target():
                pass
            def owner():
                target()
                del target
            """,
            "owner",
            None,
        ),
        (
            "augstore_shadows_outer_for_complete_scope",
            """
            def target():
                pass
            def owner():
                target()
                target += 1
            """,
            "owner",
            None,
        ),
        (
            "import_shadows_outer_for_complete_scope",
            """
            def target():
                pass
            def owner():
                target()
                from package import target
            """,
            "owner",
            None,
        ),
        (
            "function_definition_shadows_outer_for_complete_scope",
            """
            def target():
                pass
            def owner():
                target()
                def target():
                    pass
            """,
            "owner",
            None,
        ),
        (
            "class_definition_shadows_outer_for_complete_scope",
            """
            def Target():
                pass
            def owner():
                Target()
                class Target:
                    pass
            """,
            "owner",
            None,
        ),
)


@pytest.mark.parametrize(
    ("case", "source", "owner", "target"),
    _LEXICAL_BINDING_CASES,
    ids=[case for case, *_rest in _LEXICAL_BINDING_CASES],
)
def test_resolver_lexical_binding_never_falls_through_a_local_scope(
    tmp_path: Path,
    case: str,
    source: str,
    owner: str,
    target: str | None,
) -> None:
    path = f"src/lockstep/{case}.py"
    result = _resolver_fixture(tmp_path, source, path=path)
    callsite = f"{path}::{owner}::call:0001"

    if target is None:
        _assert_unresolved_call(result, callsite)
    else:
        assert _resolver_target(result, callsite) == f"{path}::{target}"


_LEXICAL_FRAME_CASES = (
    (
        "positional_only_parameter",
        """
        def target():
            pass
        def owner(target, /):
            target()
        """,
        "owner",
        None,
    ),
    (
        "keyword_only_parameter",
        """
        def target():
            pass
        def owner(*, target):
            target()
        """,
        "owner",
        None,
    ),
    (
        "vararg_parameter",
        """
        def target():
            pass
        def owner(*target):
            target()
        """,
        "owner",
        None,
    ),
    (
        "kwarg_parameter",
        """
        def target():
            pass
        def owner(**target):
            target()
        """,
        "owner",
        None,
    ),
    (
        "lambda_parameter",
        """
        def target():
            pass
        def owner():
            callback = lambda target: target()
        """,
        "owner",
        None,
    ),
    (
        "ordinary_nested_closure",
        """
        def outer():
            def target():
                pass
            def owner():
                target()
        """,
        "outer.owner",
        "outer.target",
    ),
    (
        "method_bare_name_skips_class_namespace",
        """
        class Container:
            def target(self):
                pass
            def owner(self):
                target()
        """,
        "Container.owner",
        None,
    ),
)


@pytest.mark.parametrize(
    ("case", "source", "owner", "target"),
    _LEXICAL_FRAME_CASES,
    ids=[case for case, *_rest in _LEXICAL_FRAME_CASES],
)
def test_resolver_lexical_frames_cover_every_parameter_and_class_skip_rule(
    tmp_path: Path,
    case: str,
    source: str,
    owner: str,
    target: str | None,
) -> None:
    path = f"src/lockstep/frame_{case}.py"
    result = _resolver_fixture(tmp_path, source, path=path)
    callsite = f"{path}::{owner}::call:0001"

    if target is None:
        _assert_unresolved_call(result, callsite)
    else:
        assert _resolver_target(result, callsite) == f"{path}::{target}"


def test_resolver_valid_global_and_nonlocal_redirects_are_exact(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/redirects.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def module_target():
            pass

        def global_owner():
            global module_target
            module_target()

        def outer():
            def enclosed_target():
                pass
            def inner():
                nonlocal enclosed_target
                enclosed_target()

            class ThroughClass:
                def method(self):
                    nonlocal enclosed_target
                    enclosed_target()
        """,
        path=path,
    )

    assert _resolver_target(
        result, f"{path}::global_owner::call:0001"
    ) == f"{path}::module_target"
    assert _resolver_target(
        result, f"{path}::outer.inner::call:0001"
    ) == f"{path}::outer.enclosed_target"
    assert _resolver_target(
        result, f"{path}::outer.ThroughClass.method::call:0001"
    ) == f"{path}::outer.enclosed_target"


def test_resolver_binding_applies_symbol_rules_to_decorators_and_bases(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/decorator_base_alias.py"
    result = _resolver_fixture_with_primitive_rows(
        tmp_path,
        """
        from package import decorate as imported_decorator
        decorator_alias = imported_decorator
        class Base:
            def inherited(self):
                pass
        base_alias = Base
        @decorator_alias()
        class Child(base_alias):
            def owner(self):
                self.inherited()
        """,
        (
            _primitive_entity_row(
                "package.decorate",
                ("planning/transformation",),
            ),
        ),
        path=path,
    )

    assert _resolver_target(
        result, f"{path}::Child::call:0001"
    ) == "package.decorate"
    assert _resolver_target(
        result, f"{path}::Child.owner::call:0001"
    ) == f"{path}::Base.inherited"


_INVALID_REDIRECT_CASES = (
        (
            "duplicate_global",
            """
            def target():
                pass
            def owner():
                global target
                global target
                target()
            """,
            "owner",
        ),
        (
            "duplicate_nonlocal",
            """
            def outer():
                def target():
                    pass
                def owner():
                    nonlocal target
                    nonlocal target
                    target()
            """,
            "outer.owner",
        ),
        (
            "declaration_after_use",
            """
            def target():
                pass
            def owner():
                target()
                global target
            """,
            "owner",
        ),
        (
            "nonlocal_declaration_after_use",
            """
            def outer():
                def target():
                    pass
                def owner():
                    target()
                    nonlocal target
            """,
            "outer.owner",
        ),
        (
            "missing_global",
            """
            def owner():
                global missing
                missing()
            """,
            "owner",
        ),
        (
            "missing_nonlocal",
            """
            def outer():
                def owner():
                    nonlocal missing
                    missing()
            """,
            "outer.owner",
        ),
        (
            "global_store",
            """
            def target():
                pass
            def replacement():
                pass
            def owner():
                global target
                target = replacement
                target()
            """,
            "owner",
        ),
        (
            "global_delete",
            """
            def target():
                pass
            def owner():
                global target
                del target
                target()
            """,
            "owner",
        ),
        (
            "global_augstore",
            """
            def target():
                pass
            def owner():
                global target
                target += 1
                target()
            """,
            "owner",
        ),
        (
            "nonlocal_store",
            """
            def outer():
                def target():
                    pass
                def replacement():
                    pass
                def owner():
                    nonlocal target
                    target = replacement
                    target()
            """,
            "outer.owner",
        ),
        (
            "nonlocal_delete",
            """
            def outer():
                def target():
                    pass
                def owner():
                    nonlocal target
                    del target
                    target()
            """,
            "outer.owner",
        ),
        (
            "nonlocal_augstore",
            """
            def outer():
                def target():
                    pass
                def owner():
                    nonlocal target
                    target += 1
                    target()
            """,
            "outer.owner",
        ),
)


@pytest.mark.parametrize(
    ("case", "source", "owner"),
    _INVALID_REDIRECT_CASES,
    ids=[case for case, *_rest in _INVALID_REDIRECT_CASES],
)
def test_resolver_binding_rejects_invalid_global_and_nonlocal_declarations(
    tmp_path: Path,
    case: str,
    source: str,
    owner: str,
) -> None:
    path = f"src/lockstep/{case}.py"
    result = _resolver_fixture(tmp_path, source, path=path)
    _assert_unresolved_call(result, f"{path}::{owner}::call:0001")


_CONDITIONAL_BINDING_CASES = (
    (
        "if",
        "if flag:\n    receiver = Worker()",
        "if flag:\n    alias = target",
        "if flag:\n    self.dependency = dependency",
    ),
    (
        "for",
        "for _ in items:\n    receiver = Worker()",
        "for _ in items:\n    alias = target",
        "for _ in items:\n    self.dependency = dependency",
    ),
    (
        "comprehension",
        "values = [(receiver := Worker()) for _ in items]",
        "values = [(alias := target) for _ in items]",
        "values = [value for self.dependency in (dependency,)]",
    ),
    (
        "while",
        "while flag:\n    receiver = Worker()\n    break",
        "while flag:\n    alias = target\n    break",
        "while flag:\n    self.dependency = dependency\n    break",
    ),
    (
        "try",
        "try:\n    receiver = Worker()\nexcept Exception:\n    pass",
        "try:\n    alias = target\nexcept Exception:\n    pass",
        "try:\n    self.dependency = dependency\nexcept Exception:\n    pass",
    ),
    (
        "except",
        "try:\n    pass\nexcept Exception:\n    receiver = Worker()",
        "try:\n    pass\nexcept Exception:\n    alias = target",
        "try:\n    pass\nexcept Exception:\n    self.dependency = dependency",
    ),
    (
        "finally",
        "try:\n    pass\nfinally:\n    receiver = Worker()",
        "try:\n    pass\nfinally:\n    alias = target",
        "try:\n    pass\nfinally:\n    self.dependency = dependency",
    ),
    (
        "with",
        "with manager as receiver:\n    pass",
        "with manager as alias:\n    pass",
        "with manager as self.dependency:\n    pass",
    ),
    (
        "match",
        "match subject:\n    case receiver:\n        pass",
        "match subject:\n    case alias:\n        pass",
        "match subject:\n    case 0:\n        self.dependency = dependency",
    ),
    (
        "conditional_expression",
        "receiver = Worker() if flag else Worker()",
        "alias = target if flag else target",
        "self.dependency = dependency if flag else dependency",
    ),
    (
        "short_circuit",
        "receiver = flag and Worker()",
        "alias = flag and target",
        "self.dependency = flag and dependency",
    ),
    (
        "lambda",
        "builder = lambda: ((receiver := Worker()), receiver.run())\nbuilder()",
        "builder = lambda: ((alias := target), alias())\nbuilder()",
        "builder = lambda: dependency\nself.dependency = builder()",
    ),
    (
        "assignment_expression",
        "if (receiver := Worker()):\n    pass",
        "if (alias := target):\n    pass",
        "if (bound := dependency):\n    self.dependency = bound",
    ),
    (
        "mutually_exclusive_branches",
        "if flag:\n    receiver = Worker()\nelse:\n    receiver = Worker()",
        "if flag:\n    alias = target\nelse:\n    alias = target",
        "if flag:\n    self.dependency = dependency\nelse:\n    self.dependency = dependency",
    ),
    (
        "exception_target_cleanup",
        "try:\n    pass\nexcept Exception as receiver:\n    pass",
        "try:\n    pass\nexcept Exception as alias:\n    pass",
        "try:\n    pass\nexcept Exception as ignored:\n    self.dependency = dependency",
    ),
    (
        "loop_target",
        "for receiver in items:\n    pass",
        "for alias in items:\n    pass",
        "for _ in items:\n    self.dependency = dependency",
    ),
)


@pytest.mark.parametrize(
    ("case", "assignment", "_alias_assignment", "_injection_assignment"),
    _CONDITIONAL_BINDING_CASES,
    ids=[case for case, *_rest in _CONDITIONAL_BINDING_CASES],
)
def test_resolver_receiver_assignment_is_unconditional_across_every_control_form(
    tmp_path: Path,
    case: str,
    assignment: str,
    _alias_assignment: str,
    _injection_assignment: str,
) -> None:
    path = f"src/lockstep/conditional_{case}.py"
    observed_call = "" if case == "lambda" else "    receiver.run()\n"
    source = (
        "class Worker:\n"
        "    def run(self):\n"
        "        pass\n"
        "def owner(flag, items, manager, subject):\n"
        f"{textwrap.indent(assignment, '    ')}\n"
        f"{observed_call}"
    )
    result = _resolver_fixture(tmp_path, source, path=path)
    if case == "lambda":
        record = _assert_unresolved_call(result, f"{path}::owner::call:0002")
        assert record.ast_dump == (
            "Call(func=Attribute(value=Name(id='receiver', ctx=Load()), "
            "attr='run', ctx=Load()), args=[], keywords=[])"
        )
        return

    receiver_calls = [
        record
        for record in _records_named(result, "UnresolvedCall")
        if "Attribute(value=Name(id='receiver'" in record.ast_dump
        and "attr='run'" in record.ast_dump
    ]
    assert len(receiver_calls) == 1


@pytest.mark.parametrize(
    ("case", "_receiver_assignment", "assignment", "_injection_assignment"),
    _CONDITIONAL_BINDING_CASES,
    ids=[case for case, *_rest in _CONDITIONAL_BINDING_CASES],
)
def test_resolver_binding_rejects_symbol_aliases_in_every_conditional_form(
    tmp_path: Path,
    case: str,
    _receiver_assignment: str,
    assignment: str,
    _injection_assignment: str,
) -> None:
    path = f"src/lockstep/conditional_alias_{case}.py"
    observed_call = "" if case == "lambda" else "    alias()\n"
    source = (
        "def target():\n"
        "    pass\n"
        "def owner(flag=False, items=(), manager=None, subject=None):\n"
        f"{textwrap.indent(assignment, '    ')}\n"
        f"{observed_call}"
    )
    result = _resolver_fixture(tmp_path, source, path=path)
    if case == "lambda":
        record = _assert_unresolved_call(result, f"{path}::owner::call:0001")
        assert record.ast_dump == (
            "Call(func=Name(id='alias', ctx=Load()), args=[], keywords=[])"
        )
        return

    alias_calls = [
        record
        for record in _records_named(result, "UnresolvedCall")
        if record.ast_dump == "Call(func=Name(id='alias', ctx=Load()), args=[], keywords=[])"
    ]
    assert len(alias_calls) == 1


@pytest.mark.parametrize(
    ("case", "_receiver_assignment", "assignment", "_injection_assignment"),
    _CONDITIONAL_BINDING_CASES,
    ids=[case for case, *_rest in _CONDITIONAL_BINDING_CASES],
)
def test_resolver_binding_rejects_conditional_module_aliases_in_decorator_and_base(
    tmp_path: Path,
    case: str,
    _receiver_assignment: str,
    assignment: str,
    _injection_assignment: str,
) -> None:
    path = f"src/lockstep/conditional_module_alias_{case}.py"
    decorator_assignment = (
        assignment.replace("alias", "decorator_alias")
        .replace("target", "imported_decorator")
        .replace("builder", "decorator_builder")
    )
    base_assignment = (
        assignment.replace("alias", "base_alias")
        .replace("target", "Base")
        .replace("builder", "base_builder")
    )
    source = (
        "from package import decorate as imported_decorator\n"
        "class Base:\n"
        "    def inherited(self):\n"
        "        pass\n"
        f"{decorator_assignment}\n"
        f"{base_assignment}\n"
        "@decorator_alias()\n"
        "class Child(base_alias):\n"
        "    def owner(self):\n"
        "        self.inherited()\n"
    )
    result = _resolver_fixture(tmp_path, source, path=path)

    _assert_unresolved_call(result, f"{path}::Child::call:0001")
    _assert_unresolved_call(result, f"{path}::Child.owner::call:0001")


@pytest.mark.parametrize(
    ("case", "_receiver_assignment", "_alias_assignment", "assignment"),
    _CONDITIONAL_BINDING_CASES,
    ids=[case for case, *_rest in _CONDITIONAL_BINDING_CASES],
)
def test_resolver_receiver_rejects_annotated_injection_in_every_conditional_form(
    tmp_path: Path,
    case: str,
    _receiver_assignment: str,
    _alias_assignment: str,
    assignment: str,
) -> None:
    path = f"src/lockstep/conditional_injection_{case}.py"
    source = (
        "class Dependency:\n"
        "    def work(self):\n"
        "        pass\n"
        "class Service:\n"
        "    def __init__(self, dependency: Dependency, flag=False, "
        "items=(), manager=None, subject=None):\n"
        f"{textwrap.indent(assignment, '        ')}\n"
        "    def run(self):\n"
        "        self.dependency.work()\n"
    )
    result = _resolver_fixture(tmp_path, source, path=path)
    _assert_unresolved_call(result, f"{path}::Service.run::call:0001")


def test_resolver_self_cls_and_super_use_unique_declared_inheritance(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/inheritance.py"
    result = _resolver_fixture(
        tmp_path,
        """
        class Base:
            def inherited(self):
                pass

        class Other:
            pass

        class Child(Base, Other):
            def own(self):
                pass
            def instance(self):
                self.own()
            @classmethod
            def class_side(cls):
                cls.own()
            def parent(self):
                super().inherited()
        """,
        path=path,
        allowlist=frozenset({"builtins.super"}),
    )

    assert _resolver_target(
        result, f"{path}::Child.instance::call:0001"
    ) == f"{path}::Child.own"
    assert _resolver_target(
        result, f"{path}::Child.class_side::call:0001"
    ) == f"{path}::Child.own"
    assert _resolver_target(
        result, f"{path}::Child.parent::call:0001"
    ) == f"{path}::Base.inherited"
    assert _resolver_target(
        result, f"{path}::Child.parent::call:0002"
    ) == "builtins.super"


def test_resolver_self_cls_and_super_choose_same_named_method_by_receiver(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/inheritance_same_name.py"
    result = _resolver_fixture(
        tmp_path,
        """
        class Base:
            def shared(self):
                pass

        class Child(Base):
            def shared(self):
                pass
            def instance(self):
                self.shared()
            @classmethod
            def class_side(cls):
                cls.shared()
            def parent(self):
                super().shared()
        """,
        path=path,
        allowlist=frozenset({"builtins.super"}),
    )

    assert _resolver_target(
        result, f"{path}::Child.instance::call:0001"
    ) == f"{path}::Child.shared"
    assert _resolver_target(
        result, f"{path}::Child.class_side::call:0001"
    ) == f"{path}::Child.shared"
    assert _resolver_target(
        result, f"{path}::Child.parent::call:0001"
    ) == f"{path}::Base.shared"
    assert _resolver_target(
        result, f"{path}::Child.parent::call:0002"
    ) == "builtins.super"


_SHADOWED_CLASS_RECEIVER_CASES = (
    ("nested_self", "def nested(self):\n            self.shared()"),
    ("nested_cls", "def nested(cls):\n            cls.shared()"),
    ("lambda_self", "nested = lambda self: self.shared()"),
    ("lambda_cls", "nested = lambda cls: cls.shared()"),
)


@pytest.mark.parametrize(
    ("case", "nested_source"),
    _SHADOWED_CLASS_RECEIVER_CASES,
    ids=[case for case, *_rest in _SHADOWED_CLASS_RECEIVER_CASES],
)
def test_resolver_receiver_rejects_nested_or_lambda_shadowed_self_cls(
    tmp_path: Path,
    case: str,
    nested_source: str,
) -> None:
    """Catches treating an inner parameter as the containing class receiver."""

    path = f"src/lockstep/shadowed_class_receiver_{case}.py"
    result = _resolver_fixture(
        tmp_path,
        (
            "class Box:\n"
            "    def shared(self):\n"
            "        pass\n"
            "    def owner(self):\n"
            f"        {nested_source}\n"
        ),
        path=path,
    )

    owner = "Box.owner.nested" if case.startswith("nested") else "Box.owner"
    _assert_unresolved_call(result, f"{path}::{owner}::call:0001")


_CAPTURED_CLASS_RECEIVER_CASES = (
    (
        "nested_self",
        "def owner(self):\n        def nested():\n            self.shared()",
        "Box.owner.nested",
    ),
    (
        "nested_cls",
        "@classmethod\n    def owner(cls):\n        def nested():\n            cls.shared()",
        "Box.owner.nested",
    ),
    (
        "lambda_self",
        "def owner(self):\n        nested = lambda: self.shared()",
        "Box.owner",
    ),
    (
        "lambda_cls",
        "@classmethod\n    def owner(cls):\n        nested = lambda: cls.shared()",
        "Box.owner",
    ),
)


@pytest.mark.parametrize(
    ("case", "owner_source", "call_owner"),
    _CAPTURED_CLASS_RECEIVER_CASES,
    ids=[case for case, *_rest in _CAPTURED_CLASS_RECEIVER_CASES],
)
def test_resolver_receiver_accepts_nested_or_lambda_captured_self_cls(
    tmp_path: Path,
    case: str,
    owner_source: str,
    call_owner: str,
) -> None:
    """Catches rejecting a valid closure capture while fixing shadowing."""

    path = f"src/lockstep/captured_class_receiver_{case}.py"
    result = _resolver_fixture(
        tmp_path,
        (
            "class Box:\n"
            "    def shared(self):\n"
            "        pass\n"
            f"    {owner_source}\n"
        ),
        path=path,
    )

    assert _resolver_target(result, f"{path}::{call_owner}::call:0001") == (
        f"{path}::Box.shared"
    )


_INVALID_SUPER_RECEIVER_CASES = (
    (
        "explicit_arguments",
        "def owner(self):\n        super(Child, self).shared()",
    ),
    (
        "keyword_arguments",
        "def owner(self):\n        super(type=Child, obj=self).shared()",
    ),
    (
        "module_shadow",
        "def owner(self):\n        super().shared()",
    ),
    (
        "parameter_shadow",
        "def owner(self, super):\n        super().shared()",
    ),
)


@pytest.mark.parametrize(
    ("case", "owner_source"),
    _INVALID_SUPER_RECEIVER_CASES,
    ids=[case for case, *_rest in _INVALID_SUPER_RECEIVER_CASES],
)
def test_resolver_receiver_accepts_only_unshadowed_zero_arg_builtin_super(
    tmp_path: Path,
    case: str,
    owner_source: str,
) -> None:
    """Catches spelling-only super receiver recognition."""

    path = f"src/lockstep/invalid_super_{case}.py"
    prefix = "def super():\n    pass\n" if case == "module_shadow" else ""
    result = _resolver_fixture(
        tmp_path,
        (
            prefix
            + "class Base:\n"
            "    def shared(self):\n"
            "        pass\n"
            "class Child(Base):\n"
            f"    {owner_source}\n"
        ),
        path=path,
        allowlist=frozenset({"builtins.super"}),
    )

    _assert_unresolved_call(result, f"{path}::Child.owner::call:0001")


_AMBIGUOUS_INHERITANCE_CASES = (
    ("self", "self", "", "self"),
    ("cls", "cls", "@classmethod\n    ", "cls"),
    ("super", "super()", "", "self"),
)


@pytest.mark.parametrize(
    ("case", "receiver", "decorator", "parameter"),
    _AMBIGUOUS_INHERITANCE_CASES,
    ids=[case for case, *_rest in _AMBIGUOUS_INHERITANCE_CASES],
)
def test_resolver_receiver_rejects_ambiguous_inheritance(
    tmp_path: Path,
    case: str,
    receiver: str,
    decorator: str,
    parameter: str,
) -> None:
    path = "src/lockstep/ambiguous_inheritance.py"
    owner_definition = (
        f"    {decorator}def owner({parameter}):\n"
        f"        {receiver}.collide()\n"
    )
    result = _resolver_fixture(
        tmp_path,
        (
            "class Left:\n"
            "    def collide(self):\n"
            "        pass\n"
            "class Right:\n"
            "    def collide(self):\n"
            "        pass\n"
            "class Child(Left, Right):\n"
            f"{owner_definition}"
        ),
        path=path,
        allowlist=frozenset({"builtins.super"}),
    )

    _assert_unresolved_call(result, f"{path}::Child.owner::call:0001")


def test_resolver_receiver_accepts_immutable_constructor_annotation_and_inline_forms(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/local_receivers.py"
    result = _resolver_fixture(
        tmp_path,
        """
        class Worker:
            def run(self):
                pass
        def owner():
            assigned = Worker()
            assigned.run()
            annotated: Worker
            annotated.run()
            Worker().run()
        """,
        path=path,
    )

    assert [
        _resolver_target(result, f"{path}::owner::call:{ordinal:04d}")
        for ordinal in range(1, 6)
    ] == [
        f"{path}::Worker",
        f"{path}::Worker.run",
        f"{path}::Worker.run",
        f"{path}::Worker.run",
        f"{path}::Worker",
    ]


def test_resolver_receiver_rejects_an_ambiguous_constructor_binding(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/ambiguous_constructor.py"
    result = _resolver_fixture(
        tmp_path,
        """
        from lockstep.left import Worker
        from lockstep.right import Worker
        def owner():
            worker = Worker()
            worker.run()
        """,
        path=path,
        extra_files={
            "src/lockstep/left.py": """
            class Worker:
                def run(self):
                    pass
            """,
            "src/lockstep/right.py": """
            class Worker:
                def run(self):
                    pass
            """,
        },
    )

    _assert_unresolved_call(result, f"{path}::owner::call:0002")


_LOCAL_RECEIVER_INVALIDATIONS = (
    ("rebound", "worker = Worker()\nworker = Worker()"),
    ("deleted", "worker = Worker()\ndel worker"),
    ("augmented", "worker = Worker()\nworker += other"),
    ("passed_positionally", "worker = Worker()\nconsume(worker)"),
    ("passed_by_keyword", "worker = Worker()\nconsume(value=worker)"),
    ("captured", "worker = Worker()\ninner = lambda: worker"),
    (
        "nested_scope_write",
        "worker = Worker()\ndef nested():\n    nonlocal worker\n    worker = Worker()",
    ),
)


@pytest.mark.parametrize(
    ("case", "setup"),
    _LOCAL_RECEIVER_INVALIDATIONS,
    ids=[case for case, _setup in _LOCAL_RECEIVER_INVALIDATIONS],
)
def test_resolver_receiver_rejects_rebind_delete_reference_and_capture(
    tmp_path: Path,
    case: str,
    setup: str,
) -> None:
    path = f"src/lockstep/local_receiver_{case}.py"
    source = (
        "class Worker:\n"
        "    def run(self):\n"
        "        pass\n"
        "def owner():\n"
        f"{textwrap.indent(setup, '    ')}\n"
        "    worker.run()\n"
    )
    result = _resolver_fixture(tmp_path, source, path=path)
    callsite = next(
        record.callsite
        for record in _records_named(result, "UnresolvedCall")
        if "Attribute(value=Name(id='worker'" in record.ast_dump
        and "attr='run'" in record.ast_dump
    )
    _assert_unresolved_call(result, callsite)


def test_resolver_receiver_accepts_one_class_wide_constructor_field(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/self_field.py"
    result = _resolver_fixture(
        tmp_path,
        """
        class Worker:
            def run(self):
                pass
        class Service:
            def __init__(self):
                self.worker = Worker()
            def reset(self):
                self.worker = Worker()
            def run(self):
                self.worker.run()
        """,
        path=path,
    )

    assert _resolver_target(
        result, f"{path}::Service.run::call:0001"
    ) == f"{path}::Worker.run"


_CLASS_FIELD_INVALIDATIONS = (
        (
            "different_constructor",
            "def replace(self):\n    self.worker = Other()",
        ),
        ("delete", "def replace(self):\n    del self.worker"),
        ("augstore", "def replace(self):\n    self.worker += other"),
        (
            "dynamic_assignment",
            "def replace(self, value):\n    self.worker = value",
        ),
        (
            "conditional_assignment",
            "def replace(self, flag):\n    if flag:\n        self.worker = Worker()",
        ),
)


@pytest.mark.parametrize(
    ("case", "extra"),
    _CLASS_FIELD_INVALIDATIONS,
    ids=[case for case, *_rest in _CLASS_FIELD_INVALIDATIONS],
)
def test_resolver_receiver_rejects_nonuniform_class_wide_field_bindings(
    tmp_path: Path,
    case: str,
    extra: str,
) -> None:
    path = f"src/lockstep/self_field_{case}.py"
    source = f"""
        class Worker:
            def run(self):
                pass
        class Other:
            def run(self):
                pass
        class Service:
            def __init__(self):
                self.worker = Worker()
{textwrap.indent(extra, '            ')}
            def run(self):
                self.worker.run()
    """
    result = _resolver_fixture(tmp_path, source, path=path)
    _assert_unresolved_call(result, f"{path}::Service.run::call:0001")


_ANNOTATED_INJECTION_CASES = (
    ("name", "from lockstep.dependency import Dependency", "Dependency"),
    ("attribute", "import lockstep.dependency as dep", "dep.Dependency"),
)


@pytest.mark.parametrize(
    ("case", "import_line", "annotation"),
    _ANNOTATED_INJECTION_CASES,
    ids=[case for case, *_rest in _ANNOTATED_INJECTION_CASES],
)
def test_resolver_receiver_accepts_exact_annotated_parameter_injection(
    tmp_path: Path,
    case: str,
    import_line: str,
    annotation: str,
) -> None:
    path = f"src/lockstep/injection_{case}.py"
    result = _resolver_fixture(
        tmp_path,
        f"""
        {import_line}
        class Service:
            def __init__(self, dependency: {annotation}):
                self.dependency = dependency
            def run(self):
                self.dependency.work()
        """,
        path=path,
        extra_files={
            "src/lockstep/dependency.py": """
            class Dependency:
                def work(self):
                    pass
            """
        },
    )

    assert _resolver_target(
        result, f"{path}::Service.run::call:0001"
    ) == "src/lockstep/dependency.py::Dependency.work"


_INJECTION_NEGATIVES = (
    ("missing_annotation", "", "self.dependency = dependency", "", ""),
    ("string_annotation", ': "Dependency"', "self.dependency = dependency", "", ""),
    ("generic_annotation", ": list[Dependency]", "self.dependency = dependency", "", ""),
    (
        "parameter_rebound",
        ": Dependency",
        "dependency = Dependency()\nself.dependency = dependency",
        "",
        "",
    ),
    (
        "parameter_deleted",
        ": Dependency",
        "self.dependency = dependency\ndel dependency",
        "",
        "",
    ),
    (
        "parameter_augstore",
        ": Dependency",
        "self.dependency = dependency\ndependency += other",
        "",
        "",
    ),
    (
        "conditional_assignment",
        ": Dependency",
        "if flag:\n    self.dependency = dependency",
        "",
        "",
    ),
    (
        "duplicate_assignment",
        ": Dependency",
        "self.dependency = dependency\nself.dependency = dependency",
        "",
        "",
    ),
    (
        "parameter_returned",
        ": Dependency",
        "self.dependency = dependency\nreturn dependency",
        "",
        "",
    ),
    (
        "parameter_yielded",
        ": Dependency",
        "self.dependency = dependency\nyield dependency",
        "",
        "",
    ),
    (
        "parameter_stored_elsewhere",
        ": Dependency",
        "self.dependency = dependency\nself.other = dependency",
        "",
        "",
    ),
    (
        "parameter_passed",
        ": Dependency",
        "self.dependency = dependency\nconsume(dependency)",
        "",
        "",
    ),
    (
        "parameter_captured",
        ": Dependency",
        "self.dependency = dependency\ncaptured = lambda: dependency",
        "",
        "",
    ),
    (
        "parameter_aliased",
        ": Dependency",
        "self.dependency = dependency\nalias = dependency",
        "",
        "",
    ),
    (
        "field_returned",
        ": Dependency",
        "self.dependency = dependency",
        "def escape(self):\n    return self.dependency",
        "",
    ),
    (
        "field_yielded",
        ": Dependency",
        "self.dependency = dependency",
        "def escape(self):\n    yield self.dependency",
        "",
    ),
    (
        "field_stored_elsewhere",
        ": Dependency",
        "self.dependency = dependency",
        "def escape(self):\n    self.other = self.dependency",
        "",
    ),
    (
        "field_passed",
        ": Dependency",
        "self.dependency = dependency",
        "def escape(self):\n    consume(self.dependency)",
        "",
    ),
    (
        "field_captured",
        ": Dependency",
        "self.dependency = dependency",
        "def escape(self):\n    captured = lambda: self.dependency",
        "",
    ),
    (
        "field_aliased",
        ": Dependency",
        "self.dependency = dependency",
        "def escape(self):\n    alias = self.dependency",
        "",
    ),
    (
        "subclass_store",
        ": Dependency",
        "self.dependency = dependency",
        "",
        "def replace(self, value):\n    self.dependency = value",
    ),
    (
        "subclass_delete",
        ": Dependency",
        "self.dependency = dependency",
        "",
        "def replace(self):\n    del self.dependency",
    ),
    (
        "subclass_augstore",
        ": Dependency",
        "self.dependency = dependency",
        "",
        "def replace(self):\n    self.dependency += other",
    ),
)


@pytest.mark.parametrize(
    ("case", "annotation", "assignment", "extra_service", "subclass_body"),
    _INJECTION_NEGATIVES,
    ids=[case for case, *_rest in _INJECTION_NEGATIVES],
)
def test_resolver_receiver_rejects_inexact_or_escaped_parameter_injection(
    tmp_path: Path,
    case: str,
    annotation: str,
    assignment: str,
    extra_service: str,
    subclass_body: str,
) -> None:
    path = f"src/lockstep/injection_negative_{case}.py"
    flag_parameter = ", flag" if case == "conditional_assignment" else ""
    service_extra = (
        textwrap.indent(extra_service, "    ") + "\n" if extra_service else ""
    )
    subclass = (
        "\nclass Child(Service):\n" + textwrap.indent(subclass_body, "    ")
        if subclass_body
        else ""
    )
    source = (
        "class Dependency:\n"
        "    def work(self):\n"
        "        pass\n"
        "class Service:\n"
        f"    def __init__(self, dependency{annotation}{flag_parameter}):\n"
        f"{textwrap.indent(assignment, '        ')}\n"
        f"{service_extra}"
        "    def run(self):\n"
        "        self.dependency.work()\n"
        f"{subclass}\n"
    )
    result = _resolver_fixture(tmp_path, source, path=path)
    _assert_unresolved_call(result, f"{path}::Service.run::call:0001")


def test_resolver_receiver_limits_annotated_parameter_injection_to_init(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/injection_not_init.py"
    result = _resolver_fixture(
        tmp_path,
        """
        class Dependency:
            def work(self):
                pass
        class Service:
            def configure(self, dependency: Dependency):
                self.dependency = dependency
            def run(self):
                self.dependency.work()
        """,
        path=path,
    )

    _assert_unresolved_call(result, f"{path}::Service.run::call:0001")


def test_resolver_symbol_aliases_require_one_direct_immutable_assignment(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/symbol_alias.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def target():
            pass
        class Worker:
            def run(self):
                pass
        def owner():
            alias = target
            alias()
            class_alias = Worker
            class_alias().run()
        """,
        path=path,
    )

    assert _resolver_target(
        result, f"{path}::owner::call:0001"
    ) == f"{path}::target"
    assert _resolver_target(
        result, f"{path}::owner::call:0002"
    ) == f"{path}::Worker.run"
    assert _resolver_target(
        result, f"{path}::owner::call:0003"
    ) == f"{path}::Worker"


_SYMBOL_ALIAS_INVALIDATIONS = (
    ("later_store", "alias = target\nalias = replacement"),
    ("later_delete", "alias = target\ndel alias"),
    ("later_augstore", "alias = target\nalias += replacement"),
    (
        "closure_write",
        "alias = target\ndef nested():\n    nonlocal alias\n    alias = replacement",
    ),
    ("conditional", "if flag:\n    alias = target"),
    ("indirect", "first = target\nalias = first"),
)


@pytest.mark.parametrize(
    ("case", "assignment"),
    _SYMBOL_ALIAS_INVALIDATIONS,
    ids=[case for case, _assignment in _SYMBOL_ALIAS_INVALIDATIONS],
)
def test_resolver_binding_rejects_rebound_conditional_and_indirect_symbol_aliases(
    tmp_path: Path,
    case: str,
    assignment: str,
) -> None:
    path = f"src/lockstep/alias_{case}.py"
    source = (
        "def target():\n"
        "    pass\n"
        "def replacement():\n"
        "    pass\n"
        "def owner(flag=False):\n"
        f"{textwrap.indent(assignment, '    ')}\n"
        "    alias()\n"
    )
    result = _resolver_fixture(tmp_path, source, path=path)
    _assert_unresolved_call(result, f"{path}::owner::call:0001")


_DYNAMIC_CALL_CASES = (
    ("unknown_name", "unknown()"),
    ("parameter_receiver", "value.method()"),
    ("nested_dynamic_attribute", "module.dynamic.method()"),
    ("reflective_getattr", "getattr(value, 'method')()"),
    ("dunder_reflection", "value.__getattribute__('method')()"),
    ("subscript_callable", "registry['handler']()"),
)


@pytest.mark.parametrize(
    ("case", "expression"),
    _DYNAMIC_CALL_CASES,
    ids=[case for case, *_rest in _DYNAMIC_CALL_CASES],
)
def test_resolver_callsite_dynamic_and_reflective_forms_remain_unresolved(
    tmp_path: Path,
    case: str,
    expression: str,
) -> None:
    path = f"src/lockstep/dynamic_{case}.py"
    result = _resolver_fixture(
        tmp_path,
        f"""
        import package.module as module
        def owner(value, registry):
            {expression}
        """,
        path=path,
        allowlist=frozenset({"builtins.getattr"}),
    )

    _assert_unresolved_call(result, f"{path}::owner::call:0001")


def test_resolver_binding_star_import_never_creates_a_name_binding(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/star_import.py"
    result = _resolver_fixture(
        tmp_path,
        """
        from package import *
        def owner():
            imported_name()
        """,
        path=path,
    )
    _assert_unresolved_call(result, f"{path}::owner::call:0001")


def test_resolver_callsite_unresolved_record_has_stable_coordinate_and_ast_dump(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/unresolved.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def owner(value):
            mystery(value)
        """,
        path=path,
    )

    record = _assert_unresolved_call(result, f"{path}::owner::call:0001")
    assert {field.name for field in fields(record)} == {
        "callsite",
        "line",
        "column",
        "ast_dump",
    }
    assert (record.line, record.column) == (2, 4)
    assert record.ast_dump == (
        "Call(func=Name(id='mystery', ctx=Load()), "
        "args=[Name(id='value', ctx=Load())], keywords=[])"
    )


@pytest.mark.parametrize(
    ("case", "filename"),
    (
        ("effect_free_allowlist", "architecture_effect_free_allowlist.json"),
        ("effect_primitives", "architecture_effect_primitives.json"),
    ),
    ids=("effect_free_allowlist", "effect_primitives"),
)
def test_resolver_rule_table_checked_in_bytes_are_exact_canonical_json(
    case: str,
    filename: str,
) -> None:
    """Catches pretty printing, unsorted keys, and a trailing newline."""

    raw = (ARCHITECTURE_TEST_ROOT / filename).read_bytes()
    parsed = json.loads(raw)
    canonical = json.dumps(
        parsed,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert raw == canonical, case
    assert not raw.endswith(b"\n"), case
    expected_keys = (
        {"schema_version", "targets"}
        if case == "effect_free_allowlist"
        else {
            "schema_version",
            "reference_source_sha256",
            "callsite_evidence",
            "rows",
        }
    )
    assert set(parsed) == expected_keys, case


def test_resolver_checked_in_advisory_lock_open_row_matches_read_write_source(
    tmp_path: Path,
) -> None:
    """Catches dropping read capability from the O_RDWR advisory-lock open."""

    path = "src/lockstep/runtime/advisory_lock.py"
    owner = f"{path}::advisory_file_lock"
    selector = f"{owner}::call:0002"
    index = _fixture_index({path: (ENGINE_ROOT / path).read_bytes()}, tmp_path)
    owner_node = _resolver_owner_node(index, owner)
    call = _resolver_owner_calls(index, owner)[1]
    assert isinstance(call.args[1], ast.Name) and call.args[1].id == "flags"
    assert any(
        isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == call.args[1].id
            for target in statement.targets
        )
        and any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr == "O_RDWR"
            for node in ast.walk(statement.value)
        )
        for statement in owner_node.body
    )
    table = json.loads(
        (ARCHITECTURE_TEST_ROOT / "architecture_effect_primitives.json").read_bytes()
    )

    assert [row for row in table["rows"] if row["selector"] == selector] == [
        {
            "selector_kind": "callsite",
            "selector": selector,
            "semantic_target": "os.open",
            "domains": [
                "filesystem-read",
                "filesystem-write",
                "lifecycle-control",
            ],
        }
    ]
    assert [
        record
        for record in table["callsite_evidence"]
        if record["selector"] == selector
    ] == [
        {
            "selector": selector,
            "owner_source_sha256": index.entities[owner].span.sha256,
            "call_ast_sha256": hashlib.sha256(
                ast.dump(call, include_attributes=False).encode("utf-8")
            ).hexdigest(),
        }
    ]


def test_resolver_rule_table_rejects_duplicate_allowlist_targets(
    tmp_path: Path,
) -> None:
    """Catches silently collapsing duplicate reviewed targets into a set."""

    with pytest.raises(
        ValueError,
        match=r"^duplicate effect-free allowlist target: builtins\.len$",
    ):
        _resolver_fixture(
            tmp_path,
            "def owner():\n    pass\n",
            allowlist={
                "schema_version": 1,
                "targets": ["builtins.len", "builtins.len"],
            },
        )


def test_resolver_rule_table_rejects_duplicate_primitive_binding(
    tmp_path: Path,
) -> None:
    """Catches first-row-wins ambiguity for one exact selector binding."""

    selector = "external.duplicate"
    rows = (
        {**_primitive_entity_row(selector), "semantic_target": "reviewed.first"},
        {**_primitive_entity_row(selector), "semantic_target": "reviewed.second"},
    )
    with pytest.raises(
        ValueError,
        match=rf"^duplicate primitive binding: {re.escape(selector)}$",
    ):
        _resolver_fixture_with_primitive_rows(
            tmp_path,
            "def owner():\n    pass\n",
            rows,
            path="src/lockstep/duplicate_primitive.py",
        )


def test_resolver_rule_table_accepts_exact_top_level_callsite_evidence(
    tmp_path: Path,
) -> None:
    """Catches omitting the frozen source and per-callsite evidence contract."""

    path = "src/lockstep/exact_callsite_evidence.py"
    source = "def owner(callback):\n    callback()\n"
    files = {path: _resolver_source(source)}
    index = _fixture_index(files, tmp_path)
    callsite = f"{path}::owner::call:0001"
    table = _primitive_table(
        index,
        (_primitive_callsite_row(callsite, "reviewed.callback"),),
    )

    assert set(table) == {
        "schema_version",
        "reference_source_sha256",
        "callsite_evidence",
        "rows",
    }
    assert [set(record) for record in table["callsite_evidence"]] == [
        {"selector", "owner_source_sha256", "call_ast_sha256"}
    ]
    result = resolve_calls(index, (), table)
    assert _resolver_target(result, callsite) == "reviewed.callback"


_DELIMITED_OWNER_EVIDENCE_CASES = (
    (
        "entity_owner",
        b"def owner(callback):\n    callback()\n",
        "owner",
        "f2ed8cd1d896381013ccee061ca9aa8fd1dedf775e67309a094b936c00093eb8",
    ),
    (
        "file_owner",
        b"callback()\n",
        "@file",
        "fa8cd5c1da5d9d773ba2baa5e560f185533c49ac211bc94cd5b8e2811b219b05",
    ),
)


@pytest.mark.parametrize(
    ("case", "source", "qualified_owner", "owner_source_sha256"),
    _DELIMITED_OWNER_EVIDENCE_CASES,
    ids=[case for case, *_rest in _DELIMITED_OWNER_EVIDENCE_CASES],
)
def test_resolver_callsite_evidence_splits_owner_path_at_final_delimiter(
    tmp_path: Path,
    case: str,
    source: bytes,
    qualified_owner: str,
    owner_source_sha256: str,
) -> None:
    """Catches treating `::` inside a tracked path as the owner separator."""

    path = "src/lockstep/a::b.py"
    owner = f"{path}::{qualified_owner}"
    callsite = f"{owner}::call:0001"
    index = _fixture_index({path: source}, tmp_path)
    table = _primitive_table(
        index,
        (_primitive_callsite_row(callsite, f"reviewed.{case}"),),
    )

    assert table["callsite_evidence"] == [
        {
            "selector": callsite,
            "owner_source_sha256": owner_source_sha256,
            "call_ast_sha256": (
                "e09b5cb880470516e9778c1137bbd3689e6acc7c8527775cdc3d08c833ab678a"
            ),
        }
    ]
    result = resolve_calls(index, (), table)
    assert _resolver_target(result, callsite) == f"reviewed.{case}"


_INVALID_CALLSITE_EVIDENCE_CASES = (
    ("missing", lambda records: []),
    ("duplicate", lambda records: [records[0], records[0]]),
    (
        "orphan",
        lambda records: [
            records[0],
            {**records[0], "selector": "src/lockstep/orphan.py::owner::call:0001"},
        ],
    ),
    (
        "owner_source_mismatch",
        lambda records: [{**records[0], "owner_source_sha256": "0" * 64}],
    ),
    (
        "call_ast_mismatch",
        lambda records: [{**records[0], "call_ast_sha256": "0" * 64}],
    ),
    (
        "malformed_record",
        lambda records: [{**records[0], "extra": True}],
    ),
)


@pytest.mark.parametrize(
    ("case", "mutate"),
    _INVALID_CALLSITE_EVIDENCE_CASES,
    ids=[case for case, *_rest in _INVALID_CALLSITE_EVIDENCE_CASES],
)
def test_resolver_rule_table_rejects_invalid_callsite_evidence(
    tmp_path: Path,
    case: str,
    mutate: Callable[[list[Mapping[str, object]]], list[Mapping[str, object]]],
) -> None:
    path = f"src/lockstep/invalid_callsite_evidence_{case}.py"
    source = "def owner(callback):\n    callback()\n"
    files = {path: _resolver_source(source)}
    index = _fixture_index(files, tmp_path)
    callsite = f"{path}::owner::call:0001"
    table = dict(
        _primitive_table(
            index,
            (_primitive_callsite_row(callsite, "reviewed.callback"),),
        )
    )
    table["callsite_evidence"] = mutate(table["callsite_evidence"])

    with pytest.raises(ValueError, match=rf"^invalid callsite evidence: {case}$"):
        resolve_calls(index, (), table)


def test_resolver_rule_table_rejects_noncanonical_callsite_evidence_order(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/noncanonical_callsite_evidence.py"
    source = "def owner(first, second):\n    first()\n    second()\n"
    files = {path: _resolver_source(source)}
    index = _fixture_index(files, tmp_path)
    rows = tuple(
        _primitive_callsite_row(
            f"{path}::owner::call:{ordinal:04d}", f"reviewed.callback.{ordinal}"
        )
        for ordinal in (1, 2)
    )
    table = dict(_primitive_table(index, rows))
    table["callsite_evidence"] = list(reversed(table["callsite_evidence"]))

    with pytest.raises(ValueError, match=r"^noncanonical callsite evidence order$"):
        resolve_calls(index, (), table)


def test_resolver_callsite_evidence_invalidates_reference_source_change(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/reference_source_change.py"
    dependency = "src/lockstep/reference_dependency.py"
    source = "def owner(callback):\n    callback()\n"
    before_files = {
        path: _resolver_source(source),
        dependency: b"VALUE = 1\n",
    }
    before_index = _fixture_index(before_files, tmp_path)
    callsite = f"{path}::owner::call:0001"
    before_table = _primitive_table(
        before_index,
        (_primitive_callsite_row(callsite, "reviewed.callback"),),
    )
    after_index = _fixture_index(
        {**before_files, dependency: b"VALUE = 2\n"},
        tmp_path,
    )

    with pytest.raises(ValueError, match=r"^reference source evidence mismatch$"):
        resolve_calls(after_index, (), before_table)


def test_resolver_callsite_evidence_invalidates_changed_expression_at_same_ordinal(
    tmp_path: Path,
) -> None:
    """Catches a primitive row surviving a semantic call expression change."""

    path = "src/lockstep/call_expression_change.py"
    before_index = _fixture_index(
        {path: b"def owner(callback):\n    callback()\n"},
        tmp_path,
    )
    callsite = f"{path}::owner::call:0001"
    before_table = _primitive_table(
        before_index,
        (_primitive_callsite_row(callsite, "reviewed.callback"),),
    )
    after_index = _fixture_index(
        {path: b"def owner(replacement):\n    replacement()\n"},
        tmp_path,
    )
    table = dict(
        _primitive_table(
            after_index,
            (_primitive_callsite_row(callsite, "reviewed.callback"),),
        )
    )
    current_evidence = dict(table["callsite_evidence"][0])
    current_evidence["call_ast_sha256"] = before_table["callsite_evidence"][0][
        "call_ast_sha256"
    ]
    table["callsite_evidence"] = [current_evidence]

    with pytest.raises(
        ValueError,
        match=rf"^callsite AST evidence mismatch: {callsite}$",
    ):
        resolve_calls(after_index, (), table)


_INVALID_PRIMITIVE_DOMAIN_CASES = (
    ("empty", []),
    ("string_not_array", "filesystem-read"),
    ("duplicate", ["filesystem-read", "filesystem-read"]),
    ("unknown", ["network"]),
    ("noncanonical_order", ["filesystem-write", "filesystem-read"]),
)


@pytest.mark.parametrize(
    ("case", "domains"),
    _INVALID_PRIMITIVE_DOMAIN_CASES,
    ids=[case for case, *_rest in _INVALID_PRIMITIVE_DOMAIN_CASES],
)
def test_resolver_rule_table_rejects_invalid_primitive_domains(
    tmp_path: Path,
    case: str,
    domains: object,
) -> None:
    """Catches empty, non-array, duplicate, unknown, or misordered domains."""

    path = f"src/lockstep/invalid_domains_{case}.py"
    callsite = f"{path}::owner::call:0001"
    row = {**_primitive_callsite_row(callsite, "reviewed.callback"), "domains": domains}
    with pytest.raises(ValueError, match=rf"^invalid primitive domains: {case}$"):
        _resolver_fixture_with_primitive_rows(
            tmp_path,
            "def owner(callback):\n    callback()\n",
            (row,),
            path=path,
        )


_MALFORMED_RULE_TABLE_CASES = (
    "allowlist_schema_bool",
    "allowlist_non_array_targets",
    "allowlist_empty_target",
    "primitive_schema_bool",
    "primitive_rows_not_array",
    "primitive_empty_selector",
    "primitive_empty_semantic_target",
)


@pytest.mark.parametrize(
    "case",
    _MALFORMED_RULE_TABLE_CASES,
    ids=_MALFORMED_RULE_TABLE_CASES,
)
def test_resolver_rule_table_rejects_malformed_object_or_row(
    tmp_path: Path,
    case: str,
) -> None:
    path = f"src/lockstep/malformed_{case}.py"
    files = {path: b"def owner():\n    pass\n"}
    index = _fixture_index(files, tmp_path)
    allowlist: object = {"schema_version": 1, "targets": []}
    primitives = dict(_primitive_table(index, ()))
    expected = ""
    if case == "allowlist_schema_bool":
        allowlist = {"schema_version": True, "targets": []}
        expected = "invalid effect-free allowlist schema_version"
    elif case == "allowlist_non_array_targets":
        allowlist = {"schema_version": 1, "targets": ("builtins.len",)}
        expected = "effect-free allowlist targets must be an array"
    elif case == "allowlist_empty_target":
        allowlist = {"schema_version": 1, "targets": [""]}
        expected = "effect-free allowlist target must be non-empty"
    elif case == "primitive_schema_bool":
        primitives["schema_version"] = True
        expected = "invalid effect primitive schema_version"
    elif case == "primitive_rows_not_array":
        primitives["rows"] = ()
        expected = "effect primitive rows must be an array"
    elif case == "primitive_empty_selector":
        primitives["rows"] = [_primitive_entity_row("")]
        expected = "effect primitive selector must be non-empty"
    elif case == "primitive_empty_semantic_target":
        primitives["rows"] = [
            {
                **_primitive_entity_row("external.target"),
                "semantic_target": "",
            }
        ]
        expected = "effect primitive semantic_target must be non-empty"

    primitive_input: object = () if case.startswith("allowlist_") else primitives
    with pytest.raises(ValueError, match=rf"^{expected}$"):
        resolve_calls(index, allowlist, primitive_input)


_STRUCTURALLY_MALFORMED_PRIMITIVE_ROWS = (
    ("non_object", "row", "invalid effect primitive row"),
    (
        "extra_key",
        {**_primitive_entity_row("external.target"), "extra": True},
        "invalid effect primitive row",
    ),
    (
        "invalid_selector_kind",
        {
            **_primitive_entity_row("external.target"),
            "selector_kind": "pattern",
        },
        "invalid primitive selector kind",
    ),
)


@pytest.mark.parametrize(
    ("case", "row", "expected"),
    _STRUCTURALLY_MALFORMED_PRIMITIVE_ROWS,
    ids=[case for case, *_rest in _STRUCTURALLY_MALFORMED_PRIMITIVE_ROWS],
)
def test_resolver_rule_table_rejects_structurally_malformed_row_before_evidence(
    tmp_path: Path,
    case: str,
    row: object,
    expected: str,
) -> None:
    with pytest.raises(ValueError, match=rf"^{expected}$"):
        _resolver_fixture(
            tmp_path,
            "def owner():\n    pass\n",
            path=f"src/lockstep/structural_row_{case}.py",
            primitives=(row,),
        )


_UNUSED_PRIMITIVE_CASES = (
    (
        "unused_entity",
        "def target():\n    pass\ndef owner():\n    target()\n",
        lambda _path: _primitive_entity_row("external.unused"),
        "unused primitive row",
    ),
    (
        "static_target_at_callsite",
        "def target():\n    pass\ndef owner():\n    target()\n",
        lambda path: _primitive_callsite_row(
            f"{path}::owner::call:0001", "reviewed.stale"
        ),
        "stale callsite primitive row",
    ),
)


@pytest.mark.parametrize(
    ("case", "source", "row_factory", "reason"),
    _UNUSED_PRIMITIVE_CASES,
    ids=[case for case, *_rest in _UNUSED_PRIMITIVE_CASES],
)
def test_resolver_rule_table_rejects_unused_or_stale_primitive_rows(
    tmp_path: Path,
    case: str,
    source: str,
    row_factory: Callable[[str], Mapping[str, object]],
    reason: str,
) -> None:
    path = f"src/lockstep/unused_primitive_{case}.py"
    row = row_factory(path)
    selector = row["selector"]
    assert isinstance(selector, str)
    with pytest.raises(
        ValueError,
        match=rf"^{re.escape(reason)}: {re.escape(selector)}$",
    ):
        _resolver_fixture_with_primitive_rows(
            tmp_path,
            source,
            (row,),
            path=path,
        )


_STATIC_EXTERNAL_TARGET_CASES = (
    (
        "os_open",
        "os",
        "os.open('/tmp/item', os.O_RDONLY)",
        "os.open",
        ("filesystem-read",),
    ),
    (
        "os_fsync",
        "os",
        "os.fsync(3)",
        "os.fsync",
        ("filesystem-write", "durable-state"),
    ),
    (
        "os_replace",
        "os",
        "os.replace('old', 'new')",
        "os.replace",
        ("filesystem-write",),
    ),
    (
        "fcntl_flock",
        "fcntl",
        "fcntl.flock(3, fcntl.LOCK_EX)",
        "fcntl.flock",
        ("synchronization",),
    ),
    (
        "subprocess_run",
        "subprocess",
        "subprocess.run(['tool'])",
        "subprocess.run",
        ("external-process/provider",),
    ),
    ("os_read", "os", "os.read(3, 1)", "os.read", ("filesystem-read",)),
    (
        "os_write",
        "os",
        "os.write(3, b'x')",
        "os.write",
        ("filesystem-write",),
    ),
    ("time_sleep", "time", "time.sleep(1)", "time.sleep", ("lifecycle-control",)),
    (
        "multiprocessing_process",
        "multiprocessing",
        "multiprocessing.Process()",
        "multiprocessing.Process",
        ("external-process/provider", "lifecycle-control"),
    ),
    (
        "subprocess_popen",
        "subprocess",
        "subprocess.Popen(['tool'])",
        "subprocess.Popen",
        ("external-process/provider", "lifecycle-control"),
    ),
    (
        "arbitrary_nested_module",
        "acme.transport",
        "acme.transport.send()",
        "acme.transport.send",
        ("external-process/provider",),
    ),
    (
        "arbitrary_vendor_gateway",
        "vendor.gateway",
        "vendor.gateway.dispatch()",
        "vendor.gateway.dispatch",
        ("external-process/provider",),
    ),
    (
        "arbitrary_external_constructor",
        "custom_service",
        "custom_service.Factory()",
        "custom_service.Factory",
        ("lifecycle-control",),
    ),
)


@pytest.mark.parametrize(
    ("case", "module", "expression", "target", "domains"),
    _STATIC_EXTERNAL_TARGET_CASES,
    ids=[case for case, *_rest in _STATIC_EXTERNAL_TARGET_CASES],
)
def test_resolver_effect_closure_requires_exact_coverage_for_any_external_target(
    tmp_path: Path,
    case: str,
    module: str,
    expression: str,
    target: str,
    domains: tuple[str, ...],
) -> None:
    """Catches treating a resolved external effect as implicitly pure."""

    path = f"src/lockstep/external_effect_{case}.py"
    source = f"import {module}\ndef owner():\n    {expression}\n"
    assert tuple(sorted(domains, key=_EFFECT_DOMAINS.index)) == domains
    with pytest.raises(
        ValueError,
        match=rf"^external target lacks exact effect coverage: {target}$",
    ):
        _resolver_fixture(tmp_path, source, path=path)


_EXTERNAL_COVERAGE_KINDS = ("allowlist", "entity", "callsite")


@pytest.mark.parametrize(
    "coverage_kind",
    _EXTERNAL_COVERAGE_KINDS,
    ids=_EXTERNAL_COVERAGE_KINDS,
)
def test_resolver_effect_closure_accepts_each_exact_external_coverage_kind(
    tmp_path: Path,
    coverage_kind: str,
) -> None:
    path = f"src/lockstep/external_coverage_{coverage_kind}.py"
    source = "import external_api as api\ndef owner():\n    api.perform()\n"
    target = "external_api.perform"
    callsite = f"{path}::owner::call:0001"
    if coverage_kind == "allowlist":
        covered = _resolver_fixture(
            tmp_path,
            source,
            path=path,
            allowlist=frozenset({target}),
        )
    else:
        row = (
            _primitive_entity_row(target)
            if coverage_kind == "entity"
            else {
                **_primitive_callsite_row(callsite, target),
                "domains": ["external-process/provider"],
            }
        )
        covered = _resolver_fixture_with_primitive_rows(
            tmp_path,
            source,
            (row,),
            path=path,
        )

    assert _resolver_target(covered, callsite) == target


def test_resolver_effect_closure_entity_coverage_is_exact_not_prefix_based(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/external_inexact_entity.py"
    target = "external_api.perform"
    with pytest.raises(
        ValueError,
        match=rf"^external target lacks exact effect coverage: {target}$",
    ):
        _resolver_fixture_with_primitive_rows(
            tmp_path,
            "import external_api as api\ndef owner():\n    api.perform()\n",
            (_primitive_entity_row("external_api"),),
            path=path,
        )


def test_resolver_callsite_effect_free_allowlist_matches_exact_builtin_target(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/allowlist.py"
    exact = _resolver_fixture(
        tmp_path,
        """
        def owner(items):
            len(items)
        """,
        path=path,
        allowlist=frozenset({"builtins.len"}),
    )
    assert _resolver_target(exact, f"{path}::owner::call:0001") == "builtins.len"

    for inexact in (frozenset({"len"}), frozenset({"builtins.length"})):
        unresolved = _resolver_fixture(
            tmp_path,
            """
            def owner(items):
                len(items)
            """,
            path=path,
            allowlist=inexact,
        )
        _assert_unresolved_call(unresolved, f"{path}::owner::call:0001")


_IMPORTED_BUILTIN_EFFECT_CASES = (
    (
        "module_attribute_open",
        "import builtins",
        "builtins.open('item', 'rb')",
        "builtins.open",
        "callsite",
        ("filesystem-read", "lifecycle-control"),
    ),
    (
        "from_import_input",
        "from builtins import input",
        "input()",
        "builtins.input",
        "entity",
        ("decode/validate",),
    ),
)


@pytest.mark.parametrize(
    ("case", "statement", "expression", "target", "coverage_kind", "domains"),
    _IMPORTED_BUILTIN_EFFECT_CASES,
    ids=[case for case, *_rest in _IMPORTED_BUILTIN_EFFECT_CASES],
)
def test_resolver_imported_builtin_effect_requires_exact_coverage(
    tmp_path: Path,
    case: str,
    statement: str,
    expression: str,
    target: str,
    coverage_kind: str,
    domains: tuple[str, ...],
) -> None:
    """Catches treating imported effectful builtins as intrinsically pure."""

    path = f"src/lockstep/imported_builtin_{case}.py"
    source = f"{statement}\ndef owner():\n    {expression}\n"
    with pytest.raises(
        ValueError,
        match=rf"^external target lacks exact effect coverage: {re.escape(target)}$",
    ):
        _resolver_fixture_with_primitive_rows(
            tmp_path,
            source,
            (),
            path=path,
            allowlist={"schema_version": 1, "targets": []},
        )


@pytest.mark.parametrize(
    ("case", "statement", "expression", "target", "coverage_kind", "domains"),
    _IMPORTED_BUILTIN_EFFECT_CASES,
    ids=[case for case, *_rest in _IMPORTED_BUILTIN_EFFECT_CASES],
)
def test_resolver_imported_builtin_effect_accepts_exact_coverage(
    tmp_path: Path,
    case: str,
    statement: str,
    expression: str,
    target: str,
    coverage_kind: str,
    domains: tuple[str, ...],
) -> None:
    """Catches blacklisting imported builtin spelling instead of requiring coverage."""

    path = f"src/lockstep/imported_builtin_{case}.py"
    source = f"{statement}\ndef owner():\n    {expression}\n"
    callsite = f"{path}::owner::call:0001"
    row = (
        _primitive_entity_row(target, domains)
        if coverage_kind == "entity"
        else {
            **_primitive_callsite_row(callsite, target),
            "domains": list(domains),
        }
    )
    covered = _resolver_fixture_with_primitive_rows(
        tmp_path,
        source,
        (row,),
        path=path,
        allowlist={"schema_version": 1, "targets": []},
    )
    assert _resolver_target(covered, callsite) == target


def test_resolver_callsite_primitive_is_an_exact_terminal_override(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/callsite_primitive.py"
    callsite = f"{path}::owner::call:0001"
    source = """
        def owner(callback):
            callback()
    """
    exact = _resolver_fixture_with_primitive_rows(
        tmp_path,
        source,
        (_primitive_callsite_row(callsite, "reviewed.callback"),),
        path=path,
    )
    assert _resolver_target(exact, callsite) == "reviewed.callback"


def test_resolver_callsite_and_entity_primitive_selector_spaces_are_disjoint(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/disjoint_selectors.py"
    callsite = f"{path}::owner::call:0001"
    rows = (
        _primitive_callsite_row(callsite, "reviewed.callback"),
        {
            **_primitive_callsite_row(callsite, "reviewed.callback"),
            "selector_kind": "entity",
        },
    )

    with pytest.raises(ValueError, match=r"^primitive selector spaces overlap$"):
        _resolver_fixture_with_primitive_rows(
            tmp_path,
            """
            def owner(callback):
                callback()
            """,
            rows,
            path=path,
        )


def test_resolver_callsite_primitive_is_invalidated_by_source_ordinal_change(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/ordinal_invalidation.py"
    stale_callsite = f"{path}::owner::call:0002"
    row = _primitive_callsite_row(stale_callsite, "reviewed.callback")
    before_source = _resolver_source(
        """
        def owner(callback):
            other()
            callback()
        """
    )
    before_index = _fixture_index({path: before_source}, tmp_path)
    table = _primitive_table(before_index, (row,))
    before = resolve_calls(before_index, (), table)
    assert _resolver_target(before, stale_callsite) == "reviewed.callback"

    with pytest.raises(ValueError, match=r"^reference source evidence mismatch$"):
        _resolver_fixture(
            tmp_path,
            """
            def owner(callback):
                callback()
            """,
            path=path,
            primitives=table,
        )


def test_resolver_callsite_primitive_cannot_override_new_static_semantics(
    tmp_path: Path,
) -> None:
    """Catches a stale callsite row overriding a newly exact lexical target."""

    path = "src/lockstep/semantic_invalidation.py"
    callsite = f"{path}::owner::call:0001"
    row = _primitive_callsite_row(callsite, "reviewed.callback")
    with pytest.raises(
        ValueError,
        match=rf"^stale callsite primitive row: {callsite}$",
    ):
        _resolver_fixture_with_primitive_rows(
            tmp_path,
            """
            def target():
                pass
            def owner():
                target()
            """,
            (row,),
            path=path,
        )


def test_resolver_result_records_aliases_and_receivers_are_deeply_immutable(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/immutable_resolution.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def target():
            pass
        class Worker:
            def run(self):
                pass
        def owner():
            alias = target
            alias()
            worker = Worker()
            worker.run()
            unknown()
        """,
        path=path,
    )

    assert type(result).__name__ == "ResolutionIndex"
    assert {
        "calls",
        "aliases",
        "receivers",
    } <= {field.name for field in fields(result)}
    assert isinstance(result.aliases, Mapping)
    assert isinstance(result.receivers, Mapping)
    assert f"{path}::target" in result.aliases.values()
    assert f"{path}::Worker" in result.receivers.values()
    resolved = _records_named(result, "ResolvedCall")
    unresolved = _records_named(result, "UnresolvedCall")
    assert {field.name for field in fields(resolved[0])} == {"callsite", "target"}
    assert {field.name for field in fields(unresolved[0])} == {
        "callsite",
        "line",
        "column",
        "ast_dump",
    }
    _assert_deeply_immutable(result)


def test_resolver_dependency_result_records_and_mappings_are_deeply_immutable(
    tmp_path: Path,
) -> None:
    """Catches a mutable dependency map or records outside ResolutionIndex."""

    path = "src/lockstep/immutable_dependencies.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def target(value):
            return value
        @target
        @missing_decorator
        def owner():
            pass
        """,
        path=path,
    )

    assert tuple(field.name for field in fields(result)) == (
        "calls",
        "aliases",
        "receivers",
        "dependencies",
        "reference_source_sha256",
        "call_evidence",
    )
    assert type(result).__slots__ == tuple(
        field.name for field in fields(result)
    )
    dependencies = _resolver_dependencies(result)
    resolved_dependencies = _records_named(result, "ResolvedDependency")
    unresolved_dependencies = _records_named(result, "UnresolvedDependency")
    assert tuple(dependencies) == (
        f"{path}::owner::dependency:0001",
        f"{path}::owner::dependency:0002",
    )
    assert {field.name for field in fields(resolved_dependencies[0])} == {
        "reference",
        "owner",
        "kind",
        "target",
    }
    assert {field.name for field in fields(unresolved_dependencies[0])} == {
        "reference",
        "owner",
        "kind",
        "line",
        "column",
        "ast_dump",
    }
    _assert_deeply_immutable(result)


def test_resolver_call_evidence_records_are_public_frozen_and_slotted() -> None:
    positional_type = call_resolver.PositionalLiteralEvidence
    keyword_type = call_resolver.KeywordLiteralEvidence
    callsite_type = call_resolver.CallsiteEvidence
    positional = positional_type(0, "int", 7)
    keyword = keyword_type("flag", "bool", True)
    callsite = callsite_type(
        "src/lockstep/sample.py::owner::call:0001",
        "src/lockstep/sample.py::owner",
        3,
        4,
        (positional,),
        (keyword,),
    )

    assert tuple(field.name for field in fields(positional)) == (
        "index", "type", "value"
    )
    assert tuple(field.name for field in fields(keyword)) == (
        "name", "type", "value"
    )
    assert tuple(field.name for field in fields(callsite)) == (
        "callsite", "owner", "line", "column", "positional", "keywords"
    )
    for record in (positional, keyword, callsite):
        assert type(record).__slots__ == tuple(
            field.name for field in fields(record)
        )
        _assert_deeply_immutable(record)


def test_resolver_call_evidence_preserves_all_owner_keys_and_iteration_order(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/evidence_owners.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def owner():
            body()
            local_lambda = lambda: function_lambda()
            class Nested:
                class_owned = lambda: nested_class_lambda()

        class Box:
            class_owned = lambda: class_lambda()

        file_owned = lambda: file_lambda()
        file_call()
        """,
        path=path,
    )

    assert tuple(result.call_evidence) == tuple(result.calls)
    owners = {
        evidence.owner for evidence in result.call_evidence.values()
    }
    assert owners == {
        f"{path}::owner",
        f"{path}::owner.Nested",
        f"{path}::Box",
        f"{path}::@file",
    }
    assert all(
        evidence.callsite == callsite
        and evidence.owner == callsite.rsplit("::call:", 1)[0]
        for callsite, evidence in result.call_evidence.items()
    )


def test_resolver_emits_exact_ordered_literal_evidence_for_every_callsite(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/literal_evidence.py"
    source = _resolver_source(
        """
        def owner(callback, dynamic):
            callback(None, dynamic, True, 1.5, 7, "text", *(),
                     named="ok", flag=False, count=3, bad=dynamic, **{})
        """
    )
    index = _fixture_index({path: source}, tmp_path)
    result = resolve_calls(index, (), ())
    callsite = f"{path}::owner::call:0001"

    assert tuple(result.call_evidence) == tuple(result.calls) == (callsite,)
    evidence = result.call_evidence[callsite]
    assert evidence.callsite == callsite
    assert evidence.owner == f"{path}::owner"
    assert evidence.line > 0
    assert evidence.column >= 0
    assert tuple(
        (item.index, item.type, item.value) for item in evidence.positional
    ) == (
        (0, "null", None),
        (2, "bool", True),
        (4, "int", 7),
        (5, "str", "text"),
    )
    assert tuple(
        (item.name, item.type, item.value) for item in evidence.keywords
    ) == (
        ("named", "str", "ok"),
        ("flag", "bool", False),
        ("count", "int", 3),
    )
    assert tuple(type(item.value) for item in evidence.positional) == (
        type(None),
        bool,
        int,
        str,
    )
    assert tuple(type(item.value) for item in evidence.keywords) == (
        str,
        bool,
        int,
    )
    _assert_deeply_immutable(result)


def test_resolver_binds_resolution_index_to_exact_source_population(
    tmp_path: Path,
) -> None:
    files = {
        "src/lockstep/zeta.py": b"def zeta():\n    pass\n",
        "src/lockstep/alpha.py": b"def alpha():\n    pass\n",
    }
    index = _fixture_index(files, tmp_path)
    result = resolve_calls(index, (), ())
    population = [
        {"path": path, "source_sha256": index.file_sha256[path]}
        for path in sorted(index.files)
    ]

    assert result.reference_source_sha256 == _canonical_sha256(population)
    assert isinstance(result.call_evidence, MappingProxyType)


def test_resolver_accepts_only_exact_internal_entity_primitive_binding(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/internal_entity_primitive.py"
    target = f"{path}::target"
    source = """
        def target():
            pass
        def owner():
            target()
    """
    accepted = _resolver_fixture_with_primitive_rows(
        tmp_path, source, (_primitive_entity_row(target),), path=path
    )
    assert _resolver_target(accepted, f"{path}::owner::call:0001") == target

    mismatched = {
        **_primitive_entity_row(target),
        "semantic_target": f"{path}::other",
    }
    with pytest.raises(ValueError, match="internal entity primitive"):
        _resolver_fixture_with_primitive_rows(
            tmp_path, source, (mismatched,), path=path
        )


def test_resolver_dependency_records_are_public_frozen_and_slotted() -> None:
    """Catches private, mutable, or shape-drifting dependency evidence records."""

    resolved_type = call_resolver.ResolvedDependency
    unresolved_type = call_resolver.UnresolvedDependency
    resolved = resolved_type("owner::dependency:0001", "owner", "decorator", "target")
    unresolved = unresolved_type(
        "owner::dependency:0002",
        "owner",
        "base",
        7,
        11,
        "Name(id='unknown', ctx=Load())",
    )

    assert tuple(field.name for field in fields(resolved)) == (
        "reference",
        "owner",
        "kind",
        "target",
    )
    assert tuple(field.name for field in fields(unresolved)) == (
        "reference",
        "owner",
        "kind",
        "line",
        "column",
        "ast_dump",
    )
    assert resolved_type.__slots__ == tuple(field.name for field in fields(resolved))
    assert unresolved_type.__slots__ == tuple(field.name for field in fields(unresolved))
    _assert_deeply_immutable(resolved)
    _assert_deeply_immutable(unresolved)


def test_resolver_dependency_resolves_exact_symbols_imports_classes_and_aliases(
    tmp_path: Path,
) -> None:
    """Catches a dependency path that does not reuse the closed symbol rules."""

    path = "src/lockstep/dependency_exact_bindings.py"
    result = _resolver_fixture(
        tmp_path,
        """
        import package.decorators as decorators

        def local_decorator(value):
            return value

        class LocalBase:
            pass

        class LocalMeta:
            pass

        decorator_alias = local_decorator
        base_alias = LocalBase
        meta_alias = LocalMeta

        @decorator_alias
        def sync_owner():
            pass

        @decorators.decorate
        async def async_owner():
            pass

        @decorator_alias
        class Child(base_alias, option=local_decorator, metaclass=meta_alias):
            @local_decorator
            def method(self):
                pass
        """,
        path=path,
    )

    expected = {
        f"{path}::sync_owner::dependency:0001": (
            f"{path}::sync_owner",
            "decorator",
            f"{path}::local_decorator",
        ),
        f"{path}::async_owner::dependency:0001": (
            f"{path}::async_owner",
            "decorator",
            "package.decorators.decorate",
        ),
        f"{path}::Child::dependency:0001": (
            f"{path}::Child",
            "decorator",
            f"{path}::local_decorator",
        ),
        f"{path}::Child::dependency:0002": (
            f"{path}::Child",
            "base",
            f"{path}::LocalBase",
        ),
        f"{path}::Child::dependency:0003": (
            f"{path}::Child",
            "metaclass",
            f"{path}::LocalMeta",
        ),
        f"{path}::Child.method::dependency:0001": (
            f"{path}::Child.method",
            "decorator",
            f"{path}::local_decorator",
        ),
    }
    dependencies = _resolver_dependencies(result)

    assert set(dependencies) == set(expected)
    assert {
        reference: (record.owner, record.kind, record.target)
        for reference, record in dependencies.items()
    } == expected


_RELATIVE_DEPENDENCY_IMPORT_CASES = (
    (
        "current_package_symbol",
        "src/lockstep/pkg/sub/consumer.py",
        "from .dependency import decorate",
        "decorate",
        "src/lockstep/pkg/sub/dependency.py",
        "src/lockstep/pkg/sub/dependency.py::decorate",
    ),
    (
        "parent_package_symbol",
        "src/lockstep/pkg/sub/consumer.py",
        "from ..dependency import decorate",
        "decorate",
        "src/lockstep/pkg/dependency.py",
        "src/lockstep/pkg/dependency.py::decorate",
    ),
    (
        "relative_only_module",
        "src/lockstep/pkg/sub/consumer.py",
        "from . import dependency",
        "dependency.decorate",
        "src/lockstep/pkg/sub/dependency.py",
        "src/lockstep/pkg/sub/dependency.py::decorate",
    ),
)


@pytest.mark.parametrize(
    ("case", "path", "statement", "expression", "dependency_path", "expected"),
    _RELATIVE_DEPENDENCY_IMPORT_CASES,
    ids=[case for case, *_rest in _RELATIVE_DEPENDENCY_IMPORT_CASES],
)
def test_resolver_dependency_normalizes_relative_imports(
    tmp_path: Path,
    case: str,
    path: str,
    statement: str,
    expression: str,
    dependency_path: str,
    expected: str,
) -> None:
    """Catches dependency targets that discard ImportFrom package level."""

    result = _resolver_fixture(
        tmp_path,
        f"{statement}\n@{expression}\ndef owner():\n    pass\n",
        path=path,
        extra_files={dependency_path: "def decorate(value):\n    return value\n"},
    )
    reference = f"{path}::owner::dependency:0001"

    assert _resolver_dependency_target(result, reference) == expected, case


def test_resolver_dependency_reexport_target_changes_without_reference_drift(
    tmp_path: Path,
) -> None:
    """Catches stale semantic targets hidden behind a stable re-export alias."""

    path = "src/lockstep/reexport_consumer.py"
    source = "from lockstep.provider import Exported\n@Exported\ndef owner():\n    pass\n"
    common = {
        "src/lockstep/first.py": "def Decorator(value):\n    return value\n",
        "src/lockstep/second.py": "def Decorator(value):\n    return value\n",
    }
    before = _resolver_fixture(
        tmp_path,
        source,
        path=path,
        extra_files={
            **common,
            "src/lockstep/provider.py": (
                "from lockstep.first import Decorator as Exported\n"
            ),
        },
    )
    after = _resolver_fixture(
        tmp_path,
        source,
        path=path,
        extra_files={
            **common,
            "src/lockstep/provider.py": (
                "from lockstep.second import Decorator as Exported\n"
            ),
        },
    )
    reference = f"{path}::owner::dependency:0001"

    assert _resolver_dependency_target(
        before, reference
    ) == "src/lockstep/first.py::Decorator"
    assert _resolver_dependency_target(
        after, reference
    ) == "src/lockstep/second.py::Decorator"


def test_resolver_dependency_resolves_covered_external_factory_decorator(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/external_factory_decorator.py"
    result = _resolver_fixture(
        tmp_path,
        "from dataclasses import dataclass\n@dataclass(frozen=True)\nclass Owner:\n    pass\n",
        path=path,
        allowlist=("dataclasses.dataclass",),
    )

    assert _resolver_dependency_target(
        result, f"{path}::Owner::dependency:0001"
    ) == "dataclasses.dataclass"


def test_resolver_dependency_normalizes_parameterized_base_to_origin(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/parameterized_base.py"
    result = _resolver_fixture(
        tmp_path,
        "from collections.abc import Mapping\nclass Owner(Mapping[str, object]):\n    pass\n",
        path=path,
    )

    assert _resolver_dependency_target(
        result, f"{path}::Owner::dependency:0001"
    ) == "collections.abc.Mapping"


def test_resolver_local_parameterized_base_stays_out_of_dependency_and_dispatch(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/local_parameterized_base.py"
    result = _resolver_fixture(
        tmp_path,
        "class Base:\n    def inherited(self): pass\n"
        "class Owner(Base[int]):\n    def run(self): self.inherited()\n",
        path=path,
    )

    _assert_unresolved_dependency(
        result, f"{path}::Owner::dependency:0001")
    _assert_unresolved_call(result, f"{path}::Owner.run::call:0001")


def test_resolver_dependency_owner_preorder_prunes_nested_owners_and_path_delimiters(
    tmp_path: Path,
) -> None:
    """Catches nested leakage, class-field order drift, and first-delimiter splits."""

    path = "src/lockstep/a::dependency_owners.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def outer_decorator(value):
            return value
        def nested_decorator(value):
            return value
        class Base:
            pass
        class Meta:
            pass

        @outer_decorator
        @nested_decorator
        def outer():
            @nested_decorator
            def nested():
                pass

            @nested_decorator
            class Nested(Base, metaclass=Meta):
                pass
        """,
        path=path,
    )
    dependencies = _resolver_dependencies(result)

    expected_by_owner = {
        f"{path}::outer": (
            ("decorator", f"{path}::outer_decorator"),
            ("decorator", f"{path}::nested_decorator"),
        ),
        f"{path}::outer.nested": (
            ("decorator", f"{path}::nested_decorator"),
        ),
        f"{path}::outer.Nested": (
            ("decorator", f"{path}::nested_decorator"),
            ("base", f"{path}::Base"),
            ("metaclass", f"{path}::Meta"),
        ),
    }
    assert set(dependencies) == {
        f"{owner}::dependency:{ordinal:04d}"
        for owner, expected in expected_by_owner.items()
        for ordinal in range(1, len(expected) + 1)
    }
    for owner, expected in expected_by_owner.items():
        assert tuple(
            (
                dependencies[f"{owner}::dependency:{ordinal:04d}"].kind,
                dependencies[f"{owner}::dependency:{ordinal:04d}"].target,
            )
            for ordinal in range(1, len(expected) + 1)
        ) == expected
    assert all(
        record.reference.startswith(record.owner + "::dependency:")
        for record in dependencies.values()
    )


def test_resolver_dependency_accepts_9999_references_per_owner(
    tmp_path: Path,
) -> None:
    """Catches rejecting the last valid four-digit dependency ordinal."""

    path = "src/lockstep/dependency_limit.py"
    source = (
        "def Decorator(value):\n"
        "    return value\n"
        + "@Decorator\n" * 9_999
        + "def owner():\n"
        "    pass\n"
    )
    dependencies = _resolver_dependencies(
        _resolver_fixture(tmp_path, source, path=path)
    )

    assert len(dependencies) == 9_999
    assert tuple(dependencies)[-1] == f"{path}::owner::dependency:9999"


def test_resolver_dependency_rejects_reference_10000_per_owner(
    tmp_path: Path,
) -> None:
    """Catches emitting an unstable five-digit dependency reference."""

    path = "src/lockstep/dependency_overflow.py"
    source = (
        "def Decorator(value):\n"
        "    return value\n"
        + "@Decorator\n" * 10_000
        + "def owner():\n"
        "    pass\n"
    )

    with pytest.raises(ValueError):
        _resolver_fixture(tmp_path, source, path=path)


def test_resolver_dependency_limit_is_per_owner_not_index(tmp_path: Path) -> None:
    """Catches applying the four-digit bound across independent owners."""

    path = "src/lockstep/dependency_multi_owner_limit.py"
    source = (
        "def Decorator(value):\n"
        "    return value\n"
        + "@Decorator\n" * 5_000
        + "def first():\n"
        "    pass\n"
        + "@Decorator\n" * 5_000
        + "def second():\n"
        "    pass\n"
    )
    dependencies = _resolver_dependencies(
        _resolver_fixture(tmp_path, source, path=path)
    )

    assert len(dependencies) == 10_000
    assert f"{path}::first::dependency:5000" in dependencies
    assert f"{path}::second::dependency:5000" in dependencies


_DEPENDENCY_FAIL_CLOSED_CASES = (
    (
        "decorator_subscript",
        "decorators = ()\n@decorators[0]\ndef Owner():\n    pass\n",
        "Owner",
        "decorator",
        2,
        1,
        "Subscript(value=Name(id='decorators', ctx=Load()), slice=Constant(value=0), ctx=Load())",
    ),
    (
        "decorator_reflection",
        "import package\n@getattr(package, 'decorate')\ndef Owner():\n    pass\n",
        "Owner",
        "decorator",
        2,
        1,
        "Call(func=Name(id='getattr', ctx=Load()), args=[Name(id='package', ctx=Load()), Constant(value='decorate')], keywords=[])",
    ),
    (
        "star_base",
        "bases = ()\nclass Owner(*bases):\n    pass\n",
        "Owner",
        "base",
        2,
        12,
        "Starred(value=Name(id='bases', ctx=Load()), ctx=Load())",
    ),
    (
        "ambiguous_rebound_base",
        "class First:\n    pass\nclass Second:\n    pass\nbase = First\nbase = Second\nclass Owner(base):\n    pass\n",
        "Owner",
        "base",
        7,
        12,
        "Name(id='base', ctx=Load())",
    ),
    (
        "conditional_metaclass_alias",
        "class First:\n    pass\nclass Second:\n    pass\nif flag:\n    meta = First\nelse:\n    meta = Second\nclass Owner(metaclass=meta):\n    pass\n",
        "Owner",
        "metaclass",
        9,
        22,
        "Name(id='meta', ctx=Load())",
    ),
    (
        "star_import_decorator",
        "from package import *\n@decorate\ndef Owner():\n    pass\n",
        "Owner",
        "decorator",
        2,
        1,
        "Name(id='decorate', ctx=Load())",
    ),
)


@pytest.mark.parametrize(
    (
        "case",
        "source",
        "owner_name",
        "kind",
        "line",
        "column",
        "ast_dump",
    ),
    _DEPENDENCY_FAIL_CLOSED_CASES,
    ids=[case for case, *_rest in _DEPENDENCY_FAIL_CLOSED_CASES],
)
def test_resolver_dependency_dynamic_ambiguous_and_star_forms_fail_closed(
    tmp_path: Path,
    case: str,
    source: str,
    owner_name: str,
    kind: str,
    line: int,
    column: int,
    ast_dump: str,
) -> None:
    """Catches guessing dependency targets outside exact Name/Attribute rules."""

    path = f"src/lockstep/dependency_fail_closed_{case}.py"
    reference = f"{path}::{owner_name}::dependency:0001"
    record = _assert_unresolved_dependency(
        _resolver_fixture(tmp_path, source, path=path), reference
    )

    assert (
        record.reference,
        record.owner,
        record.kind,
        record.line,
        record.column,
        record.ast_dump,
    ) == (
        reference,
        f"{path}::{owner_name}",
        kind,
        line,
        column,
        ast_dump,
    )
    assert not hasattr(record, "target")


def test_resolver_dependency_plain_unresolved_names_keep_exact_owner_preorder(
    tmp_path: Path,
) -> None:
    """Catches dropping plain Names or assigning base before decorator."""

    path = "src/lockstep/dependency_plain_unresolved_names.py"
    owner = f"{path}::Owner"
    result = _resolver_fixture(
        tmp_path,
        """
        @missing_decorator
        class Owner(missing_base):
            pass
        """,
        path=path,
    )
    dependencies = _resolver_dependencies(result)
    references = (
        f"{owner}::dependency:0001",
        f"{owner}::dependency:0002",
    )

    assert tuple(dependencies) == references
    assert tuple(
        (
            dependencies[reference].reference,
            dependencies[reference].owner,
            dependencies[reference].kind,
            dependencies[reference].line,
            dependencies[reference].column,
            dependencies[reference].ast_dump,
        )
        for reference in references
    ) == (
        (
            references[0],
            owner,
            "decorator",
            1,
            1,
            "Name(id='missing_decorator', ctx=Load())",
        ),
        (
            references[1],
            owner,
            "base",
            2,
            12,
            "Name(id='missing_base', ctx=Load())",
        ),
    )
    assert all(
        not hasattr(dependencies[reference], "target")
        for reference in references
    )


def test_resolver_dependency_unresolved_evidence_keeps_expression_calls_once(
    tmp_path: Path,
) -> None:
    """Catches resolving dynamic dependency expressions or replacing their callsites."""

    path = "src/lockstep/dependency_expression_calls.py"
    result = _resolver_fixture(
        tmp_path,
        """
        def factory():
            pass
        def metaclass_factory():
            pass
        @factory()
        def decorated():
            pass
        class Dynamic(
            factory(),
            metaclass=metaclass_factory(),
        ):
            pass
        """,
        path=path,
    )
    expected_dependencies = {
        f"{path}::decorated::dependency:0001": (
            f"{path}::decorated",
            "decorator",
            5,
            1,
            "Call(func=Name(id='factory', ctx=Load()), args=[], keywords=[])",
        ),
        f"{path}::Dynamic::dependency:0001": (
            f"{path}::Dynamic",
            "base",
            9,
            4,
            "Call(func=Name(id='factory', ctx=Load()), args=[], keywords=[])",
        ),
        f"{path}::Dynamic::dependency:0002": (
            f"{path}::Dynamic",
            "metaclass",
            10,
            14,
            "Call(func=Name(id='metaclass_factory', ctx=Load()), args=[], keywords=[])",
        ),
    }
    dependencies = _resolver_dependencies(result)

    assert set(dependencies) == set(expected_dependencies)
    assert {
        reference: (
            record.owner,
            record.kind,
            record.line,
            record.column,
            record.ast_dump,
        )
        for reference, record in dependencies.items()
    } == expected_dependencies
    assert tuple(_resolver_calls(result)) == (
        f"{path}::decorated::call:0001",
        f"{path}::Dynamic::call:0001",
        f"{path}::Dynamic::call:0002",
    )
    assert (
        _resolver_target(result, f"{path}::decorated::call:0001"),
        _resolver_target(result, f"{path}::Dynamic::call:0001"),
        _resolver_target(result, f"{path}::Dynamic::call:0002"),
    ) == (
        f"{path}::factory",
        f"{path}::factory",
        f"{path}::metaclass_factory",
    )


_LIFECYCLE_TRANSITIONS = (
    ("owner/provisioning", "owner.capture", ("absent",), "captured"),
    ("owner/provisioning", "owner.replace", ("captured",), "captured"),
    ("owner/provisioning", "owner.revoke", ("captured",), "revoked"),
    ("admission/commitment", "admission.admit", ("planned",), "admitted"),
    ("admission/commitment", "admission.park", ("admitted",), "parked"),
    ("admission/commitment", "commitment.hold", ("admitted",), "held"),
    ("admission/commitment", "commitment.commit", ("held",), "committed"),
    ("process-execution", "process.prepare", ("absent",), "prepared"),
    ("process-execution", "process.launch", ("prepared",), "launching"),
    ("process-execution", "process.running", ("launching",), "running"),
    ("process-execution", "process.terminal", ("running",), "terminal"),
    (
        "process-execution",
        "process.indeterminate",
        ("launching", "running"),
        "indeterminate",
    ),
    (
        "process-execution",
        "process.cancel",
        ("prepared", "launching", "running"),
        "cancelled",
    ),
    ("artifact/acceptance", "artifact.register", ("declared",), "registered"),
    (
        "artifact/acceptance",
        "artifact.materialize",
        ("registered",),
        "materialized",
    ),
    ("artifact/acceptance", "consent.issue", ("pending",), "issued"),
    ("artifact/acceptance", "consent.redeem", ("issued",), "redeemed"),
    ("publication", "publication.prepare", ("absent",), "prepared"),
    ("publication", "publication.apply", ("prepared",), "applied"),
    ("publication", "publication.rollback", ("prepared",), "rolled-back"),
    ("delivery", "delivery.pending", ("absent",), "pending"),
    ("delivery", "delivery.deliver", ("pending",), "delivered"),
    ("recovery/watch", "recovery.claim", ("eligible",), "claimed"),
    ("recovery/watch", "recovery.defer", ("claimed",), "eligible"),
    (
        "recovery/watch",
        "recovery.acknowledge",
        ("claimed",),
        "acknowledged",
    ),
    ("authoring-publication", "authoring.plan", ("absent",), "planned"),
    (
        "authoring-publication",
        "authoring.replace",
        ("planned",),
        "replaced",
    ),
    (
        "authoring-publication",
        "authoring.directory-durable",
        ("replaced",),
        "directory-durable",
    ),
)


def _entity_lifecycle_row(binding: str, transition_id: str) -> Mapping[str, object]:
    return {
        "binding_kind": "entity",
        "binding": binding,
        "target": binding,
        "discriminant": {"kind": "none"},
        "transition_id": transition_id,
    }


_PRODUCTION_LIFECYCLE_ROWS = (
    {
        "binding_kind": "callsite",
        "binding": "src/lockstep/runtime/providers/_codex_supervisor.py::_CodexSupervisorTransaction.execute::call:0026",
        "target": "src/lockstep/runtime/providers/_codex_supervisor.py::_publish_terminal",
        "discriminant": {
            "kind": "literal-arguments",
            "positional": [],
            "keywords": [
                {"name": "quiescent", "type": "bool", "value": True},
            ],
        },
        "transition_id": "process.terminal",
    },
    {
        "binding_kind": "callsite",
        "binding": "src/lockstep/runtime/publication.py::ProjectPublisher.prepare::call:0023",
        "target": "src/lockstep/runtime/publication.py::ProjectPublisher._write_atomic",
        "discriminant": {
            "kind": "literal-arguments",
            "positional": [],
            "keywords": [
                {"name": "mutable", "type": "bool", "value": True},
            ],
        },
        "transition_id": "publication.prepare",
    },
    _entity_lifecycle_row(
        "src/lockstep/authoring_compilation.py::_authoring_plan",
        "authoring.plan",
    ),
    _entity_lifecycle_row(
        "src/lockstep/authoring_publisher.py::_publish_owned_temporary",
        "authoring.replace",
    ),
    _entity_lifecycle_row(
        "src/lockstep/authoring_publisher.py::_publish_target",
        "authoring.directory-durable",
    ),
    _entity_lifecycle_row(
        "src/lockstep/runtime/_graph_runtime_guard.py::_GraphRuntimeGuard.commitment_guard",
        "commitment.hold",
    ),
    _entity_lifecycle_row(
        "src/lockstep/runtime/_graph_runtime_guard.py::_GraphRuntimeGuard.resume",
        "commitment.commit",
    ),
    _entity_lifecycle_row(
        "src/lockstep/runtime/effects/ledger.py::EffectLedger.mark_launching",
        "process.launch",
    ),
    _entity_lifecycle_row(
        "src/lockstep/runtime/effects/ledger.py::EffectLedger.mark_running",
        "process.running",
    ),
    _entity_lifecycle_row(
        "src/lockstep/runtime/effects/owner_consent.py::OwnerConsentAuthority.issue",
        "consent.issue",
    ),
    _entity_lifecycle_row(
        "src/lockstep/runtime/providers/_codex_attempt.py::_CodexAttemptDriver._commit_ready_supervisor",
        "process.running",
    ),
    _entity_lifecycle_row(
        "src/lockstep/runtime/providers/_codex_preparation.py::_CodexPreparation._commit_prepared_launch",
        "process.prepare",
    ),
)


def _lifecycle_table(
    rows: tuple[Mapping[str, object], ...] = (),
) -> Mapping[str, object]:
    artifact = json.loads(
        (ARCHITECTURE_TEST_ROOT / "architecture_lifecycle.json").read_bytes()
    )
    return {**artifact, "rows": [dict(row) for row in rows]}


def test_domain_lifecycle_rule_table_is_checked_in_canonical_json() -> None:
    path = ARCHITECTURE_TEST_ROOT / "architecture_lifecycle.json"
    raw = path.read_bytes()
    parsed = json.loads(raw)
    canonical = json.dumps(
        parsed,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert raw == canonical
    assert not raw.endswith(b"\n")
    assert set(parsed) == {"schema", "transitions", "rows"}
    assert parsed["schema"] == "lockstep.architecture-lifecycle/v1"
    assert parsed["rows"] == list(_PRODUCTION_LIFECYCLE_ROWS)
    assert parsed["transitions"] == [
        {
            "cluster": cluster,
            "transition_id": transition_id,
            "from": list(from_states),
            "to": to_state,
        }
        for cluster, transition_id, from_states, to_state in _LIFECYCLE_TRANSITIONS
    ]


def test_domain_lifecycle_uses_only_public_call_resolver_boundary() -> None:
    source = (
        ARCHITECTURE_TEST_ROOT / "architecture_domain_lifecycle.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    private_attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.startswith("_")
    }
    assert not ({"_Model", "_read_primitives"} & private_attributes)


def _semantic_digest_inputs():
    return domain_lifecycle.SemanticDigestInputs(
        allowlist_digest="a" * 64,
        schema_digest="b" * 64,
        threshold_digest="c" * 64,
        analyzer_version="task-12c-test",
        rule_version="v1",
    )


def _propagate_fixture(
    tmp_path: Path,
    source: str,
    *,
    path: str = "src/lockstep/semantic_fixture.py",
    primitive_rows: tuple[Mapping[str, object], ...] = (),
    lifecycle_rows: tuple[Mapping[str, object], ...] = (),
    extra_files: Mapping[str, str] | None = None,
    allowlist: tuple[str, ...] = (),
):
    files = {path: _resolver_source(source)}
    files.update(
        {
            extra_path: _resolver_source(extra_source)
            for extra_path, extra_source in (extra_files or {}).items()
        }
    )
    index = _fixture_index(files, tmp_path)
    primitives = _primitive_table(index, primitive_rows)
    resolutions = resolve_calls(index, allowlist, primitives)
    semantics = propagate_semantics(
        index,
        resolutions,
        primitives,
        _lifecycle_table(lifecycle_rows),
        digest_inputs=_semantic_digest_inputs(),
    )
    return index, resolutions, semantics


def test_domain_lifecycle_records_are_exact_frozen_slotted_and_deeply_immutable() -> None:
    exact_fields = {
        "SemanticDigestInputs": (
            "allowlist_digest",
            "schema_digest",
            "threshold_digest",
            "analyzer_version",
            "rule_version",
        ),
        "EntitySemantics": (
            "identity",
            "direct_domains",
            "propagated_domains",
            "direct_transitions",
            "propagated_transitions",
            "propagated_lifecycle_clusters",
            "semantic_dependency_sha256",
        ),
        "FileSemantics": (
            "identity",
            "propagated_domains",
            "propagated_transitions",
            "propagated_lifecycle_clusters",
            "semantic_dependency_sha256",
        ),
        "OneHopSemantics": (
            "identity",
            "root",
            "members",
            "propagated_domains",
            "propagated_transitions",
            "propagated_lifecycle_clusters",
            "semantic_dependency_sha256",
        ),
        "SemanticIndex": (
            "entities",
            "files",
            "primitive_digest",
            "lifecycle_digest",
            "digest_inputs",
        ),
    }

    for name, expected in exact_fields.items():
        record_type = getattr(domain_lifecycle, name)
        assert tuple(field.name for field in fields(record_type)) == expected
        assert record_type.__slots__ == expected
        assert record_type.__dataclass_params__.frozen


def test_domain_lifecycle_semantics_propagate_ordered_sets_through_scc(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/scc_semantics.py"
    leaf = f"{path}::leaf"
    secondary = f"{path}::secondary"
    lifecycle_rows = ({
        "binding_kind": "entity",
        "binding": leaf,
        "target": leaf,
        "discriminant": {"kind": "none"},
        "transition_id": "artifact.materialize",
    }, {
        "binding_kind": "entity",
        "binding": secondary,
        "target": secondary,
        "discriminant": {"kind": "none"},
        "transition_id": "process.prepare",
    })
    _index, _resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        def leaf():
            pass
        def secondary():
            pass
        def recursive_a():
            recursive_b()
        def recursive_b():
            recursive_a()
            leaf()
            secondary()
        def caller():
            recursive_a()
        """,
        path=path,
        primitive_rows=(
            _primitive_entity_row(leaf, ("filesystem-read",)),
            _primitive_entity_row(secondary, ("authority/commitment",)),
        ),
        lifecycle_rows=lifecycle_rows,
    )

    assert tuple(semantics.entities) == (
        leaf,
        secondary,
        f"{path}::recursive_a",
        f"{path}::recursive_b",
        f"{path}::caller",
    )
    assert semantics.entities[leaf].direct_domains == ("filesystem-read",)
    assert semantics.entities[leaf].direct_transitions == ("artifact.materialize",)
    assert semantics.entities[secondary].direct_domains == ("authority/commitment",)
    assert semantics.entities[secondary].direct_transitions == ("process.prepare",)
    for identity in (
        f"{path}::recursive_a",
        f"{path}::recursive_b",
        f"{path}::caller",
    ):
        assert semantics.entities[identity].propagated_domains == (
            "filesystem-read",
            "authority/commitment",
        )
        assert semantics.entities[identity].propagated_transitions == (
            "process.prepare",
            "artifact.materialize",
        )
        assert semantics.entities[identity].propagated_lifecycle_clusters == (
            "process-execution",
            "artifact/acceptance",
        )
    file_semantics = semantics.files[f"{path}::@file"]
    assert type(semantics.entities) is MappingProxyType
    assert type(semantics.files) is MappingProxyType
    assert file_semantics.propagated_domains == (
        "filesystem-read",
        "authority/commitment",
    )
    assert file_semantics.propagated_transitions == (
        "process.prepare",
        "artifact.materialize",
    )
    assert file_semantics.propagated_lifecycle_clusters == (
        "process-execution",
        "artifact/acceptance",
    )
    _assert_deeply_immutable(semantics)


def test_domain_lifecycle_places_external_callsite_rows_without_external_vertex(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/callsite_semantics.py"
    owner = f"{path}::owner"
    callsite = f"{owner}::call:0001"
    primitive = _primitive_callsite_row(callsite, "reviewed.callback")
    lifecycle_row = {
        "binding_kind": "callsite",
        "binding": callsite,
        "target": "reviewed.callback",
        "discriminant": {
            "kind": "literal-arguments",
            "positional": [{"index": 1, "type": "bool", "value": True}],
            "keywords": [{"name": "mode", "type": "str", "value": "safe"}],
        },
        "transition_id": "process.prepare",
    }
    _index, _resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        def owner(callback):
            callback(ignored, True, mode="safe")
        """,
        path=path,
        primitive_rows=(primitive,),
        lifecycle_rows=(lifecycle_row,),
    )

    assert tuple(semantics.entities) == (owner,)
    assert semantics.entities[owner].direct_domains == (
        "external-process/provider",
    )
    assert semantics.entities[owner].direct_transitions == ("process.prepare",)
    assert "reviewed.callback" not in semantics.entities


def test_domain_lifecycle_does_not_turn_dependency_evidence_into_scc_edges(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/dependency_not_edge.py"
    effect = f"{path}::effect"
    base = f"{path}::Base"
    metaclass = f"{path}::Meta"
    _index, _resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        def effect(value):
            return value
        @effect
        def decorated():
            pass
        class Base:
            pass
        class Meta:
            pass
        class Child(Base, metaclass=Meta):
            pass
        """,
        path=path,
        primitive_rows=(
            _primitive_entity_row(base, ("filesystem-read",)),
            _primitive_entity_row(metaclass, ("synchronization",)),
            _primitive_entity_row(effect, ("authority/commitment",)),
        ),
    )

    assert semantics.entities[effect].propagated_domains == (
        "authority/commitment",
    )
    assert semantics.entities[f"{path}::decorated"].propagated_domains == ()
    assert semantics.entities[f"{path}::Child"].propagated_domains == ()


def test_domain_lifecycle_places_external_entity_rows_on_invocation_owners(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/external_entity_semantics.py"
    lifecycle_row = {
        "binding_kind": "entity",
        "binding": "os.getcwd",
        "target": "os.getcwd",
        "discriminant": {"kind": "none"},
        "transition_id": "process.prepare",
    }
    _index, _resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        import os
        def first():
            os.getcwd()
        def second():
            os.getcwd()
        def caller():
            first()
        """,
        path=path,
        primitive_rows=(
            _primitive_entity_row("os.getcwd", ("filesystem-read",)),
        ),
        lifecycle_rows=(lifecycle_row,),
    )

    assert set(semantics.entities) == {
        f"{path}::first",
        f"{path}::second",
        f"{path}::caller",
    }
    for identity in (f"{path}::first", f"{path}::second"):
        assert semantics.entities[identity].direct_domains == ("filesystem-read",)
        assert semantics.entities[identity].direct_transitions == ("process.prepare",)
    assert semantics.entities[f"{path}::caller"].direct_domains == ()
    assert semantics.entities[f"{path}::caller"].propagated_domains == (
        "filesystem-read",
    )
    assert "os.getcwd" not in semantics.entities


def test_domain_lifecycle_file_owner_retains_direct_external_semantics(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/file_owner_semantics.py"
    _index, _resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        import os
        os.getcwd()
        """,
        path=path,
        primitive_rows=(
            _primitive_entity_row("os.getcwd", ("filesystem-read",)),
        ),
        lifecycle_rows=({
            "binding_kind": "entity",
            "binding": "os.getcwd",
            "target": "os.getcwd",
            "discriminant": {"kind": "none"},
            "transition_id": "process.prepare",
        },),
    )

    assert semantics.entities == {}
    file_semantics = semantics.files[f"{path}::@file"]
    assert file_semantics.propagated_domains == ("filesystem-read",)
    assert file_semantics.propagated_transitions == ("process.prepare",)
    assert file_semantics.propagated_lifecycle_clusters == (
        "process-execution",
    )


def test_domain_lifecycle_rejects_unresolved_and_source_population_mismatch(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/semantic_preblocks.py"
    files = {path: _resolver_source("def owner():\n    unknown()\n")}
    index = _fixture_index(files, tmp_path)
    primitives = _primitive_table(index, ())
    unresolved = resolve_calls(index, (), primitives)

    with pytest.raises(ValueError, match="unresolved call"):
        propagate_semantics(
            index,
            unresolved,
            primitives,
            _lifecycle_table(),
            digest_inputs=_semantic_digest_inputs(),
        )

    dependency_files = {
        path: _resolver_source("@unknown\ndef owner():\n    pass\n")
    }
    dependency_index = _fixture_index(dependency_files, tmp_path)
    dependency_primitives = _primitive_table(dependency_index, ())
    unresolved_dependency = resolve_calls(
        dependency_index, (), dependency_primitives
    )
    with pytest.raises(ValueError, match="unresolved dependency"):
        propagate_semantics(
            dependency_index,
            unresolved_dependency,
            dependency_primitives,
            _lifecycle_table(),
            digest_inputs=_semantic_digest_inputs(),
        )

    call_files = {
        path: _resolver_source("def owner(callback):\n    callback()\n")
    }
    call_index = _fixture_index(call_files, tmp_path)
    callsite = f"{path}::owner::call:0001"
    call_primitives = _primitive_table(
        call_index,
        (_primitive_callsite_row(callsite, "reviewed.callback"),),
    )
    resolved = resolve_calls(call_index, (), call_primitives)
    stale = replace(resolved, reference_source_sha256="0" * 64)
    with pytest.raises(ValueError, match="source population"):
        propagate_semantics(
            call_index,
            stale,
            call_primitives,
            _lifecycle_table(),
            digest_inputs=_semantic_digest_inputs(),
        )
    stale_primitive_population = {
        **call_primitives,
        "reference_source_sha256": "0" * 64,
    }
    with pytest.raises(ValueError, match="source population|reference source"):
        propagate_semantics(
            call_index,
            resolved,
            stale_primitive_population,
            _lifecycle_table(),
            digest_inputs=_semantic_digest_inputs(),
        )
    stale_attestation = dict(call_primitives["callsite_evidence"][0])
    stale_attestation["call_ast_sha256"] = "0" * 64
    stale_primitive_attestation = {
        **call_primitives,
        "callsite_evidence": [stale_attestation],
    }
    with pytest.raises(ValueError, match="callsite|primitive|AST"):
        propagate_semantics(
            call_index,
            resolved,
            stale_primitive_attestation,
            _lifecycle_table(),
            digest_inputs=_semantic_digest_inputs(),
        )


def test_domain_lifecycle_one_hop_uses_only_exact_validated_members(tmp_path: Path) -> None:
    path = "src/lockstep/one_hop_semantics.py"
    leaf = f"{path}::leaf"
    root = f"{path}::root"
    foreign_path = "src/lockstep/foreign_semantics.py"
    _index, _resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        def leaf():
            pass
        def root():
            pass
        def unrelated():
            pass
        """,
        path=path,
        primitive_rows=(_primitive_entity_row(leaf, ("filesystem-read",)),),
        lifecycle_rows=({
            "binding_kind": "entity",
            "binding": leaf,
            "target": leaf,
            "discriminant": {"kind": "none"},
            "transition_id": "process.prepare",
        },),
        extra_files={foreign_path: "def foreign():\n    pass\n"},
    )

    aggregate = semantics.build_one_hop(root=root, members=(root, leaf))
    assert aggregate.identity == root + "::@one_hop"
    assert aggregate.root == root
    assert aggregate.members == (root, leaf)
    assert aggregate.propagated_domains == ("filesystem-read",)
    assert aggregate.propagated_transitions == ("process.prepare",)
    assert aggregate.propagated_lifecycle_clusters == ("process-execution",)
    inputs = semantics.digest_inputs
    aggregate_payload = {
        "schema": "lockstep.architecture-one-hop-semantics/v1",
        "identity": root + "::@one_hop",
        "root": root,
        "members": [
            {
                "identity": member,
                "semantic_dependency_sha256": (
                    semantics.entities[member].semantic_dependency_sha256
                ),
            }
            for member in (root, leaf)
        ],
        "propagated_domains": ["filesystem-read"],
        "propagated_transitions": ["process.prepare"],
        "propagated_lifecycle_clusters": ["process-execution"],
        "rule_inputs": {
            "allowlist_digest": inputs.allowlist_digest,
            "primitive_digest": semantics.primitive_digest,
            "lifecycle_digest": semantics.lifecycle_digest,
            "schema_digest": inputs.schema_digest,
            "threshold_digest": inputs.threshold_digest,
            "analyzer_version": inputs.analyzer_version,
            "rule_version": inputs.rule_version,
        },
    }
    assert aggregate.semantic_dependency_sha256 == _canonical_sha256(
        aggregate_payload
    )

    root_only = semantics.build_one_hop(root=root, members=(root,))
    assert root_only.members == (root,)
    assert root_only.propagated_domains == ()

    invalid_members = (
        (),
        (leaf,),
        (leaf, root),
        (root, root),
        (root, "src/lockstep/missing.py::unknown"),
        (root, f"{foreign_path}::foreign"),
        (root, f"{path}::unrelated", leaf),
    )
    for members in invalid_members:
        with pytest.raises(ValueError):
            semantics.build_one_hop(root=root, members=members)


def test_domain_lifecycle_rejects_malformed_rule_objects_before_partial_result(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/strict_semantic_rules.py"
    files = {
        path: _resolver_source(
            "def owner():\n    pass\n\ndef other():\n    pass\n"
        )
    }
    index = _fixture_index(files, tmp_path)
    primitives = _primitive_table(index, ())
    resolutions = resolve_calls(index, (), primitives)
    lifecycle = _lifecycle_table()

    primitive_variants = (
        {**primitives, "extra": True},
        {key: value for key, value in primitives.items() if key != "schema_version"},
        {**primitives, "schema_version": 2},
        {**primitives, "rows": ()},
        {
            **primitives,
            "callsite_evidence": [{
                "selector": f"{path}::owner::call:0001",
                "owner_source_sha256": "0" * 64,
                "call_ast_sha256": "0" * 64,
            }],
        },
        {
            **primitives,
            "rows": [{
                **_primitive_entity_row(f"{path}::owner"),
                "extra": True,
            }],
        },
        {
            **primitives,
            "rows": [{
                **_primitive_entity_row(f"{path}::owner"),
                "domains": ["authority/commitment", "filesystem-read"],
            }],
        },
    )
    for malformed_primitives in primitive_variants:
        with pytest.raises(ValueError, match="primitive|effect|callsite"):
            propagate_semantics(
                index,
                resolutions,
                malformed_primitives,
                lifecycle,
                digest_inputs=_semantic_digest_inputs(),
            )

    altered_transition = dict(lifecycle["transitions"][0])
    altered_transition["to"] = "wrong"
    extra_transition_key = dict(lifecycle["transitions"][0])
    extra_transition_key["extra"] = True
    missing_transition_key = dict(lifecycle["transitions"][0])
    missing_transition_key.pop("from")
    lifecycle_variants = (
        {**lifecycle, "extra": True},
        {key: value for key, value in lifecycle.items() if key != "rows"},
        {**lifecycle, "schema": "wrong"},
        {**lifecycle, "transitions": lifecycle["transitions"][:-1]},
        {**lifecycle, "transitions": list(reversed(lifecycle["transitions"]))},
        {
            **lifecycle,
            "transitions": [altered_transition, *lifecycle["transitions"][1:]],
        },
        {
            **lifecycle,
            "transitions": [extra_transition_key, *lifecycle["transitions"][1:]],
        },
        {
            **lifecycle,
            "transitions": [missing_transition_key, *lifecycle["transitions"][1:]],
        },
        {**lifecycle, "rows": ()},
    )
    for malformed_lifecycle in lifecycle_variants:
        with pytest.raises(ValueError, match="lifecycle|transition"):
            propagate_semantics(
                index,
                resolutions,
                primitives,
                malformed_lifecycle,
                digest_inputs=_semantic_digest_inputs(),
            )

    owner = f"{path}::owner"
    other = f"{path}::other"
    ordered_rows = tuple(
        {
            "binding_kind": "entity",
            "binding": binding,
            "target": binding,
            "discriminant": {"kind": "none"},
            "transition_id": "process.prepare",
        }
        for binding in sorted((owner, other))
    )
    propagate_semantics(
        index,
        resolutions,
        primitives,
        _lifecycle_table(ordered_rows),
        digest_inputs=_semantic_digest_inputs(),
    )
    for malformed_rows in (
        tuple(reversed(ordered_rows)),
        (ordered_rows[0], ordered_rows[0]),
        ({**ordered_rows[0], "binding": f"{path}::missing", "target": f"{path}::missing"},),
        ({**ordered_rows[0], "target": owner},),
    ):
        with pytest.raises(ValueError, match="lifecycle|order|duplicate|orphan|target"):
            propagate_semantics(
                index,
                resolutions,
                primitives,
                _lifecycle_table(malformed_rows),
                digest_inputs=_semantic_digest_inputs(),
            )

    invalid_digest_inputs = (
        {"allowlist_digest": "not-a-digest"},
        {"schema_digest": "B" * 64},
        {"threshold_digest": "0" * 63},
        {"analyzer_version": ""},
        {"rule_version": ""},
    )
    for changes in invalid_digest_inputs:
        values = {
            "allowlist_digest": "a" * 64,
            "schema_digest": "b" * 64,
            "threshold_digest": "c" * 64,
            "analyzer_version": "task-12c-test",
            "rule_version": "v1",
            **changes,
        }
        with pytest.raises(ValueError, match="digest|version"):
            domain_lifecycle.SemanticDigestInputs(**values)


def test_domain_lifecycle_literal_rows_fail_closed_on_every_binding_drift(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/lifecycle_literal_drift.py"
    owner = f"{path}::owner"
    callsite = f"{owner}::call:0001"
    source = _resolver_source(
        """
        def owner(callback, dynamic):
            callback(dynamic, 7, alpha="a", omega=True)
            callback(8)
        """
    )
    index = _fixture_index({path: source}, tmp_path)
    second_callsite = f"{owner}::call:0002"
    primitives = _primitive_table(
        index,
        (
            _primitive_callsite_row(callsite, "reviewed.callback"),
            _primitive_callsite_row(second_callsite, "reviewed.callback"),
        ),
    )
    resolutions = resolve_calls(index, (), primitives)
    valid = {
        "binding_kind": "callsite",
        "binding": callsite,
        "target": "reviewed.callback",
        "discriminant": {
            "kind": "literal-arguments",
            "positional": [{"index": 1, "type": "int", "value": 7}],
            "keywords": [
                {"name": "alpha", "type": "str", "value": "a"},
                {"name": "omega", "type": "bool", "value": True},
            ],
        },
        "transition_id": "process.prepare",
    }
    valid_lifecycle = _lifecycle_table((valid,))
    propagate_semantics(
        index,
        resolutions,
        primitives,
        valid_lifecycle,
        digest_inputs=_semantic_digest_inputs(),
    )

    evidence = primitives["callsite_evidence"]
    rows = primitives["rows"]
    missing_evidence_key = dict(evidence[0])
    missing_evidence_key.pop("call_ast_sha256")
    primitive_variants = (
        {**primitives, "callsite_evidence": evidence[:-1]},
        {**primitives, "callsite_evidence": [*evidence, evidence[0]]},
        {**primitives, "callsite_evidence": list(reversed(evidence))},
        {**primitives, "callsite_evidence": [missing_evidence_key, evidence[1]]},
        {**primitives, "rows": list(reversed(rows))},
        {**primitives, "rows": [*rows, rows[0]]},
        {**primitives, "rows": [{**rows[0], "selector": f"{owner}::call:9999"}, rows[1]]},
        {**primitives, "rows": [{**rows[0], "selector_kind": "unknown"}, rows[1]]},
        {**primitives, "rows": [{**rows[0], "semantic_target": "reviewed.other"}, rows[1]]},
        {**primitives, "rows": [{**rows[0], "domains": []}, rows[1]]},
        {**primitives, "rows": [{**rows[0], "domains": ["filesystem-read", "filesystem-read"]}, rows[1]]},
        {**primitives, "rows": [{**rows[0], "domains": ["unknown-domain"]}, rows[1]]},
        {**primitives, "rows": [{**rows[0], "domains": ["authority/commitment", "filesystem-read"]}, rows[1]]},
    )
    for malformed_primitives in primitive_variants:
        with pytest.raises(ValueError, match="primitive|effect|callsite|domain|order"):
            propagate_semantics(
                index,
                resolutions,
                malformed_primitives,
                valid_lifecycle,
                digest_inputs=_semantic_digest_inputs(),
            )

    drifted_discriminants = (
        {**valid["discriminant"], "positional": [] , "keywords": []},
        {**valid["discriminant"], "positional": [{"index": 0, "type": "int", "value": 7}]},
        {**valid["discriminant"], "positional": [{"index": 1, "type": "int", "value": 8}]},
        {**valid["discriminant"], "positional": [{"index": 1, "type": "bool", "value": True}]},
        {
            **valid["discriminant"],
            "positional": [
                {"index": 2, "type": "int", "value": 8},
                {"index": 1, "type": "int", "value": 7},
            ],
        },
        {
            **valid["discriminant"],
            "positional": [
                {"index": 1, "type": "int", "value": 7},
                {"index": 1, "type": "int", "value": 7},
            ],
        },
        {
            **valid["discriminant"],
            "keywords": list(reversed(valid["discriminant"]["keywords"])),
        },
        {
            **valid["discriminant"],
            "keywords": [
                valid["discriminant"]["keywords"][0],
                valid["discriminant"]["keywords"][0],
            ],
        },
        {
            **valid["discriminant"],
            "positional": [{"index": 1, "type": "null", "value": False}],
        },
        {
            key: value
            for key, value in valid["discriminant"].items()
            if key != "keywords"
        },
        {**valid["discriminant"], "spread": True},
    )
    invalid_rows = [
        {**valid, "discriminant": discriminant}
        for discriminant in drifted_discriminants
    ]
    invalid_rows.extend(
        (
            {**valid, "target": "reviewed.other"},
            {**valid, "binding": f"{owner}::call:9999"},
            {**valid, "binding_kind": "unknown"},
            {**valid, "transition_id": "unknown.transition"},
            {key: value for key, value in valid.items() if key != "target"},
        )
    )
    for row in invalid_rows:
        with pytest.raises(ValueError, match="lifecycle|literal|binding|target"):
            propagate_semantics(
                index,
                resolutions,
                primitives,
                _lifecycle_table((row,)),
                digest_inputs=_semantic_digest_inputs(),
            )
    with pytest.raises(ValueError, match="duplicate|ambiguous"):
        propagate_semantics(
            index,
            resolutions,
            primitives,
            _lifecycle_table((valid, valid)),
            digest_inputs=_semantic_digest_inputs(),
        )


def test_domain_lifecycle_semantic_digest_changes_with_source_and_rule_inputs(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/semantic_digest_fixture.py"
    source = "def owner():\n    return 1\n"
    _index, _resolutions, baseline = _propagate_fixture(
        tmp_path, source, path=path
    )
    identity = f"{path}::owner"

    changed_files = {path: _resolver_source("def owner():\n    return 2\n")}
    changed_index = _fixture_index(changed_files, tmp_path)
    changed_primitives = _primitive_table(changed_index, ())
    changed_resolutions = resolve_calls(changed_index, (), changed_primitives)
    changed_source = propagate_semantics(
        changed_index,
        changed_resolutions,
        changed_primitives,
        _lifecycle_table(),
        digest_inputs=_semantic_digest_inputs(),
    )
    changed_rules = propagate_semantics(
        _index,
        _resolutions,
        _primitive_table(_index, ()),
        _lifecycle_table(),
        digest_inputs=replace(_semantic_digest_inputs(), rule_version="v2"),
    )
    rule_variants = (
        {"allowlist_digest": "d" * 64},
        {"schema_digest": "e" * 64},
        {"threshold_digest": "f" * 64},
        {"analyzer_version": "task-12c-test-v2"},
    )
    variant_digests = []
    for changes in rule_variants:
        variant = propagate_semantics(
            _index,
            _resolutions,
            _primitive_table(_index, ()),
            _lifecycle_table(),
            digest_inputs=replace(_semantic_digest_inputs(), **changes),
        )
        variant_digests.append(
            variant.entities[identity].semantic_dependency_sha256
        )
    changed_primitive = _propagate_fixture(
        tmp_path,
        source,
        path=path,
        primitive_rows=(
            _primitive_entity_row(identity, ("filesystem-read",)),
        ),
    )[2]
    changed_lifecycle = _propagate_fixture(
        tmp_path,
        source,
        path=path,
        lifecycle_rows=({
            "binding_kind": "entity",
            "binding": identity,
            "target": identity,
            "discriminant": {"kind": "none"},
            "transition_id": "process.prepare",
        },),
    )[2]

    digests = {
        baseline.entities[identity].semantic_dependency_sha256,
        changed_source.entities[identity].semantic_dependency_sha256,
        changed_rules.entities[identity].semantic_dependency_sha256,
        changed_primitive.entities[identity].semantic_dependency_sha256,
        changed_lifecycle.entities[identity].semantic_dependency_sha256,
        *variant_digests,
    }
    assert len(digests) == 9


def test_domain_lifecycle_entity_file_and_one_hop_digests_use_closed_payloads(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/closed_semantic_payload.py"
    identity = f"{path}::owner"
    index, _resolutions, semantics = _propagate_fixture(
        tmp_path,
        "def owner():\n    return 1\n",
        path=path,
    )
    primitives = _primitive_table(index, ())
    lifecycle = _lifecycle_table()
    primitive_digest = _canonical_sha256(primitives)
    lifecycle_digest = _canonical_sha256(lifecycle)
    digest_inputs = _semantic_digest_inputs()
    rule_inputs = {
        "allowlist_digest": digest_inputs.allowlist_digest,
        "primitive_digest": primitive_digest,
        "lifecycle_digest": lifecycle_digest,
        "schema_digest": digest_inputs.schema_digest,
        "threshold_digest": digest_inputs.threshold_digest,
        "analyzer_version": digest_inputs.analyzer_version,
        "rule_version": digest_inputs.rule_version,
    }
    entity_payload = {
        "schema": "lockstep.architecture-entity-semantics/v1",
        "identity": identity,
        "source_sha256": index.entities[identity].span.sha256,
        "imports": [],
        "aliases": [],
        "receivers": [],
        "calls": [],
        "dependencies": [],
        "containment": [],
        "direct_domains": [],
        "propagated_domains": [],
        "direct_transitions": [],
        "propagated_transitions": [],
        "propagated_lifecycle_clusters": [],
        "rule_inputs": rule_inputs,
    }
    entity_digest = _canonical_sha256(entity_payload)
    assert semantics.entities[identity].semantic_dependency_sha256 == entity_digest
    assert semantics.primitive_digest == primitive_digest
    assert semantics.lifecycle_digest == lifecycle_digest

    file_payload = {
        "schema": "lockstep.architecture-file-semantics/v1",
        "identity": f"{path}::@file",
        "file_sha256": index.file_sha256[path],
        "definitions": [{
            "identity": identity,
            "semantic_dependency_sha256": entity_digest,
        }],
        "imports": [],
        "aliases": [],
        "receivers": [],
        "calls": [],
        "dependencies": [],
        "propagated_domains": [],
        "propagated_transitions": [],
        "propagated_lifecycle_clusters": [],
        "rule_inputs": rule_inputs,
    }
    assert semantics.files[f"{path}::@file"].semantic_dependency_sha256 == (
        _canonical_sha256(file_payload)
    )

    aggregate = semantics.build_one_hop(root=identity, members=(identity,))
    one_hop_payload = {
        "schema": "lockstep.architecture-one-hop-semantics/v1",
        "identity": identity + "::@one_hop",
        "root": identity,
        "members": [{
            "identity": identity,
            "semantic_dependency_sha256": entity_digest,
        }],
        "propagated_domains": [],
        "propagated_transitions": [],
        "propagated_lifecycle_clusters": [],
        "rule_inputs": rule_inputs,
    }
    assert aggregate.semantic_dependency_sha256 == _canonical_sha256(one_hop_payload)


_CANDIDATE_FIELD_ORDER = {
    "FunctionMetrics": (
        "cyclomatic", "cognitive", "max_nesting", "legacy_syntactic_fanout",
        "resolved_fanout", "direct_domains", "propagated_domains",
        "direct_transitions", "propagated_transitions",
        "propagated_lifecycle_clusters", "unresolved_callsites", "signals",
        "composite_score", "hard_triggers", "candidate",
    ),
    "OneHopMetrics": (
        "root", "members", "helper_count", "summed_cyclomatic",
        "summed_cognitive", "max_nesting", "legacy_syntactic_fanout_union",
        "resolved_fanout_union", "propagated_domains", "propagated_transitions",
        "propagated_lifecycle_clusters", "signals", "composite_score",
        "hard_triggers", "candidate",
    ),
    "ClassMetrics": (
        "method_count", "public_method_count", "mutable_fields",
        "mutable_field_count", "cohesion_components", "bases",
        "propagated_domains", "propagated_transitions",
        "propagated_lifecycle_clusters", "signals", "composite_score",
        "hard_triggers", "candidate",
    ),
    "FileMetrics": (
        "definition_count", "class_count", "subsystem_imports",
        "subsystem_import_count", "definition_dependency_components",
        "propagated_domains", "propagated_transitions",
        "propagated_lifecycle_clusters", "signals", "composite_score",
        "hard_triggers", "candidate",
    ),
}
_CANDIDATE_SIGNAL_ORDER = {
    "function": ("cyclomatic", "cognitive", "nesting",
                 "legacy_syntactic_fanout", "domain_mixing", "lifecycle_mixing"),
    "one_hop": ("summed_cyclomatic", "summed_cognitive", "nesting",
                "legacy_syntactic_fanout_union", "domain_mixing", "lifecycle_mixing"),
    "class": ("method_count", "public_method_count", "mutable_field_count",
              "cohesion_components", "domain_mixing", "lifecycle_mixing"),
    "file": ("definition_count", "class_count", "subsystem_import_count",
             "definition_dependency_components", "domain_mixing", "lifecycle_mixing"),
}
_CANDIDATE_HARD_TRIGGER_ORDER = {
    "function": ("cyclomatic_gt_15", "cognitive_gt_25", "nesting_gt_4",
                 "legacy_syntactic_fanout_gt_24"),
    "one_hop": ("helper_count_gt_12",),
    "class": ("method_count_gt_24", "mutable_field_count_gt_24"),
    "file": ("definition_count_gt_50",),
}
_CANDIDATE_RULE = {
    "function": "hard_triggers or (composite_score>=3 and (domain_mixing or lifecycle_mixing))",
    "one_hop": "hard_triggers or (composite_score>=3 and (domain_mixing or lifecycle_mixing))",
    "class": "hard_triggers or (composite_score>=3 and (domain_mixing or lifecycle_mixing or cohesion_components))",
    "file": "hard_triggers or (composite_score>=3 and (domain_mixing or lifecycle_mixing or definition_dependency_components))",
}


def test_candidate_policy_records_are_exact_frozen_slotted() -> None:
    for name, expected in _CANDIDATE_FIELD_ORDER.items():
        record_type = getattr(candidate_policy, name)
        assert tuple(field.name for field in fields(record_type)) == expected
        assert record_type.__slots__ == expected
        assert record_type.__dataclass_params__.frozen
    assert tuple(
        field.name for field in fields(candidate_policy.ArchitectureReport)
    ) == (
        "functions", "one_hops", "classes", "files", "unresolved_callsites",
        "allowlist_digest", "primitive_digest", "lifecycle_digest",
        "schema_digest", "threshold_digest", "analyzer_version", "rule_version",
    )
    report_type = candidate_policy.ArchitectureReport
    assert report_type.__slots__ == tuple(field.name for field in fields(report_type))
    assert report_type.__dataclass_params__.frozen


def test_candidate_policy_metric_map_freezes_values_and_binds_global_ast_rank() -> None:
    first = candidate_policy._metric_map({"x.py::f": 1}, {"x.py::f": 3})
    same = candidate_policy._metric_map({"x.py::f": 1}, {"x.py::f": 3})
    different_rank = candidate_policy._metric_map({"x.py::f": 1}, {"x.py::f": 4})
    assert first == same
    assert first != different_rank
    with pytest.raises((AttributeError, TypeError)):
        first._values = MappingProxyType({})
    with pytest.raises((AttributeError, TypeError)):
        first.ast_order = MappingProxyType({})
    with pytest.raises(TypeError):
        first._values["x.py::f"] = 2


def test_candidate_policy_checked_in_schema_and_thresholds_are_canonical() -> None:
    schema_path = ARCHITECTURE_TEST_ROOT / "architecture_metrics.schema.json"
    threshold_path = ARCHITECTURE_TEST_ROOT / "architecture_thresholds.json"
    for path in (schema_path, threshold_path):
        raw = path.read_bytes()
        parsed = json.loads(raw)
        assert raw == json.dumps(parsed, ensure_ascii=False, allow_nan=False,
                                 sort_keys=True, separators=(",", ":")).encode()
        assert not raw.endswith(b"\n")

    schema = json.loads(schema_path.read_bytes())
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == "lockstep.architecture-metrics/v1"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert tuple(schema["$defs"]) == ("class", "file", "function", "one_hop")
    assert all(definition["additionalProperties"] is False
               for definition in schema["$defs"].values())
    for kind, record_name in (("function", "FunctionMetrics"),
                              ("one_hop", "OneHopMetrics"),
                              ("class", "ClassMetrics"), ("file", "FileMetrics")):
        definition = schema["$defs"][kind]
        assert definition["type"] == "object"
        assert tuple(definition["required"]) == _CANDIDATE_FIELD_ORDER[record_name]
        assert set(definition["properties"]) == set(definition["required"])
        assert tuple(definition["x-lockstep-signal-order"]) == _CANDIDATE_SIGNAL_ORDER[kind]
        assert tuple(definition["x-lockstep-hard-trigger-order"]) == (
            _CANDIDATE_HARD_TRIGGER_ORDER[kind]
        )
        assert definition["x-lockstep-candidate-rule"] == _CANDIDATE_RULE[kind]
        signals = definition["properties"]["signals"]
        assert signals["type"] == "object"
        assert signals["additionalProperties"] is False
        assert tuple(signals["required"]) == _CANDIDATE_SIGNAL_ORDER[kind]
        assert set(signals["properties"]) == set(signals["required"])
        assert all(value == {"type": "boolean"}
                   for value in signals["properties"].values())
        assert definition["properties"]["composite_score"] == {
            "maximum": 6, "minimum": 0, "type": "integer"
        }
        assert definition["properties"]["candidate"] == {"type": "boolean"}
        assert definition["properties"]["hard_triggers"]["items"]["enum"] == list(
            _CANDIDATE_HARD_TRIGGER_ORDER[kind]
        )
        assert definition["properties"]["hard_triggers"]["type"] == "array"
        assert definition["properties"]["hard_triggers"]["uniqueItems"] is True
        for field_name in ("direct_domains", "propagated_domains"):
            if field_name in definition["properties"]:
                value = definition["properties"][field_name]
                assert value["type"] == "array" and value["uniqueItems"] is True
                assert value["items"]["enum"] == list(_EFFECT_DOMAINS)
        for field_name in ("direct_transitions", "propagated_transitions"):
            if field_name in definition["properties"]:
                value = definition["properties"][field_name]
                assert value["type"] == "array" and value["uniqueItems"] is True
                assert value["items"]["enum"] == [
                    transition for _cluster, transition, _source, _target
                    in _LIFECYCLE_TRANSITIONS
                ]
        clusters = definition["properties"].get("propagated_lifecycle_clusters")
        if clusters is not None:
            assert clusters["type"] == "array" and clusters["uniqueItems"] is True
            assert clusters["items"]["enum"] == list(dict.fromkeys(
                cluster for cluster, _transition, _source, _target
                in _LIFECYCLE_TRANSITIONS
            ))
    integer_fields = {
        "function": {"cyclomatic", "cognitive", "max_nesting",
                     "legacy_syntactic_fanout", "resolved_fanout", "composite_score"},
        "one_hop": {"helper_count", "summed_cyclomatic", "summed_cognitive",
                    "max_nesting", "legacy_syntactic_fanout_union",
                    "resolved_fanout_union", "composite_score"},
        "class": {"method_count", "public_method_count", "mutable_field_count",
                  "cohesion_components", "composite_score"},
        "file": {"definition_count", "class_count", "subsystem_import_count",
                 "definition_dependency_components", "composite_score"},
    }
    for kind, names in integer_fields.items():
        for name in names - {"composite_score"}:
            assert schema["$defs"][kind]["properties"][name] == {
                "minimum": 0, "type": "integer"
            }
    array_contracts = {
        "function": ("unresolved_callsites",),
        "one_hop": ("members",), "class": ("bases", "mutable_fields"),
        "file": ("subsystem_imports",),
    }
    for kind, names in array_contracts.items():
        for name in names:
            value = schema["$defs"][kind]["properties"][name]
            assert value["type"] == "array" and value["uniqueItems"] is True
            assert value["items"]["type"] == "string"
    assert schema["$defs"]["one_hop"]["properties"]["root"]["type"] == "string"
    function_identity = {"format": "lockstep-function-identity", "type": "string"}
    callsite_identity = {"format": "lockstep-callsite-identity", "type": "string"}
    assert schema["$defs"]["one_hop"]["properties"]["root"] == function_identity
    assert schema["$defs"]["one_hop"]["properties"]["members"]["items"] == function_identity
    assert schema["$defs"]["class"]["properties"]["bases"]["items"] == function_identity
    assert schema["$defs"]["function"]["properties"]["unresolved_callsites"]["items"] == callsite_identity
    mutable_field = {"format": "lockstep-mutable-field", "type": "string"}
    subsystem = {"format": "python-identifier", "type": "string"}
    assert schema["$defs"]["class"]["properties"]["mutable_fields"]["items"] == mutable_field
    assert schema["$defs"]["file"]["properties"]["subsystem_imports"]["items"] == subsystem
    checker = FormatChecker()

    @checker.checks("python-identifier")
    def python_identifier(value: object) -> bool:
        return isinstance(value, str) and value.isidentifier()

    @checker.checks("lockstep-function-identity")
    def stable_function_identity(value: object) -> bool:
        if not isinstance(value, str) or value.count("::") != 1:
            return False
        path_text, qualified = value.split("::")
        path = PurePosixPath(path_text)
        return all((not path.is_absolute(), path.as_posix() == path_text,
                    path_text.startswith("src/lockstep/"), path_text.endswith(".py"),
                    all(part not in {"", ".", ".."} for part in path.parts),
                    "\\" not in path_text, all(part.isidentifier()
                    for part in qualified.split("."))))

    @checker.checks("lockstep-callsite-identity")
    def stable_callsite_identity(value: object) -> bool:
        if not isinstance(value, str) or "::call:" not in value:
            return False
        owner, ordinal = value.rsplit("::call:", 1)
        return (stable_function_identity(owner) and len(ordinal) == 4
                and ordinal.isascii() and ordinal.isdigit())

    @checker.checks("lockstep-mutable-field")
    def stable_mutable_field(value: object) -> bool:
        return (isinstance(value, str) and value.startswith("self.")
                and value[5:].isidentifier())

    identity_schema = schema["$defs"]["one_hop"]["properties"]["root"]
    for invalid in ("src/lockstep/../x.py::f", "src/lockstep//x.py::f",
                    "src/other/x.py::f", "src/lockstep/x.py::f:g",
                    "src/lockstep/x.py::not a name", "src/lockstep/x.py::f/g",
                    "src/lockstep/x.py::f..g"):
        assert Draft202012Validator(identity_schema, format_checker=checker).is_valid(invalid) is False
    for valid in ("src/lockstep/x-y.py::f", "src/lockstep/путь/модуль.py::функция",
                  "src/lockstep/x.py::℘", "src/lockstep/x.py::a·b"):
        assert Draft202012Validator(identity_schema, format_checker=checker).is_valid(valid) is True
    subsystem_schema = schema["$defs"]["file"]["properties"]["subsystem_imports"]["items"]
    for invalid in ("runtime.effects", "foo-bar", "", "1runtime"):
        assert Draft202012Validator(subsystem_schema, format_checker=checker).is_valid(invalid) is False
    for valid in ("исполнение", "℘", "a·b"):
        assert Draft202012Validator(subsystem_schema, format_checker=checker).is_valid(valid) is True
    callsite_schema = schema["$defs"]["function"]["properties"]["unresolved_callsites"]["items"]
    assert Draft202012Validator(callsite_schema, format_checker=checker).is_valid(
        "src/lockstep/x-y.py::функция::call:0001") is True
    for invalid in ("src/lockstep/x.py::f::call:001",
                    "src/lockstep/x.py::f::call:00001",
                    "src/lockstep/x.py::f::call:٠٠٠١",
                    "src/lockstep/x.py::not a name::call:0001"):
        assert Draft202012Validator(callsite_schema, format_checker=checker).is_valid(invalid) is False
    mutable_schema = schema["$defs"]["class"]["properties"]["mutable_fields"]["items"]
    for valid in ("self.items", "self.поле", "self.℘"):
        assert Draft202012Validator(mutable_schema, format_checker=checker).is_valid(valid) is True
    for invalid in ("cls.items", "self.not a name", "self.1field", "self.a.b"):
        assert Draft202012Validator(mutable_schema, format_checker=checker).is_valid(invalid) is False

    thresholds = json.loads(threshold_path.read_bytes())
    assert set(thresholds) == {"schema", "rule_version", "kinds"}
    assert thresholds["schema"] == "lockstep.architecture-thresholds/v1"
    assert thresholds["rule_version"] == "v1"
    assert set(thresholds["kinds"]) == {"function", "one_hop", "class", "file"}
    assert thresholds["kinds"] == {
        "function": {"signals": {"cyclomatic": 10, "cognitive": 15,
            "nesting": 4, "legacy_syntactic_fanout": 16,
            "domain_mixing": 2, "lifecycle_mixing": 2},
            "hard": {"cyclomatic_gt_15": 15, "cognitive_gt_25": 25,
                     "nesting_gt_4": 4, "legacy_syntactic_fanout_gt_24": 24},
            "minimum_signals": 3},
        "one_hop": {"signals": {"summed_cyclomatic": 24, "summed_cognitive": 40,
            "nesting": 4, "legacy_syntactic_fanout_union": 32,
            "domain_mixing": 3, "lifecycle_mixing": 2},
            "hard": {"helper_count_gt_12": 12}, "minimum_signals": 3},
        "class": {"signals": {"method_count": 15, "public_method_count": 8,
            "mutable_field_count": 8, "cohesion_components": 3,
            "domain_mixing": 3, "lifecycle_mixing": 2},
            "hard": {"method_count_gt_24": 24, "mutable_field_count_gt_24": 24},
            "minimum_signals": 3},
        "file": {"signals": {"definition_count": 25, "class_count": 6,
            "subsystem_import_count": 4, "definition_dependency_components": 4,
            "domain_mixing": 4, "lifecycle_mixing": 3},
            "hard": {"definition_count_gt_50": 50}, "minimum_signals": 3},
    }


def test_candidate_policy_binds_exact_rule_digests_and_versions(tmp_path: Path) -> None:
    path = "src/lockstep/digest_binding.py"
    index, resolutions, semantics = _propagate_fixture(tmp_path, "def root(): pass", path=path)
    schema_digest = hashlib.sha256(
        (ARCHITECTURE_TEST_ROOT / "architecture_metrics.schema.json").read_bytes()
    ).hexdigest()
    threshold_digest = hashlib.sha256(
        (ARCHITECTURE_TEST_ROOT / "architecture_thresholds.json").read_bytes()
    ).hexdigest()
    semantics = replace(semantics, digest_inputs=domain_lifecycle.SemanticDigestInputs(
        semantics.digest_inputs.allowlist_digest, schema_digest, threshold_digest,
        "task-12c-policy", "v1"))

    report = evaluate_candidates(index, measure_legacy_metrics(index), semantics, resolutions)

    assert report.allowlist_digest == semantics.digest_inputs.allowlist_digest
    assert report.primitive_digest == semantics.primitive_digest
    assert report.lifecycle_digest == semantics.lifecycle_digest
    assert report.schema_digest == schema_digest
    assert report.threshold_digest == threshold_digest
    assert (report.analyzer_version, report.rule_version) == ("task-12c-policy", "v1")


def _repository_architecture_report():
    tracked = subprocess.run(
        ("git", "ls-files", "src/lockstep"), cwd=ENGINE_ROOT,
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    paths = tuple(sorted(path for path in tracked if path.endswith(".py")))
    files = {path: (ENGINE_ROOT / path).read_bytes() for path in paths}
    index = build_source_index(ENGINE_ROOT, paths, files)
    rule_names = {
        "allowlist": "architecture_effect_free_allowlist.json",
        "primitives": "architecture_effect_primitives.json",
        "lifecycle": "architecture_lifecycle.json",
        "schema": "architecture_metrics.schema.json",
        "thresholds": "architecture_thresholds.json",
    }
    rules = {
        name: json.loads((ARCHITECTURE_TEST_ROOT / filename).read_bytes())
        for name, filename in rule_names.items()
    }
    resolutions = resolve_calls(index, rules["allowlist"], rules["primitives"])
    semantics = propagate_semantics(
        index, resolutions, rules["primitives"], rules["lifecycle"],
        digest_inputs=domain_lifecycle.SemanticDigestInputs(
            _canonical_sha256(rules["allowlist"]),
            _canonical_sha256(rules["schema"]),
            _canonical_sha256(rules["thresholds"]),
            "task-12c", "v1",
        ),
    )
    return evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions)


def test_candidate_policy_recomputes_function_formula_and_ignores_no_stored_claim(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/candidate_function.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        def leaf():
            pass
        def root():
            leaf()
        """,
        path=path,
        primitive_rows=(
            _primitive_entity_row(f"{path}::leaf", ("filesystem-read", "filesystem-write")),
        ),
        lifecycle_rows=({
            "binding_kind": "entity", "binding": f"{path}::leaf",
            "target": f"{path}::leaf", "discriminant": {"kind": "none"},
            "transition_id": "publication.apply",
        },),
    )
    legacy = dict(measure_legacy_metrics(index))
    base = legacy[f"{path}::root"]
    legacy[f"{path}::root"] = type(base)(10, 15, 4, 1)

    report = evaluate_candidates(index, MappingProxyType(legacy), semantics, resolutions)
    metric = report.functions[f"{path}::root"]

    _assert_deeply_immutable(report)
    with pytest.raises(TypeError):
        report.functions["replacement"] = metric
    assert tuple(metric.signals) == (
        "cyclomatic", "cognitive", "nesting", "legacy_syntactic_fanout",
        "domain_mixing", "lifecycle_mixing",
    )
    assert tuple(metric.signals.values()) == (True, True, True, False, True, False)
    assert metric.composite_score == 4
    assert metric.hard_triggers == ()
    assert metric.candidate is True
    assert metric.resolved_fanout == 1
    assert metric.unresolved_callsites == ()
    assert metric.direct_domains == ()
    assert metric.propagated_domains == ("filesystem-read", "filesystem-write")
    assert metric.direct_transitions == ()
    assert metric.propagated_transitions == ("publication.apply",)
    assert metric.propagated_lifecycle_clusters == ("publication",)


@pytest.mark.parametrize(
    ("values", "trigger"),
    (
        ((16, 0, 0, 0), "cyclomatic_gt_15"),
        ((0, 26, 0, 0), "cognitive_gt_25"),
        ((0, 0, 5, 0), "nesting_gt_4"),
        ((0, 0, 0, 25), "legacy_syntactic_fanout_gt_24"),
    ),
)
def test_candidate_policy_function_hard_triggers_are_exact(
    tmp_path: Path, values: tuple[int, int, int, int], trigger: str
) -> None:
    path = "src/lockstep/hard_trigger.py"
    index, resolutions, semantics = _propagate_fixture(tmp_path, "def root(): pass", path=path)
    identity = f"{path}::root"
    base = next(iter(measure_legacy_metrics(index).values()))
    legacy = MappingProxyType({identity: type(base)(*values)})

    metric = evaluate_candidates(index, legacy, semantics, resolutions).functions[identity]

    assert metric.hard_triggers == (trigger,)
    assert metric.candidate is True


def test_candidate_policy_function_boundaries_order_and_mixing_prerequisite(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/function_boundaries.py"
    index, resolutions, semantics = _propagate_fixture(tmp_path, "def root(): pass", path=path)
    identity = f"{path}::root"
    base = next(iter(measure_legacy_metrics(index).values()))
    legacy = MappingProxyType({identity: type(base)(15, 25, 4, 24)})

    metric = evaluate_candidates(index, legacy, semantics, resolutions).functions[identity]

    assert tuple(metric.signals.values()) == (True, True, True, True, False, False)
    assert metric.composite_score == 4
    assert metric.hard_triggers == ()
    assert metric.candidate is False

    hard = MappingProxyType({identity: type(base)(16, 26, 5, 25)})
    metric = evaluate_candidates(index, hard, semantics, resolutions).functions[identity]
    assert metric.hard_triggers == _CANDIDATE_HARD_TRIGGER_ORDER["function"]
    assert metric.candidate is True


def test_candidate_policy_exact_three_lifecycle_signal_and_below_three_boundary(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/lifecycle_formula.py"
    root = f"{path}::root"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path, "def first(): pass\ndef second(): pass\ndef root(): first(); second()",
        path=path,
        lifecycle_rows=(
            {"binding_kind": "entity", "binding": f"{path}::first",
             "target": f"{path}::first", "discriminant": {"kind": "none"},
             "transition_id": "process.prepare"},
            {"binding_kind": "entity", "binding": f"{path}::second",
             "target": f"{path}::second", "discriminant": {"kind": "none"},
             "transition_id": "publication.apply"},
        ),
    )
    legacy = dict(measure_legacy_metrics(index))
    metric_type = type(legacy[root])
    legacy[root] = metric_type(10, 15, 0, 0)
    metric = evaluate_candidates(
        index, MappingProxyType(legacy), semantics, resolutions).functions[root]
    assert metric.composite_score == 3
    assert metric.signals["lifecycle_mixing"] is True
    assert metric.candidate is True

    legacy[root] = metric_type(10, 0, 0, 0)
    below = evaluate_candidates(
        index, MappingProxyType(legacy), semantics, resolutions).functions[root]
    assert below.composite_score == 2
    assert below.signals["lifecycle_mixing"] is True
    assert below.candidate is False


def test_candidate_policy_one_hop_closure_is_private_scc_and_excludes_shared(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/helper_closure.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        def root(): _a()
        def _a(): _b()
        def _b(): _a(); _shared()
        def _shared(): pass
        def other(): _shared()
        """,
        path=path,
    )
    report = evaluate_candidates(index, measure_legacy_metrics(index), semantics, resolutions)
    metric = report.one_hops[f"{path}::root::@one_hop"]

    assert metric.members == (f"{path}::root", f"{path}::_a", f"{path}::_b")
    assert metric.helper_count == 2
    assert f"{path}::_shared" not in metric.members


def test_candidate_policy_one_hop_fixed_point_names_order_overlap_and_metrics(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/one_hop_policy.py"
    root = f"{path}::root"
    second = f"{path}::_second"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        def root(): _second(); __private(); __str__()
        def _second(): _a()
        def _a(): _b()
        def _b(): _leaf()
        def _leaf(): pass
        def __private(): pass
        def __str__(): pass
        """,
        path=path,
        primitive_rows=(
            _primitive_entity_row(f"{path}::__private", ("filesystem-read",)),
            _primitive_entity_row(f"{path}::_leaf", ("filesystem-write", "durable-state")),
        ),
        lifecycle_rows=({"binding_kind": "entity", "binding": f"{path}::__private",
            "target": f"{path}::__private", "discriminant": {"kind": "none"},
            "transition_id": "delivery.deliver"},
            {"binding_kind": "entity", "binding": f"{path}::_leaf",
            "target": f"{path}::_leaf", "discriminant": {"kind": "none"},
            "transition_id": "publication.apply"}),
    )
    legacy = dict(measure_legacy_metrics(index))
    metric_type = type(legacy[root])
    legacy[root] = metric_type(8, 15, 4, 10)
    legacy[f"{path}::__private"] = metric_type(8, 15, 1, 10)

    report = evaluate_candidates(index, MappingProxyType(legacy), semantics, resolutions)
    first = report.one_hops[root + "::@one_hop"]
    overlapping = report.one_hops[second + "::@one_hop"]

    assert first.members == (
        root, f"{path}::__private", f"{path}::_a", f"{path}::_b",
        f"{path}::_leaf", second)
    assert f"{path}::__str__" not in first.members
    assert overlapping.members == (second, f"{path}::_a", f"{path}::_b", f"{path}::_leaf")
    assert first.summed_cyclomatic == 20
    assert first.summed_cognitive == 30
    assert first.max_nesting == 4
    assert first.legacy_syntactic_fanout_union == 6
    assert first.resolved_fanout_union == 6
    assert first.propagated_domains == (
        "filesystem-read", "filesystem-write", "durable-state")
    assert first.propagated_transitions == ("publication.apply", "delivery.deliver")
    assert first.propagated_lifecycle_clusters == ("publication", "delivery")


def test_candidate_policy_one_hop_formula_and_helper_hard_boundary(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/helper_threshold.py"
    helpers = "\n".join(f"def _h{i}(): pass" for i in range(13))
    calls = "; ".join(f"_h{i}()" for i in range(13))
    index, resolutions, semantics = _propagate_fixture(
        tmp_path, f"def root(): {calls}\n{helpers}\n", path=path)
    report = evaluate_candidates(index, measure_legacy_metrics(index), semantics, resolutions)
    metric = report.one_hops[f"{path}::root::@one_hop"]

    assert metric.helper_count == 13
    assert metric.hard_triggers == ("helper_count_gt_12",)
    assert metric.candidate is True

    twelve_source = "def root(): " + "; ".join(
        f"_h{i}()" for i in range(12)) + "\n" + "\n".join(
        f"def _h{i}(): pass" for i in range(12))
    twelve_index, twelve_resolutions, twelve_semantics = _propagate_fixture(
        tmp_path, twelve_source, path="src/lockstep/helper_nontrigger.py")
    twelve = evaluate_candidates(
        twelve_index, measure_legacy_metrics(twelve_index), twelve_semantics,
        twelve_resolutions).one_hops[
            "src/lockstep/helper_nontrigger.py::root::@one_hop"]
    assert twelve.helper_count == 12
    assert twelve.hard_triggers == ()


def test_candidate_policy_one_hop_same_class_private_and_root_without_helpers(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/class_helpers.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        class Service:
            def root(self): self._helper(); self.__private(); self.__str__()
            def _helper(self): pass
            def __private(self): pass
            def __str__(self): pass
            def leaf(self): pass
        """, path=path)
    report = evaluate_candidates(index, measure_legacy_metrics(index), semantics, resolutions)
    root = f"{path}::Service.root"
    assert report.one_hops[root + "::@one_hop"].members == (
        root, f"{path}::Service.__private", f"{path}::Service._helper")
    leaf = f"{path}::Service.leaf"
    leaf_metric = report.one_hops[leaf + "::@one_hop"]
    assert leaf_metric.members == (leaf,)
    assert leaf_metric.helper_count == 0


def test_candidate_policy_one_hop_composite_formula_without_hard_trigger(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/helper_composite.py"
    root, helper = f"{path}::root", f"{path}::_helper"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path, "def root(): _helper()\ndef _helper(): pass", path=path,
        primitive_rows=(_primitive_entity_row(
            helper, ("filesystem-read", "filesystem-write", "durable-state")
        ),),
    )
    legacy = dict(measure_legacy_metrics(index))
    metric_type = type(legacy[root])
    legacy[root] = metric_type(12, 0, 4, 10)
    legacy[helper] = metric_type(12, 0, 1, 10)
    metric = evaluate_candidates(
        index, MappingProxyType(legacy), semantics, resolutions
    ).one_hops[root + "::@one_hop"]

    assert tuple(metric.signals.values()) == (True, False, True, False, True, False)
    assert metric.composite_score == 3
    assert metric.hard_triggers == ()
    assert metric.candidate is True


def test_candidate_policy_one_hop_excludes_helper_called_by_file_lambda(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/lambda_caller.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        "def root(): _helper()\ndef _helper(): pass\nexternal = lambda: _helper()",
        path=path,
    )
    root = f"{path}::root"
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).one_hops[root + "::@one_hop"]
    assert metric.members == (root,)


def test_candidate_policy_class_cohesion_fields_and_file_components(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/aggregate.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        import lockstep.runtime.effects
        import lockstep.workflow.lowering
        class Aggregate:
            def first(self): self.items = []
            def second(self): return self.items
            def isolated(self): self.flag = True
        def caller(): return Aggregate()
        """,
        path=path,
    )
    report = evaluate_candidates(index, measure_legacy_metrics(index), semantics, resolutions)
    class_metric = report.classes[f"{path}::Aggregate"]
    file_metric = report.files[f"{path}::@file"]

    assert class_metric.method_count == 3
    assert class_metric.public_method_count == 3
    assert class_metric.mutable_fields == ("self.flag", "self.items")
    assert class_metric.mutable_field_count == 2
    assert class_metric.cohesion_components == 2
    assert file_metric.definition_count == 5
    assert file_metric.class_count == 1
    assert file_metric.subsystem_imports == ("runtime", "workflow")
    assert file_metric.subsystem_import_count == 2
    assert file_metric.definition_dependency_components >= 1


def test_candidate_policy_class_store_forms_calls_lambdas_bases_and_formula(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/class_policy.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        class Base: pass
        class Aggregate(Base):
            projection = lambda self: self.items
            def initialize(self): self.items: list = []; self.count = 0
            def mutate(self): self.count += 1; self.items = self.items + [1]
            def remove(self): del self.count
            def delegate(self): return self.initialize()
            def isolated(self): self.flag = True
        """,
        path=path,
    )
    report = evaluate_candidates(index, measure_legacy_metrics(index), semantics, resolutions)
    metric = report.classes[f"{path}::Aggregate"]

    assert metric.method_count == 5
    assert metric.public_method_count == 5
    assert metric.mutable_fields == ("self.count", "self.flag", "self.items")
    assert metric.mutable_field_count == 3
    assert metric.cohesion_components == 2
    assert metric.bases == (f"{path}::Base",)


def test_candidate_policy_lambda_is_real_cohesion_vertex_and_inheritance_is_excluded(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/lambda_cohesion.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        def decorator(value): return value
        class Base:
            def inherited(self): self.base = True
        class Child(Base):
            projection = lambda self: self.items
            @decorator
            def direct(self): pass
        """, path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).classes[f"{path}::Child"]

    assert metric.method_count == 1
    assert metric.public_method_count == 1
    assert metric.mutable_fields == ()
    assert metric.cohesion_components == 2
    assert metric.bases == (f"{path}::Base",)


def test_candidate_policy_class_nested_definitions_do_not_donate_field_evidence(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/nested_field_evidence.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        class Aggregate:
            def outer(self):
                def nested(): self.items = []
                return nested
            def reader(self): return self.items
            def isolated(self): pass
        """, path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).classes[f"{path}::Aggregate"]
    assert metric.mutable_fields == ()
    assert metric.cohesion_components == 3


def test_candidate_policy_class_lambda_resolved_call_is_cohesion_edge(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/lambda_call_cohesion.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        class Aggregate:
            projection = lambda self: self.direct()
            def direct(self): pass
            def isolated(self): pass
        """, path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).classes[f"{path}::Aggregate"]
    assert metric.mutable_fields == ()
    assert metric.cohesion_components == 2


def test_candidate_policy_method_owned_lambda_contributes_field_evidence(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/method_lambda_field.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        class Aggregate:
            def outer(self): return lambda: self.items
            def reader(self): return self.items
        """, path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).classes[f"{path}::Aggregate"]
    assert metric.cohesion_components == 1


def test_candidate_policy_cls_fields_normalize_to_closed_mutable_field_identity(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/cls_fields.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        class Aggregate:
            @classmethod
            def write(cls): cls.items = []
            @classmethod
            def read(cls): return cls.items
        """, path=path, allowlist=("builtins.classmethod",))
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).classes[f"{path}::Aggregate"]
    assert metric.mutable_fields == ("self.items",)
    assert metric.cohesion_components == 1


def _unresolved_candidate_fixture(tmp_path: Path, source: str, path: str):
    files = {path: _resolver_source(source)}
    index = _fixture_index(files, tmp_path)
    primitives = _primitive_table(index, ())
    resolutions = resolve_calls(index, (), primitives)
    entity_semantics = MappingProxyType({
        identity: domain_lifecycle.EntitySemantics(
            identity, (), (), (), (), (), "a" * 64)
        for identity in index.entities
    })
    file_identity = f"{path}::@file"
    semantics = domain_lifecycle.SemanticIndex(
        entity_semantics,
        MappingProxyType({file_identity: domain_lifecycle.FileSemantics(
            file_identity, (), (), (), "b" * 64)}),
        "c" * 64, "d" * 64, _semantic_digest_inputs())
    return index, resolutions, semantics


@pytest.mark.parametrize(
    "body",
    (
        "setattr(self, 'field', value)",
        "delattr(self, 'field')",
        "alias = self.field; alias = value; alias.append(1)",
        "self.field.unknown_mutator(value)",
        "consume(self.field)",
    ),
)
def test_candidate_policy_mutable_field_uncertainty_is_never_ignored(
    tmp_path: Path, body: str
) -> None:
    path = "src/lockstep/unresolved_fields.py"
    index, resolutions, semantics = _unresolved_candidate_fixture(
        tmp_path,
        "class Mutable:\n    def mutate(self, value):\n"
        + textwrap.indent(body, "        ") + "\n",
        path,
    )

    report = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions)

    assert report.unresolved_callsites
    assert report.functions[f"{path}::Mutable.mutate"].unresolved_callsites


def test_candidate_policy_class_exact_mutators_and_immutable_local_aliases(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/class_mutators.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        class Box:
            def append(self, *args): pass
            def extend(self, *args): pass
            def insert(self, *args): pass
            def remove(self, *args): pass
            def pop(self, *args): pass
            def clear(self, *args): pass
            def sort(self, *args): pass
            def reverse(self, *args): pass
            def update(self, *args): pass
            def setdefault(self, *args): pass
            def add(self, *args): pass
            def discard(self, *args): pass
            def difference_update(self, *args): pass
            def intersection_update(self, *args): pass
            def symmetric_difference_update(self, *args): pass
        class Mutable:
            def initialize(self):
                self.items = Box(); self.mapping = Box(); self.values = Box()
            def mutate(self):
                alias = self.items
                alias.append(1); self.items.extend([]); self.items.insert(0, 1)
                self.items.remove(1); self.items.pop(); self.items.clear()
                self.items.sort(); self.items.reverse()
                self.mapping.update({}); self.mapping.setdefault("key", 1)
                self.values.add(2); self.values.discard(1)
                self.values.difference_update([])
                self.values.intersection_update([])
                self.values.symmetric_difference_update([])
        """,
        path=path,
        primitive_rows=(_primitive_callsite_row(
            f"{path}::Mutable.mutate::call:0001", f"{path}::Box.append"
        ),),
    )
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).classes[f"{path}::Mutable"]

    assert metric.mutable_fields == ("self.items", "self.mapping", "self.values")


def test_candidate_policy_class_hard_trigger_order_and_nontrigger_boundaries(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/class_hard.py"
    methods = "\n".join(
        f"    def m{i}(self): self.f{i} = {i}" for i in range(25)
    )
    index, resolutions, semantics = _propagate_fixture(
        tmp_path, f"class Aggregate:\n{methods}\n", path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).classes[f"{path}::Aggregate"]

    assert metric.method_count == 25
    assert metric.mutable_field_count == 25
    assert metric.hard_triggers == _CANDIDATE_HARD_TRIGGER_ORDER["class"]
    assert metric.candidate is True

    boundary_path = "src/lockstep/class_boundary.py"
    boundary_methods = "\n".join(
        f"    def m{i}(self): self.f{i} = {i}" for i in range(24))
    boundary_index, boundary_resolutions, boundary_semantics = _propagate_fixture(
        tmp_path, f"class Aggregate:\n{boundary_methods}\n", path=boundary_path)
    boundary = evaluate_candidates(
        boundary_index, measure_legacy_metrics(boundary_index), boundary_semantics,
        boundary_resolutions).classes[f"{boundary_path}::Aggregate"]
    assert boundary.method_count == 24 and boundary.mutable_field_count == 24
    assert boundary.hard_triggers == ()


def test_candidate_policy_class_composite_rule_without_hard_trigger(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/class_composite.py"
    public = [f"    def m{i}(self): self.f{i} = {i}" for i in range(8)]
    private = [f"    def _m{i}(self): pass" for i in range(6)]
    index, resolutions, semantics = _propagate_fixture(
        tmp_path, "class Aggregate:\n" + "\n".join((*public, *private)) + "\n",
        path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).classes[f"{path}::Aggregate"]

    assert tuple(metric.signals.values()) == (False, True, True, True, False, False)
    assert metric.composite_score == 3
    assert metric.hard_triggers == ()
    assert metric.candidate is True


def test_candidate_policy_class_lifecycle_mixing_drives_exact_three_formula(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/class_lifecycle.py"
    methods = ["    def m0(self): first(); second()"] + [
        f"    def _m{i}(self): pass" for i in range(1, 15)]
    source = "def first(): pass\ndef second(): pass\nclass Aggregate:\n" + "\n".join(methods)
    index, resolutions, semantics = _propagate_fixture(
        tmp_path, source, path=path,
        lifecycle_rows=(
            {"binding_kind": "entity", "binding": f"{path}::first",
             "target": f"{path}::first", "discriminant": {"kind": "none"},
             "transition_id": "process.prepare"},
            {"binding_kind": "entity", "binding": f"{path}::second",
             "target": f"{path}::second", "discriminant": {"kind": "none"},
             "transition_id": "publication.apply"},
        ))
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).classes[f"{path}::Aggregate"]
    assert metric.propagated_domains == ()
    assert metric.propagated_lifecycle_clusters == ("process-execution", "publication")
    assert metric.composite_score == 3
    assert metric.signals["lifecycle_mixing"] is True
    assert metric.candidate is True


def test_candidate_policy_file_dependency_components_are_exact_and_containment_is_not_edge(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/file_components.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        import json
        def decorator(value): return value
        class Base: pass
        def leaf(): pass
        alias = leaf
        @decorator
        class Connected(Base):
            def method(self): alias()
        class ContainedOnly:
            def method(self): pass
        def isolated(): pass
        def encode_a(): return json.dumps({})
        def encode_b(): return json.dumps({})
        """,
        path=path,
        allowlist=("json.dumps",),
    )
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]

    assert metric.definition_count == 10
    assert metric.class_count == 3
    assert metric.definition_dependency_components == 4


def test_candidate_policy_file_attributes_nested_function_and_class_dependencies_to_top_owner(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/nested_components.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        def leaf_a(): pass
        def leaf_b(): pass
        def outer():
            def nested(): leaf_a()
            class Nested:
                def method(self): leaf_b()
            nested()
        def isolated(): pass
        """, path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]
    assert metric.definition_count == 7
    assert metric.definition_dependency_components == 2


def test_candidate_policy_file_plain_alias_and_import_references_form_edges(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/reference_components.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        import json
        def leaf(): pass
        alias = leaf
        def use_alias(): return alias
        def import_a(): return json
        def import_b(): return json
        def isolated(): pass
        """, path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]
    assert metric.definition_dependency_components == 3


def test_candidate_policy_file_rebound_alias_and_lexical_shadowing_are_not_edges(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/reference_shadowing.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        import json
        def leaf(): pass
        def other(): pass
        alias = leaf
        alias = other
        def use_alias(): return alias
        def local_json():
            json = 1
            return json
        def module_json(): return json
        def isolated(): pass
        """, path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]
    assert metric.definition_dependency_components == 6


def test_candidate_policy_file_nested_scope_shadowing_is_not_module_reference(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/nested_reference_shadowing.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        import json
        def outer():
            def nested():
                json = 1
                return json
            return nested
        def module_json(): return json
        def isolated(): pass
        """, path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]
    assert metric.definition_dependency_components == 3


@pytest.mark.parametrize(("source", "components"), (
    ("""
     def helper(): pass
     def outer():
         def helper(): pass
         return helper
     def isolated(): pass
     """, 3),
    ("""
     import json
     def outer():
         import decimal as json
         return json
     def module_json(): return json
     def isolated(): pass
     """, 3),
    ("""
     def leaf(): pass
     def outer():
         [leaf for leaf in ()]
         return leaf
     def isolated(): pass
     """, 2),
    ("""
     import json
     class Outer:
         json = 1
         class Nested:
             observed = json
     def module_json(): return json
     def isolated(): pass
     """, 2),
))
def test_candidate_policy_file_uses_python_lexical_binding_semantics(
    tmp_path: Path, source: str, components: int
) -> None:
    path = "src/lockstep/python_scope_components.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path, source, path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]
    assert metric.definition_dependency_components == components


@pytest.mark.parametrize("source", (
    """
    def leaf(): pass
    def outer():
        first = lambda leaf: leaf; second = lambda: leaf
        return first, second
    def isolated(): pass
    """,
    """
    def leaf(): pass
    def outer(value=leaf):
        leaf = 1
        return value
    def isolated(): pass
    """,
))
def test_candidate_policy_file_attributes_same_line_scopes_and_defaults_exactly(
    tmp_path: Path, source: str
) -> None:
    path = "src/lockstep/scope_defaults.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path, source, path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]
    assert metric.definition_dependency_components == 2


@pytest.mark.parametrize("signature", (
    "def outer(value: Leaf): return value",
    "def outer() -> Leaf: return Leaf()",
))
def test_candidate_policy_file_attributes_runtime_annotations(
    tmp_path: Path, signature: str
) -> None:
    path = "src/lockstep/annotation_components.py"
    source = f"class Leaf: pass\n{signature}\ndef isolated(): pass"
    index, resolutions, semantics = _propagate_fixture(tmp_path, source, path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]
    assert metric.definition_dependency_components == 2


def test_candidate_policy_file_does_not_double_count_comprehension_outer_iterable(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/comprehension_scope.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        import json
        class Outer:
            json = ()
            values = (item for item in json)
        def module_json(): return json
        def isolated(): pass
        """, path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]
    assert metric.definition_dependency_components == 3


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP-695 syntax needs Python 3.12")
@pytest.mark.parametrize(("source", "components"), (
    ("class T: pass\ndef outer[T](value: T): return value\ndef isolated(): pass", 3),
    ("class Leaf: pass\ndef outer[T: Leaf](value: T): return value\ndef isolated(): pass", 2),
    ("class T: pass\ndef outer():\n    type Alias[T] = T\n    return Alias\ndef isolated(): pass", 3),
    ("class Leaf: pass\ndef outer():\n    type Alias[T: Leaf] = T\n    return Alias\ndef isolated(): pass", 2),
))
def test_candidate_policy_file_resolves_pep695_type_parameter_scopes(
    tmp_path: Path, source: str, components: int
) -> None:
    path = "src/lockstep/type_parameter_components.py"
    index, resolutions, semantics = _propagate_fixture(tmp_path, source, path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]
    assert metric.definition_dependency_components == components


def test_candidate_policy_file_subsystems_formula_and_hard_boundary(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/file_threshold.py"
    imports = "\n".join((
        "import lockstep.runtime.effects", "import lockstep.workflow.lowering",
        "import lockstep.authoring", "import json",
    ))
    definitions = "\n".join(f"def f{i}(): pass" for i in range(51))
    index, resolutions, semantics = _propagate_fixture(
        tmp_path, f"{imports}\n{definitions}\n", path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]

    assert metric.definition_count == 51
    assert metric.subsystem_imports == ("authoring", "json", "runtime", "workflow")
    assert metric.subsystem_import_count == 4
    assert metric.hard_triggers == ("definition_count_gt_50",)
    assert metric.candidate is True

    boundary_path = "src/lockstep/file_boundary.py"
    boundary_definitions = "\n".join(f"def f{i}(): pass" for i in range(50))
    boundary_index, boundary_resolutions, boundary_semantics = _propagate_fixture(
        tmp_path, boundary_definitions, path=boundary_path)
    boundary = evaluate_candidates(
        boundary_index, measure_legacy_metrics(boundary_index), boundary_semantics,
        boundary_resolutions).files[f"{boundary_path}::@file"]
    assert boundary.definition_count == 50
    assert boundary.hard_triggers == ()


def test_candidate_policy_file_subsystems_exclude_future_directives(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/future_imports.py"
    index, resolutions, semantics = _propagate_fixture(
        tmp_path,
        """
        from __future__ import annotations
        from collections.abc import Callable
        from typing import Any
        import lockstep.runtime.effects
        def value(callback: Callable[[Any], Any]): return callback
        """,
        path=path,
    )
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]

    assert metric.subsystem_imports == ("collections", "runtime", "typing")
    assert metric.subsystem_import_count == 3


def test_candidate_policy_file_composite_rule_without_hard_trigger(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/file_composite.py"
    imports = "\n".join(("import lockstep.runtime", "import lockstep.workflow",
                          "import lockstep.authoring", "import json"))
    classes = "\n".join(f"class C{i}: pass" for i in range(6))
    functions = "\n".join(f"def f{i}(): pass" for i in range(18))
    index, resolutions, semantics = _propagate_fixture(
        tmp_path, f"{imports}\n{classes}\n{functions}\n", path=path)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]

    assert tuple(metric.signals.values()) == (False, True, True, True, False, False)
    assert metric.composite_score == 3
    assert metric.hard_triggers == ()
    assert metric.candidate is True


def test_candidate_policy_file_lifecycle_mixing_drives_exact_three_formula(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/file_lifecycle.py"
    names = ("process", "publish", "deliver")
    source = "\n".join(
        [*(f"def {name}(): pass" for name in names),
         *(f"def f{i}(): pass" for i in range(22))])
    lifecycle_rows = tuple({
        "binding_kind": "entity", "binding": f"{path}::{name}",
        "target": f"{path}::{name}", "discriminant": {"kind": "none"},
        "transition_id": transition,
    } for name, transition in (
        ("deliver", "delivery.deliver"), ("process", "process.prepare"),
        ("publish", "publication.apply")))
    index, resolutions, semantics = _propagate_fixture(
        tmp_path, source, path=path, lifecycle_rows=lifecycle_rows)
    metric = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions
    ).files[f"{path}::@file"]
    assert metric.definition_count == 25
    assert metric.propagated_domains == ()
    assert metric.propagated_lifecycle_clusters == (
        "process-execution", "publication", "delivery")
    assert metric.composite_score == 3
    assert metric.signals["lifecycle_mixing"] is True
    assert metric.candidate is True


def _empty_architecture_report():
    return candidate_policy.ArchitectureReport(
        MappingProxyType({}), MappingProxyType({}), MappingProxyType({}),
        MappingProxyType({}), (), "a" * 64, "b" * 64, "c" * 64,
        "d" * 64, "e" * 64, "task-12c-test", "v1",
    )


def test_manifest_verdict_record_is_exact_frozen_slotted() -> None:
    record_type = manifest_verifier.ManifestVerdict
    expected = ("valid", "errors", "accepted_exceptions")
    assert tuple(field.name for field in fields(record_type)) == expected
    assert record_type.__slots__ == expected
    assert record_type.__dataclass_params__.frozen


def test_manifest_checked_in_empty_ratchet_is_closed_canonical_json() -> None:
    path = ARCHITECTURE_TEST_ROOT / "architecture_exceptions.json"
    raw = path.read_bytes()
    manifest = json.loads(raw)
    assert raw == json.dumps(manifest, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":")).encode()
    assert not raw.endswith(b"\n")
    assert set(manifest) == {
        "schema_version", "ratchet_version", "reference_commit", "scan_root",
        "population", "analyzer_digest", "primitive_digest", "allowlist_digest",
        "lifecycle_digest", "schema_digest", "threshold_digest", "exceptions",
    }
    assert manifest["schema_version"] == 1
    assert manifest["ratchet_version"] == "v1"
    assert manifest["scan_root"] == "src/lockstep"
    assert manifest["exceptions"] == []


def test_repository_ratchet_lists_every_unremediated_candidate() -> None:
    manifest = json.loads(
        (ARCHITECTURE_TEST_ROOT / "architecture_exceptions.json").read_bytes())
    assert manifest["exceptions"] == []
    report = _repository_architecture_report()
    assert report.unresolved_callsites == ()
    verdict = manifest_verifier.ManifestVerdict(
        False, ("repository candidates remain unremediated",), ())
    rendered = render_report(report, verdict)
    candidates = json.loads(rendered)["candidates"]

    assert candidates == [], rendered


def test_recovery_driver_remediation_matches_exact_analyzer_projection() -> None:
    report = _repository_architecture_report()
    assert report.unresolved_callsites == ()
    scoped_paths = {
        "src/lockstep/runtime/recovery_driver.py",
        "src/lockstep/runtime/_recovery_backfill.py",
        "src/lockstep/runtime/_recovery_watch_errors.py",
        "src/lockstep/runtime/_recovery_watch_enumeration.py",
        "src/lockstep/runtime/_recovery_watch_admission.py",
        "src/lockstep/runtime/_recovery_watch_drive.py",
        "src/lockstep/runtime/_recovery_watch_inspection.py",
        "src/lockstep/runtime/_recovery_watch_settlement.py",
    }
    scoped_candidates = sorted(
        (kind, identity, metric.composite_score, metric.hard_triggers)
        for kind in ("functions", "one_hops", "classes", "files")
        for identity, metric in getattr(report, kind).items()
        if identity.partition("::")[0] in scoped_paths and metric.candidate
    )
    assert scoped_candidates == [
        (
            "functions",
            "src/lockstep/runtime/_recovery_backfill.py::_bound_runtime",
            3,
            (),
        )
    ]
    assert (
        "src/lockstep/runtime/recovery_driver.py::"
        "RecoveryDriver._sweep_run_drive_watches::@one_hop"
    ) not in report.one_hops
    assert (
        "src/lockstep/runtime/recovery_driver.py::RecoveryDriver._drive_run_watch"
    ) not in report.functions


def test_hook_decision_remediation_matches_exact_analyzer_projection() -> None:
    report = _repository_architecture_report()
    assert report.unresolved_callsites == ()
    hooks = "src/lockstep/runtime/hooks.py"
    scoped_paths = {
        hooks,
        "src/lockstep/runtime/_hook_stop_decision.py",
        "src/lockstep/runtime/_hook_pretool_decision.py",
        "src/lockstep/runtime/_hook_posttool_decision.py",
    }
    scoped_candidates = sorted(
        (kind, identity, metric.composite_score, metric.hard_triggers)
        for kind in ("functions", "one_hops", "classes", "files")
        for identity, metric in getattr(report, kind).items()
        if identity.partition("::")[0] in scoped_paths and metric.candidate
    )
    assert scoped_candidates == [
        (
            "functions",
            f"{hooks}::_find_marked_run_id",
            4,
            ("cognitive_gt_25", "nesting_gt_4"),
        ),
        (
            "functions",
            f"{hooks}::_find_run_id",
            4,
            ("cognitive_gt_25", "nesting_gt_4"),
        ),
    ]
    for kind, identity in (
        ("functions", f"{hooks}::hook_stop"),
        ("functions", f"{hooks}::hook_pretool"),
        ("one_hops", f"{hooks}::hook_pretool::@one_hop"),
        ("functions", f"{hooks}::hook_posttool"),
        ("one_hops", f"{hooks}::hook_posttool::@one_hop"),
    ):
        assert not getattr(report, kind)[identity].candidate


def test_command_service_remediation_matches_exact_analyzer_projection() -> None:
    report = _repository_architecture_report()
    assert report.unresolved_callsites == ()
    scoped_paths = {
        "src/lockstep/runtime/service.py",
        "src/lockstep/runtime/_service_activation_lifecycle.py",
        "src/lockstep/runtime/_service_composition.py",
        "src/lockstep/runtime/_service_effect_drive.py",
        "src/lockstep/runtime/_service_interrupt_descriptors.py",
        "src/lockstep/runtime/_service_payloads.py",
        "src/lockstep/runtime/_service_preflight.py",
        "src/lockstep/runtime/_service_publication_consent.py",
        "src/lockstep/runtime/_service_recovery_pump.py",
        "src/lockstep/runtime/_service_session.py",
        "src/lockstep/runtime/_service_start.py",
        "src/lockstep/runtime/_service_values.py",
        "src/lockstep/runtime/_service_worker.py",
        "src/lockstep/runtime/_service_writable_core.py",
    }
    scoped_candidates = sorted(
        (kind, identity, metric.composite_score, metric.hard_triggers)
        for kind in ("functions", "one_hops", "classes", "files")
        for identity, metric in getattr(report, kind).items()
        if identity.partition("::")[0] in scoped_paths and metric.candidate
    )
    assert scoped_candidates == [
        (
            "functions",
            "src/lockstep/runtime/_service_effect_drive.py::_ServiceEffectDrive._drive_recovered_run",
            3,
            (),
        ),
        (
            "functions",
            "src/lockstep/runtime/_service_preflight.py::preflight_recipe",
            3,
            (),
        ),
        (
            "one_hops",
            "src/lockstep/runtime/_service_effect_drive.py::_ServiceEffectDrive._drive_recovered_run::@one_hop",
            3,
            (),
        ),
    ]
    assert not report.files["src/lockstep/runtime/service.py::@file"].candidate
    assert not report.classes[
        "src/lockstep/runtime/service.py::LockstepCommandService"
    ].candidate
    assert (
        "src/lockstep/runtime/service.py::LockstepCommandService._completion_pump"
        not in report.functions
    )


def test_n1_adapter_remediation_matches_exact_analyzer_projection() -> None:
    report = _repository_architecture_report()
    assert report.unresolved_callsites == ()
    scoped_paths = {
        "src/lockstep/cli.py",
        "src/lockstep/_cli_scenario.py",
        "src/lockstep/_cli_consent.py",
        "src/lockstep/_cli_parser.py",
        "src/lockstep/_cli_support.py",
        "src/lockstep/mcp/server.py",
        "src/lockstep/mcp/_scenario_dryrun.py",
    }
    scoped_candidates = sorted(
        (kind, identity, metric.composite_score, metric.hard_triggers)
        for kind in ("functions", "one_hops", "classes", "files")
        for identity, metric in getattr(report, kind).items()
        if identity.partition("::")[0] in scoped_paths and metric.candidate
    )
    assert scoped_candidates == [
        (
            "files",
            "src/lockstep/mcp/server.py::@file",
            4,
            (),
        ),
        (
            "functions",
            "src/lockstep/mcp/server.py::_containment_errors",
            3,
            (),
        ),
        (
            "functions",
            "src/lockstep/mcp/server.py::_project_for_context",
            3,
            ("nesting_gt_4",),
        ),
    ]
    for kind, identity in (
        ("files", "src/lockstep/cli.py::@file"),
        ("functions", "src/lockstep/cli.py::_cmd_scenario"),
        ("one_hops", "src/lockstep/cli.py::_cmd_scenario::@one_hop"),
        ("functions", "src/lockstep/cli.py::_cmd_consent"),
        ("functions", "src/lockstep/cli.py::_build_parser"),
        ("one_hops", "src/lockstep/cli.py::main::@one_hop"),
        ("functions", "src/lockstep/mcp/server.py::scenario_dryrun"),
        ("one_hops", "src/lockstep/mcp/server.py::scenario_dryrun::@one_hop"),
    ):
        metric = getattr(report, kind).get(identity)
        assert metric is None or not metric.candidate


def test_n2_workflow_remediation_matches_exact_analyzer_projection() -> None:
    report = _repository_architecture_report()
    assert report.unresolved_callsites == ()
    scoped_names = {
        "lowering.py", "semantics.py",
        "_lowering_artifact_matching.py", "_lowering_artifacts.py",
        "_lowering_block_dispatch.py", "_lowering_blocks.py",
        "_lowering_call.py", "_lowering_call_bundle.py",
        "_lowering_call_planning.py", "_lowering_child_specialization.py",
        "_lowering_conditions.py", "_lowering_contracts.py",
        "_lowering_core.py", "_lowering_descriptors.py", "_lowering_flow.py",
        "_lowering_graph.py", "_lowering_graph_descriptor.py",
        "_lowering_graph_driver.py", "_lowering_graph_nodes.py",
        "_lowering_graph_plan.py", "_lowering_graph_rewrite.py",
        "_lowering_graph_validation.py", "_lowering_identity.py",
        "_lowering_parallel.py", "_semantics_blocks.py", "_semantics_calls.py",
        "_semantics_catalog.py", "_semantics_common.py",
        "_semantics_contracts.py", "_semantics_decisions.py",
        "_semantics_parallel.py", "_semantics_repeats.py",
        "_semantics_validation.py",
    }
    scoped_paths = {
        f"src/lockstep/workflow/{name}" for name in scoped_names
    }
    candidates = sorted(
        (kind, identity, metric.composite_score, metric.hard_triggers)
        for kind in ("functions", "one_hops", "classes", "files")
        for identity, metric in getattr(report, kind).items()
        if identity.partition("::")[0] in scoped_paths and metric.candidate
    )
    assert candidates == []


def test_workflow_schema_parser_decomposition_does_not_worsen_cohesion() -> None:
    report = _repository_architecture_report()
    assert report.unresolved_callsites == ()
    metric = report.classes["src/lockstep/workflow/schema.py::_Parser"]

    assert metric.cohesion_components < 3
    assert metric.signals["cohesion_components"] is False
    assert metric.composite_score <= 2


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ({"unknown": True}, "manifest keys"),
        ({"schema_version": "1"}, "schema_version"),
        ({"scan_root": "./src/lockstep"}, "scan_root"),
        ({"reference_commit": 7}, "reference_commit"),
        ({"analyzer_digest": 7}, "analyzer_digest"),
        ({"exceptions": [{"entity": "missing"}]}, "exception"),
    ),
)
def test_manifest_rejects_malformed_or_stale_claims_without_trusting_stored_data(
    tmp_path: Path, mutation: Mapping[str, object], reason: str
) -> None:
    manifest = {
        "schema_version": 1, "ratchet_version": "v1",
        "reference_commit": "0" * 40, "scan_root": "src/lockstep",
        "population": [], "analyzer_digest": "a" * 64,
        "primitive_digest": "b" * 64, "allowlist_digest": "a" * 64,
        "lifecycle_digest": "c" * 64, "schema_digest": "d" * 64,
        "threshold_digest": "e" * 64, "exceptions": [],
    }
    manifest.update(mutation)

    verdict = verify_manifest(
        _empty_architecture_report(), manifest,
        repo_root=tmp_path, current_commit="f" * 40,
    )

    assert verdict.valid is False
    assert any(reason in error for error in verdict.errors)
    assert verdict.accepted_exceptions == ()


def _member_closure_sha256(items: tuple[tuple[str, str], ...]) -> str:
    value = bytearray(b"lockstep.architecture-members/v1\0")
    for identity, digest in items:
        value.extend(identity.encode("utf-8"))
        value.append(0)
        value.extend(digest.encode("ascii"))
        value.append(0)
    return hashlib.sha256(value).hexdigest()


def test_manifest_reads_review_and_historical_inputs_from_exact_git_tree_blob(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "history"
    inner = repo / "lockstep"
    architecture = inner / "engine" / "tests" / "architecture"
    source_path = inner / "engine" / "src" / "lockstep" / "sample.py"
    review_path = inner / ".superpowers" / "reviews" / "candidate.md"
    gate_path = architecture / "test_gate.py"
    for path in (architecture, source_path.parent, review_path.parent):
        path.mkdir(parents=True, exist_ok=True)
    source = ("def god(value):\n" + "".join(
        f"    if value == {number}: pass\n" for number in range(16)
    )).encode()
    source_path.write_bytes(source)
    gate_path.write_text("def test_focus(): pass\n", encoding="utf-8")
    stable_path = "src/lockstep/sample.py"
    index = build_source_index(inner / "engine", (stable_path,), {stable_path: source})
    allowlist = {"schema_version": 1, "targets": []}
    primitives = _primitive_table(index, ())
    lifecycle = _lifecycle_table()
    schema = json.loads(
        (ARCHITECTURE_TEST_ROOT / "architecture_metrics.schema.json").read_bytes()
    )
    thresholds = json.loads(
        (ARCHITECTURE_TEST_ROOT / "architecture_thresholds.json").read_bytes()
    )
    resolutions = resolve_calls(index, (), primitives)
    semantics = propagate_semantics(
        index, resolutions, primitives, lifecycle,
        digest_inputs=domain_lifecycle.SemanticDigestInputs(
            _canonical_sha256(allowlist), _canonical_sha256(schema),
            _canonical_sha256(thresholds), "task-12c-test", "v1"),
    )
    report = evaluate_candidates(
        index, measure_legacy_metrics(index), semantics, resolutions)
    identity = f"{stable_path}::god"
    metric = report.functions[identity]
    semantic_digest = semantics.entities[identity].semantic_dependency_sha256
    review_bytes = (
        "# Independent Architecture Review\n\n"
        f"Entity: `{identity}`\n\n"
        f"Semantic dependency SHA-256: `{semantic_digest}`\n\n"
        "Finding counts: C0 / I0 / M0\n\nVerdict: PASS\n"
    ).encode()
    review_path.write_bytes(review_bytes)
    wrong_review = review_path.with_name("wrong.md")
    wrong_review_bytes = review_bytes.replace(identity.encode(), b"src/lockstep/wrong.py::wrong")
    wrong_review.write_bytes(wrong_review_bytes)
    incidental_review = review_path.with_name("incidental.md")
    incidental_bytes = (
        "Entity: `src/lockstep/wrong.py::wrong`\n"
        f"Semantic dependency SHA-256: `{'0' * 64}`\n"
        f"Notes only: {identity} {semantic_digest}\n"
    ).encode()
    incidental_review.write_bytes(incidental_bytes)
    review_link = review_path.with_name("candidate-link.md")
    review_link.symlink_to(review_path.name)
    rule_values = {
        "architecture_effect_free_allowlist.json": allowlist,
        "architecture_effect_primitives.json": primitives,
        "architecture_lifecycle.json": lifecycle,
        "architecture_metrics.schema.json": schema,
        "architecture_thresholds.json": thresholds,
    }
    for filename, value in rule_values.items():
        (architecture / filename).write_bytes(json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":")).encode())
    analyzer_names = (
        "architecture_source_index.py", "architecture_legacy_metrics.py",
        "architecture_call_resolver.py", "architecture_domain_lifecycle.py",
        "architecture_candidate_policy.py", "architecture_manifest_verifier.py",
        "architecture_diagnostics.py",
    )
    for filename in analyzer_names:
        (architecture / filename).write_bytes(
            (ARCHITECTURE_TEST_ROOT / filename).read_bytes())
    (architecture / "architecture_candidate_policy.py").write_text(
        "raise RuntimeError('historical analyzer Python must not execute')\n",
        encoding="utf-8")

    subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
    subprocess.run(("git", "add", "."), cwd=repo, check=True)
    subprocess.run(("git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "review"),
                   cwd=repo, check=True)
    review_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repo, check=True,
        capture_output=True, text=True).stdout.strip()
    (repo / "marker").write_text("current\n", encoding="utf-8")
    subprocess.run(("git", "add", "marker"), cwd=repo, check=True)
    subprocess.run(("git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "current"),
                   cwd=repo, check=True)
    current_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repo, check=True,
        capture_output=True, text=True).stdout.strip()

    evidence_without_digest = {
        "project_relative_artifact_path": ".superpowers/reviews/candidate.md",
        "git_tree_artifact_path": "lockstep/.superpowers/reviews/candidate.md",
        "review_commit": review_commit,
        "artifact_blob_sha256": hashlib.sha256(review_bytes).hexdigest(),
        "reviewer_role": "architecture", "verdict": "PASS",
        "finding_counts": {"critical": 0, "important": 0, "minor": 0},
        "reviewed_semantic_dependency_sha256": semantic_digest,
    }
    evidence = {**evidence_without_digest,
        "review_evidence_sha256": _canonical_sha256(evidence_without_digest)}
    baseline = {
        field.name: getattr(metric, field.name) for field in fields(metric)
    }
    baseline["signals"] = dict(metric.signals)
    baseline = json.loads(json.dumps(baseline))
    exception = {
        "entity": identity, "kind": "function",
        "trigger_reasons": ["hard:cyclomatic_gt_15"],
        "responsibility": "Validate one closed sample branch matrix",
        "invariant": "All branches preserve one exact validation decision",
        "focused_gate": ["lockstep/engine/tests/architecture/test_gate.py::test_focus"],
        "baseline_metrics": baseline,
        "source_sha256": index.entities[identity].span.sha256,
        "semantic_dependency_sha256": semantic_digest,
        "member_closure_sha256": _member_closure_sha256(((identity, semantic_digest),)),
        "review_evidence": evidence,
        "next_review_gate": "task-12-final-source-review",
        "expires_on": {name: True for name in (
            "source_changed", "semantic_dependency_changed", "member_closure_changed",
            "any_metric_increased", "any_component_increased",
            "composite_score_increased", "new_domain", "new_lifecycle_cluster",
            "focused_gate_missing_or_renamed", "review_evidence_unverifiable",
            "analyzer_or_rule_version_changed")},
    }
    assert set(exception) == {
        "entity", "kind", "trigger_reasons", "responsibility", "invariant",
        "focused_gate", "baseline_metrics", "source_sha256",
        "semantic_dependency_sha256", "member_closure_sha256", "review_evidence",
        "next_review_gate", "expires_on",
    }
    assert set(evidence) == {
        "project_relative_artifact_path", "git_tree_artifact_path", "review_commit",
        "artifact_blob_sha256", "reviewer_role", "verdict", "finding_counts",
        "reviewed_semantic_dependency_sha256", "review_evidence_sha256",
    }
    assert set(exception["expires_on"]) == {
        "source_changed", "semantic_dependency_changed", "member_closure_changed",
        "any_metric_increased", "any_component_increased",
        "composite_score_increased", "new_domain", "new_lifecycle_cluster",
        "focused_gate_missing_or_renamed", "review_evidence_unverifiable",
        "analyzer_or_rule_version_changed",
    }
    assert set(baseline) == set(_CANDIDATE_FIELD_ORDER["FunctionMetrics"])
    analyzer_digest = _canonical_sha256([
        {"path": filename, "sha256": hashlib.sha256(
            (architecture / filename).read_bytes()).hexdigest()}
        for filename in analyzer_names
    ])
    manifest = {
        "schema_version": 1, "ratchet_version": "v1",
        "reference_commit": review_commit, "scan_root": "src/lockstep",
        "population": [{"path": stable_path,
                        "source_sha256": index.file_sha256[stable_path]}],
        "analyzer_digest": analyzer_digest,
        "primitive_digest": semantics.primitive_digest,
        "allowlist_digest": semantics.digest_inputs.allowlist_digest,
        "lifecycle_digest": semantics.lifecycle_digest,
        "schema_digest": semantics.digest_inputs.schema_digest,
        "threshold_digest": semantics.digest_inputs.threshold_digest,
        "exceptions": [exception],
    }

    verdict = verify_manifest(
        report, manifest, repo_root=repo, current_commit=current_commit)
    assert verdict.valid is True
    assert verdict.errors == ()
    assert verdict.accepted_exceptions == (identity,)

    malformed_population = json.loads(json.dumps(manifest))
    malformed_population["population"][0]["path"] = []
    rejected = verify_manifest(
        report, malformed_population, repo_root=repo, current_commit=current_commit)
    assert rejected.valid is False
    assert any("population path" in error for error in rejected.errors)

    malformed_entity = json.loads(json.dumps(manifest))
    malformed_entity["exceptions"][0]["entity"] = []
    rejected = verify_manifest(
        report, malformed_entity, repo_root=repo, current_commit=current_commit)
    assert rejected.valid is False
    assert any("exception entity" in error for error in rejected.errors)

    review_path.write_text("checkout substitution\n", encoding="utf-8")
    source_path.write_text("raise RuntimeError('checkout source substitution')\n",
                           encoding="utf-8")
    (architecture / "architecture_thresholds.json").write_text(
        '{"checkout":"substitution"}', encoding="utf-8")
    manifest_verifier._historical_cached.cache_clear()
    assert verify_manifest(
        report, manifest, repo_root=repo, current_commit=current_commit).valid is True
    bad = json.loads(json.dumps(manifest))
    bad["exceptions"][0]["review_evidence"]["artifact_blob_sha256"] = "0" * 64
    bad_evidence = bad["exceptions"][0]["review_evidence"]
    bad_evidence["review_evidence_sha256"] = _canonical_sha256({
        key: value for key, value in bad_evidence.items()
        if key != "review_evidence_sha256"})
    rejected = verify_manifest(report, bad, repo_root=repo, current_commit=current_commit)
    assert rejected.valid is False
    assert any("artifact blob" in error for error in rejected.errors)

    def rejected_manifest(value, reason: str) -> None:
        outcome = verify_manifest(report, value, repo_root=repo, current_commit=current_commit)
        assert outcome.valid is False
        assert any(reason in error for error in outcome.errors), outcome.errors

    mutations = []
    duplicate = json.loads(json.dumps(manifest)); duplicate["exceptions"].append(duplicate["exceptions"][0])
    mutations.append((duplicate, "duplicate"))
    missing = json.loads(json.dumps(manifest)); missing["exceptions"] = []
    mutations.append((missing, "candidate"))
    trigger = json.loads(json.dumps(manifest)); trigger["exceptions"][0]["trigger_reasons"] = ["signal:cyclomatic"]
    mutations.append((trigger, "trigger"))
    stale_source = json.loads(json.dumps(manifest)); stale_source["exceptions"][0]["source_sha256"] = "0" * 64
    mutations.append((stale_source, "source"))
    stale_semantic = json.loads(json.dumps(manifest)); stale_semantic["exceptions"][0]["semantic_dependency_sha256"] = "0" * 64
    mutations.append((stale_semantic, "semantic"))
    stale_closure = json.loads(json.dumps(manifest)); stale_closure["exceptions"][0]["member_closure_sha256"] = "0" * 64
    mutations.append((stale_closure, "member closure"))
    stale_metric = json.loads(json.dumps(manifest)); stale_metric["exceptions"][0]["baseline_metrics"]["cyclomatic"] -= 1
    mutations.append((stale_metric, "baseline"))
    bad_expiry = json.loads(json.dumps(manifest)); bad_expiry["exceptions"][0]["expires_on"]["source_changed"] = False
    mutations.append((bad_expiry, "expires_on"))
    bad_focus = json.loads(json.dumps(manifest)); bad_focus["exceptions"][0]["focused_gate"] = ["missing.py::test_missing"]
    mutations.append((bad_focus, "focused gate"))
    bad_path = json.loads(json.dumps(manifest)); bad_path["exceptions"][0]["review_evidence"]["project_relative_artifact_path"] = ".superpowers/reviews/../candidate.md"
    bad_path_evidence = bad_path["exceptions"][0]["review_evidence"]
    bad_path_evidence["review_evidence_sha256"] = _canonical_sha256({key: value for key, value in bad_path_evidence.items() if key != "review_evidence_sha256"})
    mutations.append((bad_path, "review path"))
    bad_role = json.loads(json.dumps(manifest)); bad_role["exceptions"][0]["review_evidence"]["reviewer_role"] = "author"
    bad_role_evidence = bad_role["exceptions"][0]["review_evidence"]
    bad_role_evidence["review_evidence_sha256"] = _canonical_sha256({key: value for key, value in bad_role_evidence.items() if key != "review_evidence_sha256"})
    mutations.append((bad_role, "reviewer role"))
    nonancestor = json.loads(json.dumps(manifest)); nonancestor["reference_commit"] = "0" * 40
    mutations.append((nonancestor, "ancestor"))
    unknown_exception = json.loads(json.dumps(manifest)); unknown_exception["exceptions"][0]["unknown"] = True
    mutations.append((unknown_exception, "exception keys"))
    missing_exception = json.loads(json.dumps(manifest)); del missing_exception["exceptions"][0]["responsibility"]
    mutations.append((missing_exception, "exception keys"))
    wrong_kind = json.loads(json.dumps(manifest)); wrong_kind["exceptions"][0]["kind"] = "method"
    mutations.append((wrong_kind, "exception kind"))
    unknown_evidence = json.loads(json.dumps(manifest)); unknown_evidence["exceptions"][0]["review_evidence"]["unknown"] = True
    mutations.append((unknown_evidence, "review evidence keys"))
    missing_evidence = json.loads(json.dumps(manifest)); del missing_evidence["exceptions"][0]["review_evidence"]["verdict"]
    mutations.append((missing_evidence, "review evidence keys"))
    wrong_expiry = json.loads(json.dumps(manifest)); wrong_expiry["exceptions"][0]["expires_on"]["unknown"] = True
    mutations.append((wrong_expiry, "expires_on keys"))
    missing_expiry = json.loads(json.dumps(manifest)); del missing_expiry["exceptions"][0]["expires_on"]["new_domain"]
    mutations.append((missing_expiry, "expires_on keys"))
    baseline_unknown = json.loads(json.dumps(manifest)); baseline_unknown["exceptions"][0]["baseline_metrics"]["unknown"] = 1
    mutations.append((baseline_unknown, "baseline"))
    baseline_missing = json.loads(json.dumps(manifest)); del baseline_missing["exceptions"][0]["baseline_metrics"]["candidate"]
    mutations.append((baseline_missing, "baseline"))
    baseline_coercion = json.loads(json.dumps(manifest)); baseline_coercion["exceptions"][0]["baseline_metrics"]["cyclomatic"] = "17"
    mutations.append((baseline_coercion, "baseline"))
    bad_population = json.loads(json.dumps(manifest)); bad_population["population"][0]["source_sha256"] = "0" * 64
    mutations.append((bad_population, "population"))
    bad_rule_digest = json.loads(json.dumps(manifest)); bad_rule_digest["threshold_digest"] = "0" * 64
    mutations.append((bad_rule_digest, "threshold"))
    nested_nonancestor = json.loads(json.dumps(manifest)); nested_nonancestor["exceptions"][0]["review_evidence"]["review_commit"] = "0" * 40
    nested_evidence = nested_nonancestor["exceptions"][0]["review_evidence"]
    nested_evidence["review_evidence_sha256"] = _canonical_sha256({key: value for key, value in nested_evidence.items() if key != "review_evidence_sha256"})
    mutations.append((nested_nonancestor, "review commit ancestor"))
    missing_node = json.loads(json.dumps(manifest)); missing_node["exceptions"][0]["focused_gate"] = ["lockstep/engine/tests/architecture/test_gate.py::test_absent"]
    mutations.append((missing_node, "focused gate"))
    bad_next_gate = json.loads(json.dumps(manifest)); bad_next_gate["exceptions"][0]["next_review_gate"] = "whenever"
    mutations.append((bad_next_gate, "next_review_gate"))
    omitted_population = json.loads(json.dumps(manifest)); omitted_population["population"] = []
    mutations.append((omitted_population, "population"))
    bad_verdict = json.loads(json.dumps(manifest)); bad_verdict["exceptions"][0]["review_evidence"]["verdict"] = "FAIL"
    bad_verdict_evidence = bad_verdict["exceptions"][0]["review_evidence"]
    bad_verdict_evidence["review_evidence_sha256"] = _canonical_sha256({key: value for key, value in bad_verdict_evidence.items() if key != "review_evidence_sha256"})
    mutations.append((bad_verdict, "verdict"))
    bad_counts = json.loads(json.dumps(manifest)); bad_counts["exceptions"][0]["review_evidence"]["finding_counts"]["minor"] = 1
    bad_counts_evidence = bad_counts["exceptions"][0]["review_evidence"]
    bad_counts_evidence["review_evidence_sha256"] = _canonical_sha256({key: value for key, value in bad_counts_evidence.items() if key != "review_evidence_sha256"})
    mutations.append((bad_counts, "finding counts"))
    bad_reviewed_digest = json.loads(json.dumps(manifest)); bad_reviewed_digest["exceptions"][0]["review_evidence"]["reviewed_semantic_dependency_sha256"] = "0" * 64
    bad_reviewed_evidence = bad_reviewed_digest["exceptions"][0]["review_evidence"]
    bad_reviewed_evidence["review_evidence_sha256"] = _canonical_sha256({key: value for key, value in bad_reviewed_evidence.items() if key != "review_evidence_sha256"})
    mutations.append((bad_reviewed_digest, "reviewed semantic"))
    bad_evidence_digest = json.loads(json.dumps(manifest)); bad_evidence_digest["exceptions"][0]["review_evidence"]["review_evidence_sha256"] = "0" * 64
    mutations.append((bad_evidence_digest, "review evidence digest"))
    bad_evidence_type = json.loads(json.dumps(manifest)); bad_evidence_type["exceptions"][0]["review_evidence"] = 7
    mutations.append((bad_evidence_type, "review evidence keys"))
    for value, reason in mutations:
        rejected_manifest(value, reason)

    unresolved_report = replace(report, unresolved_callsites=(identity + "::call:0001",))
    unresolved = verify_manifest(
        unresolved_report, manifest, repo_root=repo, current_commit=current_commit)
    assert unresolved.valid is False
    assert any("unresolved" in error for error in unresolved.errors)

    wrong = json.loads(json.dumps(manifest))
    wrong_evidence = wrong["exceptions"][0]["review_evidence"]
    wrong_evidence["project_relative_artifact_path"] = ".superpowers/reviews/wrong.md"
    wrong_evidence["git_tree_artifact_path"] = "lockstep/.superpowers/reviews/wrong.md"
    wrong_evidence["artifact_blob_sha256"] = hashlib.sha256(wrong_review_bytes).hexdigest()
    without_digest = {key: value for key, value in wrong_evidence.items()
                      if key != "review_evidence_sha256"}
    wrong_evidence["review_evidence_sha256"] = _canonical_sha256(without_digest)
    rejected_manifest(wrong, "review artifact entity")

    incidental = json.loads(json.dumps(manifest))
    incidental_evidence = incidental["exceptions"][0]["review_evidence"]
    incidental_evidence["project_relative_artifact_path"] = ".superpowers/reviews/incidental.md"
    incidental_evidence["git_tree_artifact_path"] = "lockstep/.superpowers/reviews/incidental.md"
    incidental_evidence["artifact_blob_sha256"] = hashlib.sha256(incidental_bytes).hexdigest()
    incidental_evidence["review_evidence_sha256"] = _canonical_sha256({
        key: value for key, value in incidental_evidence.items()
        if key != "review_evidence_sha256"})
    rejected_manifest(incidental, "review artifact entity")

    linked = json.loads(json.dumps(manifest))
    linked_evidence = linked["exceptions"][0]["review_evidence"]
    linked_evidence["project_relative_artifact_path"] = ".superpowers/reviews/candidate-link.md"
    linked_evidence["git_tree_artifact_path"] = "lockstep/.superpowers/reviews/candidate-link.md"
    linked_evidence["artifact_blob_sha256"] = hashlib.sha256(review_path.name.encode()).hexdigest()
    linked_evidence["review_evidence_sha256"] = _canonical_sha256({
        key: value for key, value in linked_evidence.items()
        if key != "review_evidence_sha256"})
    rejected_manifest(linked, "regular blob")

    no_longer_candidate = replace(
        metric, hard_triggers=(), candidate=False,
        signals=MappingProxyType({key: False for key in metric.signals}),
        composite_score=0,
    )
    stale_report = replace(
        report, functions=MappingProxyType({identity: no_longer_candidate}))
    stale = verify_manifest(
        stale_report, manifest, repo_root=repo, current_commit=current_commit)
    assert stale.valid is False
    assert any("noncandidate" in error or "stale exception" in error
               for error in stale.errors)

    forged_metric = replace(metric, cyclomatic=metric.cyclomatic + 1)
    forged_report = replace(
        report, functions=MappingProxyType({identity: forged_metric}))
    forged = json.loads(json.dumps(manifest))
    forged["exceptions"][0]["baseline_metrics"]["cyclomatic"] += 1
    recomputed = verify_manifest(
        forged_report, forged, repo_root=repo, current_commit=current_commit)
    assert recomputed.valid is False
    assert any("historical recomputation" in error for error in recomputed.errors)

    gate_path.unlink()
    subprocess.run(("git", "add", "-u", "lockstep/engine/tests/architecture/test_gate.py"),
                   cwd=repo, check=True)
    subprocess.run(("git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "gate removed"),
                   cwd=repo, check=True)
    removed_gate_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repo, check=True,
        capture_output=True, text=True).stdout.strip()
    missing_current_gate = verify_manifest(
        report, manifest, repo_root=repo, current_commit=removed_gate_commit)
    assert missing_current_gate.valid is False
    assert any("focused gate" in error for error in missing_current_gate.errors)

    gate_path.write_text("def test_focus(): pass\n", encoding="utf-8")
    subprocess.run(("git", "add", "lockstep/engine/tests/architecture/test_gate.py",
                    "lockstep/engine/src/lockstep/sample.py"), cwd=repo, check=True)
    subprocess.run(("git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "source drift"),
                   cwd=repo, check=True)
    changed_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repo, check=True,
        capture_output=True, text=True).stdout.strip()
    manifest_verifier._historical_cached.cache_clear()
    changed = verify_manifest(
        report, manifest, repo_root=repo, current_commit=changed_commit)
    assert changed.valid is False
    assert any("population" in error or "current commit" in error
               for error in changed.errors)


def test_diagnostics_is_pure_canonical_rendering_of_computed_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _empty_architecture_report()
    verdict = manifest_verifier.ManifestVerdict(False, ("candidate remains",), ())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("diagnostics attempted filesystem or subprocess analysis")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    rendered = render_report(report, verdict)

    assert rendered.endswith("\n")
    assert json.loads(rendered) == {
        "candidates": [],
        "digests": {
            "allowlist": "a" * 64, "analyzer": "task-12c-test",
            "lifecycle": "c" * 64, "primitive": "b" * 64,
            "rule_version": "v1", "schema": "d" * 64,
            "threshold": "e" * 64,
        },
        "manifest": {"accepted_exceptions": [], "errors": ["candidate remains"],
                     "valid": False},
        "unresolved_callsites": [],
    }


def test_diagnostics_renders_all_candidate_kinds_in_stable_order() -> None:
    function_signals = MappingProxyType({key: False for key in (
        "cyclomatic", "cognitive", "nesting", "legacy_syntactic_fanout",
        "domain_mixing", "lifecycle_mixing")})
    one_hop_signals = MappingProxyType({key: False for key in (
        "summed_cyclomatic", "summed_cognitive", "nesting",
        "legacy_syntactic_fanout_union", "domain_mixing", "lifecycle_mixing")})
    class_signals = MappingProxyType({key: False for key in (
        "method_count", "public_method_count", "mutable_field_count",
        "cohesion_components", "domain_mixing", "lifecycle_mixing")})
    file_signals = MappingProxyType({key: False for key in (
        "definition_count", "class_count", "subsystem_import_count",
        "definition_dependency_components", "domain_mixing", "lifecycle_mixing")})
    function = candidate_policy.FunctionMetrics(
        16, 1, 0, 0, 0, (), (), (), (), (), ("z.py::f::call:0001",),
        function_signals, 0, ("cyclomatic_gt_15",), True)
    one_hop = candidate_policy.OneHopMetrics(
        "a.py::root", ("a.py::root", *(f"a.py::_h{i}" for i in range(13))), 13, 1, 0,
        0, 0, 0, (), (), (), one_hop_signals, 0,
        ("helper_count_gt_12",), True)
    klass = candidate_policy.ClassMetrics(
        25, 13, (), 0, 1, (), (), (), (), class_signals, 0,
        ("method_count_gt_24",), True)
    file_metric = candidate_policy.FileMetrics(
        51, 0, (), 0, 1, (), (), (), file_signals, 0,
        ("definition_count_gt_50",), True)
    report = candidate_policy.ArchitectureReport(
        candidate_policy._metric_map(
            {"a.py::later": function, "a.py::earlier": function},
            {"a.py::later": 2, "a.py::earlier": 0}),
        candidate_policy._metric_map(
            {"a.py::root::@one_hop": one_hop}, {"a.py::root::@one_hop": 1}),
        candidate_policy._metric_map(
            {"a.py::C": klass}, {"a.py::C": 1}),
        candidate_policy._metric_map(
            {"a.py::@file": file_metric}, {"a.py::@file": -1}),
        ("z.py::f::call:0001",), "a" * 64, "b" * 64, "c" * 64,
        "d" * 64, "e" * 64, "task-12c-test", "v1")
    verdict = manifest_verifier.ManifestVerdict(
        True, (), ("a.py::root::@one_hop",))

    value = json.loads(render_report(report, verdict))
    rendered = render_report(report, verdict)
    assert rendered == json.dumps(
        json.loads(rendered), ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":")) + "\n"

    assert [(row["kind"], row["identity"]) for row in value["candidates"]] == [
        ("file", "a.py::@file"),
        ("function", "a.py::earlier"),
        ("one_hop", "a.py::root::@one_hop"),
        ("class", "a.py::C"),
        ("function", "a.py::later"),
    ]
    assert value["candidates"][2]["metrics"]["members"][0] == "a.py::root"
    assert len(value["candidates"][2]["metrics"]["members"]) == 14
    assert value["candidates"][3]["metrics"]["method_count"] == 25
    assert value["candidates"][4]["metrics"]["hard_triggers"] == [
        "cyclomatic_gt_15"]
    assert value["unresolved_callsites"] == ["z.py::f::call:0001"]


def test_domain_lifecycle_entity_digest_binds_all_exact_owner_evidence(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/rich_semantic_payload.py"
    owner = f"{path}::owner"
    files = {
        path: _resolver_source(
            """
            def target(value):
                return value
            class Worker:
                def run(self):
                    pass
            @target
            def owner():
                import os as operating
                alias = target
                worker = Worker()
                alias(worker.run())
                operating.getcwd()
                class Nested:
                    pass
            """
        )
    }
    index = _fixture_index(files, tmp_path)
    primitives = _primitive_table(
        index,
        (_primitive_entity_row("os.getcwd", ("filesystem-read",)),),
    )
    resolutions = resolve_calls(index, (), primitives)
    lifecycle = _lifecycle_table()
    digest_inputs = _semantic_digest_inputs()
    semantics = propagate_semantics(
        index,
        resolutions,
        primitives,
        lifecycle,
        digest_inputs=digest_inputs,
    )
    primitive_digest = _canonical_sha256(primitives)
    lifecycle_digest = _canonical_sha256(lifecycle)
    rule_inputs = {
        "allowlist_digest": digest_inputs.allowlist_digest,
        "primitive_digest": primitive_digest,
        "lifecycle_digest": lifecycle_digest,
        "schema_digest": digest_inputs.schema_digest,
        "threshold_digest": digest_inputs.threshold_digest,
        "analyzer_version": digest_inputs.analyzer_version,
        "rule_version": digest_inputs.rule_version,
    }
    imports = [
        {
            "identity": record.identity,
            "owner": record.owner,
            "kind": record.kind,
            "module": record.module,
            "level": record.level,
            "aliases": [dict(alias) for alias in record.aliases],
            "targets": list(record.targets),
            "span_sha256": record.span_sha256,
            "import_semantic_sha256": record.import_semantic_sha256,
        }
        for record in index.imports.values()
        if record.owner == owner
    ]
    aliases = [
        {"binding": binding, "target": target}
        for binding, target in sorted(resolutions.aliases.items())
        if binding.rsplit("::", 1)[0] == owner
    ]
    receivers = [
        {"binding": binding, "target": target}
        for binding, target in sorted(resolutions.receivers.items())
        if binding.rsplit("::", 1)[0] == owner
    ]
    calls = [
        {"callsite": record.callsite, "target": record.target}
        for record in resolutions.calls.values()
        if record.callsite.rsplit("::call:", 1)[0] == owner
    ]
    dependencies = [
        {
            "reference": record.reference,
            "owner": record.owner,
            "kind": record.kind,
            "target": record.target,
        }
        for record in resolutions.dependencies.values()
        if record.owner == owner
    ]
    payload = {
        "schema": "lockstep.architecture-entity-semantics/v1",
        "identity": owner,
        "source_sha256": index.entities[owner].span.sha256,
        "imports": imports,
        "aliases": aliases,
        "receivers": receivers,
        "calls": calls,
        "dependencies": dependencies,
        "containment": [
            identity
            for identity, entity in index.entities.items()
            if entity.parent == owner
        ],
        "direct_domains": ["filesystem-read"],
        "propagated_domains": ["filesystem-read"],
        "direct_transitions": [],
        "propagated_transitions": [],
        "propagated_lifecycle_clusters": [],
        "rule_inputs": rule_inputs,
    }
    assert semantics.entities[owner].semantic_dependency_sha256 == (
        _canonical_sha256(payload)
    )
    file_payload = {
        "schema": "lockstep.architecture-file-semantics/v1",
        "identity": f"{path}::@file",
        "file_sha256": index.file_sha256[path],
        "definitions": [
            {
                "identity": identity,
                "semantic_dependency_sha256": (
                    semantics.entities[identity].semantic_dependency_sha256
                ),
            }
            for identity in index.entities
        ],
        "imports": [
            {
                "identity": record.identity,
                "import_semantic_sha256": record.import_semantic_sha256,
            }
            for record in index.imports.values()
        ],
        "aliases": [],
        "receivers": [],
        "calls": [],
        "dependencies": [],
        "propagated_domains": ["filesystem-read"],
        "propagated_transitions": [],
        "propagated_lifecycle_clusters": [],
        "rule_inputs": rule_inputs,
    }
    assert semantics.files[f"{path}::@file"].semantic_dependency_sha256 == (
        _canonical_sha256(file_payload)
    )
