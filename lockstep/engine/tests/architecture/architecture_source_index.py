"""Immutable, byte-exact source inventory for the architecture ratchet."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class SourceSpan:
    start_line: int
    end_line: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Entity:
    identity: str
    parent: str
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    source: bytes
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class ImportRecord:
    identity: str
    owner: str
    kind: str
    module: str | None
    level: int
    aliases: tuple[Mapping[str, str | None], ...]
    targets: tuple[str, ...]
    span_sha256: str
    import_semantic_sha256: str


@dataclass(frozen=True, slots=True)
class SourceIndex:
    files: Mapping[str, bytes]
    file_sha256: Mapping[str, str]
    entities: Mapping[str, Entity]
    imports: Mapping[str, ImportRecord]
    lambda_owners: Mapping[ast.Lambda, str]
    class_lambda_evidence: Mapping[str, tuple[str, ...]]


def _frozen(values: Mapping) -> Mapping:
    return MappingProxyType(dict(values))


def _path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    normalized = path.as_posix()
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"tracked path must be relative: {value!r}")
    if not normalized.startswith("src/lockstep/") or not normalized.endswith(".py"):
        raise ValueError(f"tracked path is outside src/lockstep Python sources: {value!r}")
    return normalized


def _span(node: ast.AST, source: bytes) -> SourceSpan:
    decorators = getattr(node, "decorator_list", ())
    start = min((item.lineno for item in decorators), default=node.lineno)
    end = node.end_lineno
    exact = b"".join(source.splitlines(keepends=True)[start - 1 : end])
    return SourceSpan(start, end, hashlib.sha256(exact).hexdigest())


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, source: bytes) -> None:
        self.path = path
        self.source = source
        self.file_owner = f"{path}::@file"
        self.stack: list[tuple[str, ast.AST]] = []
        self.entities: dict[str, Entity] = {}
        self.imports: dict[str, ImportRecord] = {}
        self.lambda_owners: dict[ast.Lambda, str] = {}
        self.class_evidence: dict[str, list[str]] = {}
        self.import_ordinal = 0

    def _visit_named(self, node: ast.AST) -> None:
        names = [item.name for _identity, item in self.stack]
        identity = f"{self.path}::{'.'.join((*names, node.name))}"
        if identity in self.entities:
            raise ValueError(f"duplicate stable identity: {identity}")
        parent = self.stack[-1][0] if self.stack else self.file_owner
        self.entities[identity] = Entity(
            identity, parent, node, self.source, _span(node, self.source)
        )
        self.stack.append((identity, node))
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_named
    visit_AsyncFunctionDef = _visit_named
    visit_ClassDef = _visit_named

    def _visit_import(self, node: ast.Import | ast.ImportFrom) -> None:
        self.import_ordinal += 1
        if self.import_ordinal > 9_999:
            raise ValueError("import ordinal exceeds 9,999")
        identity = f"{self.path}::import:{self.import_ordinal:04d}"
        owner = self.stack[-1][0] if self.stack else self.file_owner
        is_from = isinstance(node, ast.ImportFrom)
        module = node.module if is_from else None
        level = node.level if is_from else 0
        aliases = tuple(
            MappingProxyType({"name": alias.name, "asname": alias.asname})
            for alias in node.names
        )
        if is_from:
            base = "." * level + (module or "")
            joiner = "." if module else ""
            targets = tuple(f"{base}{joiner}{alias.name}" for alias in node.names)
        else:
            targets = tuple(alias.name for alias in node.names)
        span = _span(node, self.source).sha256
        payload = {
            "identity": identity,
            "owner": owner,
            "kind": "from" if is_from else "import",
            "module": module,
            "level": level,
            "aliases": [dict(alias) for alias in aliases],
            "targets": list(targets),
            "span_sha256": span,
        }
        self.imports[identity] = ImportRecord(
            identity,
            owner,
            payload["kind"],
            module,
            level,
            aliases,
            targets,
            span,
            _canonical_sha256(payload),
        )

    visit_Import = _visit_import
    visit_ImportFrom = _visit_import

    def visit_Lambda(self, node: ast.Lambda) -> None:
        owner = self.stack[-1][0] if self.stack else self.file_owner
        self.lambda_owners[node] = owner
        if self.stack and isinstance(self.stack[-1][1], ast.ClassDef):
            evidence = self.class_evidence.setdefault(owner, [])
            if len(evidence) >= 9_999:
                raise ValueError("class lambda ordinal exceeds 9,999")
            evidence.append(f"@lambda:{len(evidence) + 1:04d}")
        self.generic_visit(node)


def build_source_index(
    repo_root: Path,
    tracked_paths: Sequence[str],
    files: Mapping[str, bytes] | None = None,
) -> SourceIndex:
    """Build the deterministic index from an exact tracked-path snapshot."""

    paths = tuple(sorted(_path(path) for path in tracked_paths))
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate normalized tracked path")
    supplied = None if files is None else {_path(path): data for path, data in files.items()}
    captured: dict[str, bytes] = {}
    digests: dict[str, str] = {}
    entities: dict[str, Entity] = {}
    imports: dict[str, ImportRecord] = {}
    lambda_owners: dict[ast.Lambda, str] = {}
    class_evidence: dict[str, tuple[str, ...]] = {}
    for path in paths:
        source = (Path(repo_root) / path).read_bytes() if supplied is None else supplied[path]
        if not isinstance(source, bytes):
            raise TypeError(f"source bytes required for {path}")
        captured[path] = source
        digests[path] = hashlib.sha256(source).hexdigest()
        scanner = _Scanner(path, source)
        scanner.visit(ast.parse(source.decode("utf-8"), filename=path))
        entities.update(scanner.entities)
        imports.update(scanner.imports)
        lambda_owners.update(scanner.lambda_owners)
        class_evidence.update(
            (owner, tuple(evidence))
            for owner, evidence in scanner.class_evidence.items()
        )
    return SourceIndex(
        _frozen(captured),
        _frozen(digests),
        _frozen(entities),
        _frozen(imports),
        _frozen(lambda_owners),
        _frozen(class_evidence),
    )
