"""Structural guardrail for methods confirmed as mixed-responsibility gods."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
import hashlib
import json
import operator
from pathlib import Path
import subprocess
import textwrap
from types import MappingProxyType
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
        metric.line_count,
        metric.cyclomatic,
        metric.cognitive,
        metric.max_nesting,
        metric.legacy_syntactic_fanout,
    ) == (2, 1, 0, 0, 1)


def test_legacy_metrics_characterize_current_complexity_length_and_pruned_fanout() -> None:
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
        "line_count",
        "cyclomatic",
        "cognitive",
        "max_nesting",
        "legacy_syntactic_fanout",
    )
    assert {
        identity: tuple(getattr(metric, name) for name in metric_fields)
        for identity, metric in metrics.items()
    } == {
        f"{path}::parent": (24, 7, 10, 3, 8),
        f"{path}::parent.duplicate": (5, 3, 3, 2, 4),
        f"{path}::parent.Nested.duplicate": (3, 2, 1, 1, 2),
        f"{path}::branch_forms": (18, 11, 14, 2, 2),
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


def _primitive_callsite_row(callsite: str, semantic_target: str) -> Mapping[str, object]:
    return {
        "selector_kind": "callsite",
        "selector": callsite,
        "semantic_target": semantic_target,
        "domains": ["external-process/provider"],
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
    result = _resolver_fixture(
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


@pytest.mark.parametrize(
    ("case", "source", "owner", "target"),
    (
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
    ),
    ids=lambda value: value if isinstance(value, str) and "\n" not in value else None,
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
    result = _resolver_fixture(
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
        path=path,
    )

    assert _resolver_target(
        result, f"{path}::Child::call:0001"
    ) == "package.decorate"
    assert _resolver_target(
        result, f"{path}::Child.owner::call:0001"
    ) == f"{path}::Base.inherited"


@pytest.mark.parametrize(
    ("case", "source", "owner"),
    (
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
    ),
    ids=lambda value: value if isinstance(value, str) and "\n" not in value else None,
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


_CONDITIONAL_RECEIVER_ASSIGNMENTS = (
    ("if", "if flag:\n    receiver = Worker()"),
    ("for", "for _ in items:\n    receiver = Worker()"),
    ("comprehension", "values = [(receiver := Worker()) for _ in items]"),
    ("while", "while flag:\n    receiver = Worker()\n    break"),
    ("try", "try:\n    receiver = Worker()\nexcept Exception:\n    pass"),
    ("except", "try:\n    pass\nexcept Exception:\n    receiver = Worker()"),
    ("finally", "try:\n    pass\nfinally:\n    receiver = Worker()"),
    ("with", "with manager as receiver:\n    pass"),
    ("match", "match subject:\n    case receiver:\n        pass"),
    ("conditional_expression", "receiver = Worker() if flag else Worker()"),
    ("short_circuit", "receiver = flag and Worker()"),
    (
        "lambda",
        "builder = lambda: (receiver := Worker())\nbuilder()",
    ),
    ("assignment_expression", "if (receiver := Worker()):\n    pass"),
    (
        "mutually_exclusive_branches",
        "if flag:\n    receiver = Worker()\nelse:\n    receiver = Worker()",
    ),
    (
        "exception_target_cleanup",
        "try:\n    pass\nexcept Exception as receiver:\n    pass",
    ),
    ("loop_target", "for receiver in items:\n    pass"),
)


@pytest.mark.parametrize(
    ("case", "assignment"),
    _CONDITIONAL_RECEIVER_ASSIGNMENTS,
    ids=[case for case, _assignment in _CONDITIONAL_RECEIVER_ASSIGNMENTS],
)
def test_resolver_receiver_assignment_is_unconditional_across_every_control_form(
    tmp_path: Path,
    case: str,
    assignment: str,
) -> None:
    path = f"src/lockstep/conditional_{case}.py"
    source = (
        "class Worker:\n"
        "    def run(self):\n"
        "        pass\n"
        "def owner(flag, items, manager, subject):\n"
        f"{textwrap.indent(assignment, '    ')}\n"
        "    receiver.run()\n"
    )
    result = _resolver_fixture(tmp_path, source, path=path)
    receiver_calls = [
        record
        for record in _records_named(result, "UnresolvedCall")
        if "Attribute(value=Name(id='receiver'" in record.ast_dump
        and "attr='run'" in record.ast_dump
    ]
    assert len(receiver_calls) == 1


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


@pytest.mark.parametrize(
    ("receiver", "decorator", "parameter"),
    (
        ("self", "", "self"),
        ("cls", "@classmethod\n    ", "cls"),
        ("super()", "", "self"),
    ),
    ids=("self", "cls", "super"),
)
def test_resolver_receiver_rejects_ambiguous_inheritance(
    tmp_path: Path,
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


@pytest.mark.parametrize(
    ("case", "extra"),
    (
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
    ),
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


@pytest.mark.parametrize(
    ("case", "import_line", "annotation"),
    (
        ("name", "from lockstep.dependency import Dependency", "Dependency"),
        ("attribute", "import lockstep.dependency as dep", "dep.Dependency"),
    ),
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


@pytest.mark.parametrize(
    ("case", "expression"),
    (
        ("unknown_name", "unknown()"),
        ("parameter_receiver", "value.method()"),
        ("nested_dynamic_attribute", "module.dynamic.method()"),
        ("reflective_getattr", "getattr(value, 'method')()"),
        ("dunder_reflection", "value.__getattribute__('method')()"),
        ("subscript_callable", "registry['handler']()"),
    ),
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


def test_resolver_callsite_primitive_is_an_exact_terminal_override(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/callsite_primitive.py"
    callsite = f"{path}::owner::call:0001"
    source = """
        def owner(callback):
            callback()
    """
    exact = _resolver_fixture(
        tmp_path,
        source,
        path=path,
        primitives=(_primitive_callsite_row(callsite, "reviewed.callback"),),
    )
    assert _resolver_target(exact, callsite) == "reviewed.callback"

    for wrong_row in (
        _primitive_callsite_row(f"{path}::owner::call:0002", "reviewed.callback"),
        {
            **_primitive_callsite_row(callsite, "reviewed.callback"),
            "selector_kind": "entity",
        },
    ):
        unresolved = _resolver_fixture(
            tmp_path,
            source,
            path=path,
            primitives=(wrong_row,),
        )
        _assert_unresolved_call(unresolved, callsite)


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

    with pytest.raises(ValueError, match="selector|disjoint|callsite"):
        _resolver_fixture(
            tmp_path,
            """
            def owner(callback):
                callback()
            """,
            path=path,
            primitives=rows,
        )


def test_resolver_callsite_primitive_is_invalidated_by_source_ordinal_change(
    tmp_path: Path,
) -> None:
    path = "src/lockstep/ordinal_invalidation.py"
    stale_callsite = f"{path}::owner::call:0002"
    row = _primitive_callsite_row(stale_callsite, "reviewed.callback")
    before = _resolver_fixture(
        tmp_path,
        """
        def owner(callback):
            other()
            callback()
        """,
        path=path,
        primitives=(row,),
    )
    assert _resolver_target(before, stale_callsite) == "reviewed.callback"

    after = _resolver_fixture(
        tmp_path,
        """
        def owner(callback):
            callback()
        """,
        path=path,
        primitives=(row,),
    )
    _assert_unresolved_call(after, f"{path}::owner::call:0001")
    assert stale_callsite not in _resolver_calls(after)


def test_resolver_callsite_primitive_cannot_override_new_static_semantics(
    tmp_path: Path,
) -> None:
    """Catches a stale callsite row overriding a newly exact lexical target."""

    path = "src/lockstep/semantic_invalidation.py"
    callsite = f"{path}::owner::call:0001"
    row = _primitive_callsite_row(callsite, "reviewed.callback")
    result = _resolver_fixture(
        tmp_path,
        """
        def target():
            pass
        def owner():
            target()
        """,
        path=path,
        primitives=(row,),
    )

    assert _resolver_target(result, callsite) == f"{path}::target"


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
    assert {field.name for field in fields(result)} == {"calls", "aliases", "receivers"}
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
