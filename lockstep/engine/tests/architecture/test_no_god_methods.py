"""Structural guardrail for methods confirmed as mixed-responsibility gods."""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
import hashlib
import json
import operator
from pathlib import Path
import re
import subprocess
import textwrap
from types import MappingProxyType

import pytest

import architecture_call_resolver as call_resolver
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
    callsite = f"{path}::owner::call:0001"
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

    assert {field.name for field in fields(result)} == {
        "calls",
        "aliases",
        "receivers",
        "dependencies",
    }
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
