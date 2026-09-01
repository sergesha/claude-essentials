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
    lambda_owners: Mapping[tuple[str, int, int, int, int], str]
    class_lambda_evidence: Mapping[str, tuple[str, ...]]


def _frozen(values: Mapping) -> Mapping:
    return MappingProxyType(dict(values))


def _path(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("tracked path must be a string")
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
        self.lambda_owners: dict[tuple[str, int, int, int, int], str] = {}
        self.class_evidence: dict[str, list[str]] = {}
        self.import_ordinal = 0

    def _visit_named(self, node: ast.AST) -> None:
        names = [item.name for _identity, item in self.stack]
        identity = f"{self.path}::{'.'.join((*names, node.name))}"
        if identity in self.entities:
            raise ValueError(f"duplicate stable identity: {identity}")
        parent = self.stack[-1][0] if self.stack else self.file_owner
        self.entities[identity] = Entity(identity, parent, self.source, _span(node, self.source))
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
        evidence = (
            self.path,
            node.lineno,
            node.col_offset,
            node.end_lineno,
            node.end_col_offset,
        )
        self.lambda_owners[evidence] = owner
        if self.stack and isinstance(self.stack[-1][1], ast.ClassDef):
            evidence = self.class_evidence.setdefault(owner, [])
            if len(evidence) >= 9_999:
                raise ValueError("class lambda ordinal exceeds 9,999")
            evidence.append(f"@lambda:{len(evidence) + 1:04d}")
        self.generic_visit(node)


def _supplied_snapshot(paths, files):
    supplied = None
    if files is not None:
        supplied = {}
        for raw_path, source in files.items():
            path = _path(raw_path)
            if not isinstance(source, bytes):
                raise TypeError(f"source bytes required for {path}")
            if path in supplied:
                raise ValueError(f"duplicate normalized supplied path: {path}")
            supplied[path] = source
        missing = tuple(sorted(set(paths) - set(supplied)))
        extra = tuple(sorted(set(supplied) - set(paths)))
        if missing and extra:
            raise ValueError(
                f"supplied files mismatch: missing {', '.join(missing)}; "
                f"extra {', '.join(extra)}"
            )
        if missing:
            raise ValueError(f"supplied files missing tracked paths: {', '.join(missing)}")
        if extra:
            raise ValueError(f"supplied files contain untracked paths: {', '.join(extra)}")
    return supplied


def _scan_source(path, source):
    if not isinstance(source, bytes):
        raise TypeError(f"source bytes required for {path}")
    scanner = _Scanner(path, source)
    scanner.visit(ast.parse(source.decode("utf-8"), filename=path))
    evidence = {owner: tuple(rows) for owner, rows in scanner.class_evidence.items()}
    return scanner, evidence


def build_source_index(
    repo_root: Path,
    tracked_paths: Sequence[str],
    files: Mapping[str, bytes] | None = None,
) -> SourceIndex:
    """Build the deterministic index from an exact tracked-path snapshot."""

    paths = tuple(sorted(_path(path) for path in tracked_paths))
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate normalized tracked path")
    supplied = _supplied_snapshot(paths, files)
    captured: dict[str, bytes] = {}
    digests: dict[str, str] = {}
    entities: dict[str, Entity] = {}
    imports: dict[str, ImportRecord] = {}
    lambda_owners: dict[tuple[str, int, int, int, int], str] = {}
    class_evidence: dict[str, tuple[str, ...]] = {}
    for path in paths:
        source = (Path(repo_root) / path).read_bytes() if supplied is None else supplied[path]
        captured[path] = source
        digests[path] = hashlib.sha256(source).hexdigest()
        scanner, evidence = _scan_source(path, source)
        entities.update(scanner.entities)
        imports.update(scanner.imports)
        lambda_owners.update(scanner.lambda_owners)
        class_evidence.update(evidence)
    return SourceIndex(
        _frozen(captured),
        _frozen(digests),
        _frozen(entities),
        _frozen(imports),
        _frozen(lambda_owners),
        _frozen(class_evidence),
    )
