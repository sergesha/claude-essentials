"""Closed, conservative call resolution for the architecture analyzer."""

from __future__ import annotations

import ast
import builtins
from collections import defaultdict, namedtuple
from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Sequence

from architecture_source_index import SourceIndex


@dataclass(frozen=True, slots=True)
class ResolvedCall:
    callsite: str
    target: str


@dataclass(frozen=True, slots=True)
class UnresolvedCall:
    callsite: str
    line: int
    column: int
    ast_dump: str


@dataclass(frozen=True, slots=True)
class ResolvedDependency:
    reference: str
    owner: str
    kind: str
    target: str


@dataclass(frozen=True, slots=True)
class UnresolvedDependency:
    reference: str
    owner: str
    kind: str
    line: int
    column: int
    ast_dump: str


@dataclass(frozen=True, slots=True)
class PositionalLiteralEvidence:
    index: int
    type: str
    value: None | bool | int | str


@dataclass(frozen=True, slots=True)
class KeywordLiteralEvidence:
    name: str
    type: str
    value: None | bool | int | str


@dataclass(frozen=True, slots=True)
class CallsiteEvidence:
    callsite: str
    owner: str
    line: int
    column: int
    positional: tuple[PositionalLiteralEvidence, ...]
    keywords: tuple[KeywordLiteralEvidence, ...]


@dataclass(frozen=True, slots=True)
class ResolutionIndex:
    calls: Mapping[str, object]
    aliases: Mapping[str, str]
    receivers: Mapping[str, str]
    dependencies: Mapping[str, object]
    reference_source_sha256: str
    call_evidence: Mapping[str, CallsiteEvidence]

    def validate_primitives(
        self, index: SourceIndex, value: object
    ) -> tuple[Mapping[str, object], ...]:
        model = _Model(index)
        _require(
            self.reference_source_sha256 == model.reference_source_sha256,
            "resolution source population mismatch",
        )
        return _read_primitives(model, value)


@dataclass(slots=True)
class _Binding:
    kind: str
    value: object
    node: ast.AST
    conditional: bool = False


@dataclass(slots=True)
class _Scope:
    kind: str
    parent: _Scope | None
    identity: str
    node: ast.AST
    bindings: dict[str, list[_Binding]] = field(default_factory=lambda: defaultdict(list))
    params: set[str] = field(default_factory=set)
    globals: dict[str, list[ast.Global]] = field(default_factory=lambda: defaultdict(list))
    nonlocals: dict[str, list[ast.Nonlocal]] = field(default_factory=lambda: defaultdict(list))
    loads: dict[str, list[ast.Name]] = field(default_factory=lambda: defaultdict(list))


_ClassInfo = namedtuple("_ClassInfo", "scope methods bases")


_NAMED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

_CONDITIONAL = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.TryStar, ast.With,
                ast.AsyncWith, ast.Match, ast.comprehension, ast.IfExp, ast.BoolOp, ast.Lambda, ast.NamedExpr)

_EFFECT_DOMAINS = ("decode/validate", "planning/transformation", "filesystem-read",
                   "filesystem-write", "durable-state", "synchronization",
                   "external-process/provider", "authority/commitment", "lifecycle-control",
                   "projection/output")


class _Model:
    """Private AST model reparsed from the exact indexed source bytes."""

    def __init__(self, index: SourceIndex):
        self.index = index
        self.scopes: list[_Scope] = []
        self.node_scope: dict[int, _Scope] = {}
        self.parents: dict[int, ast.AST] = {}
        self.conditional: set[int] = set()
        self.calls: dict[str, list[ast.Call]] = defaultdict(list)
        self.classes: dict[str, _ClassInfo] = {}
        self.named_scopes: dict[str, _Scope] = {}
        self.import_modules: set[str] = set()
        self.modules = {path.removeprefix("src/").removesuffix(".py").replace("/", ".")
                        .removesuffix(".__init__"): path for path in index.files}
        population = [
            {"path": path, "source_sha256": index.file_sha256[path]}
            for path in sorted(index.files)
        ]
        encoded = json.dumps(
            population,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.reference_source_sha256 = hashlib.sha256(encoded).hexdigest()
        for path in sorted(index.files):
            tree = ast.parse(index.files[path], filename=path)
            module = _Scope("module", None, f"{path}::@file", tree)
            self.scopes.append(module)
            for node in tree.body:
                self._visit(node, module, module.identity, False)
        for scope in self.scopes:
            self._collect_scope_facts(scope)

    def _visit(self, node: ast.AST, scope: _Scope, owner: str, conditional: bool) -> None:
        self.node_scope[id(node)] = scope
        self.conditional.update((id(node),) * conditional)
        if isinstance(node, _NAMED):
            self._register_named(node, scope, conditional)
            return
        if isinstance(node, ast.Lambda):
            child = _Scope("lambda", scope, owner, node)
            self.scopes.append(child)
            self.node_scope[id(node.args)] = scope
            self.conditional.update((id(node.args),) * conditional)
            arguments = (
                *node.args.posonlyargs,
                *node.args.args,
                *filter(None, (node.args.vararg,)),
                *node.args.kwonlyargs,
                *filter(None, (node.args.kwarg,)),
            )
            for arg in arguments:
                child.params.add(arg.arg)
                if arg.annotation is not None:
                    self._child(arg.annotation, scope, owner, conditional, arg)
            for default in (*node.args.kw_defaults, *node.args.defaults):
                if default is not None:
                    self._child(default, scope, owner, conditional, node.args)
            self._child(node.body, child, owner, True, node)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            child = _Scope("comprehension", scope, owner, node)
            self.scopes.append(child)
            generators = node.generators
            element_nodes = ((node.key, node.value) if isinstance(node, ast.DictComp)
                             else (node.elt,))
            for element in element_nodes:
                self._child(element, child, owner, True, node)
            for position, generator in enumerate(generators):
                self.node_scope[id(generator)] = child
                self.conditional.add(id(generator))
                iter_scope = scope if position == 0 else child
                self._child(generator.target, child, owner, True, generator)
                self._child(generator.iter, iter_scope, owner, conditional, generator)
                for condition in generator.ifs:
                    self._child(condition, child, owner, True, generator)
            return
        if isinstance(node, ast.Call):
            self.calls[owner].append(node)
        child_conditional = conditional or isinstance(node, _CONDITIONAL)
        for child in ast.iter_child_nodes(node):
            self._child(child, scope, owner, child_conditional, node)

    def _child(
        self,
        child: object,
        scope: _Scope,
        owner: str,
        conditional: bool,
        parent: ast.AST,
    ) -> None:
        if isinstance(child, ast.AST):
            if child is getattr(parent, "target", None) and isinstance(parent, ast.NamedExpr):
                while scope.kind == "comprehension":
                    assert scope.parent is not None
                    scope = scope.parent
            self.parents[id(child)] = parent
            self._visit(child, scope, owner, conditional)
        elif isinstance(child, list):
            for item in child:
                self._child(item, scope, owner, conditional, parent)

    def _register_named(self, node: ast.AST, parent: _Scope, conditional: bool) -> None:
        assert isinstance(node, _NAMED)
        path, _separator, suffix = parent.identity.rpartition("::")
        qualified = node.name if suffix == "@file" else f"{suffix}.{node.name}"
        identity = f"{path}::{qualified}"
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        child = _Scope(kind, parent, identity, node)
        self.scopes.append(child)
        self.named_scopes[identity] = child
        parent.bindings[node.name].append(_Binding(kind, identity, node, conditional))
        if isinstance(node, ast.ClassDef):
            self.classes[identity] = _ClassInfo(child, {}, list(node.bases))
            for decorator in node.decorator_list:
                self._child(decorator, parent, identity, conditional, node)
            for base in node.bases:
                self._child(base, parent, identity, conditional, node)
            for keyword in node.keywords:
                self._child(keyword, parent, identity, conditional, node)
            for statement in node.body:
                self._visit(statement, child, identity, conditional)
            for statement in node.body:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method = f"{identity}.{statement.name}"
                    self.classes[identity].methods[statement.name] = method
            return
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list:
            self._child(decorator, parent, identity, conditional, node)
        # CPython's arguments field order is the frozen owner-preorder contract.
        self._child(node.args, parent, identity, conditional, node)
        if node.returns is not None:
            self._child(node.returns, parent, identity, conditional, node)
        arguments = (
            *node.args.posonlyargs,
            *node.args.args,
            *filter(None, (node.args.vararg,)),
            *node.args.kwonlyargs,
            *filter(None, (node.args.kwarg,)),
        )
        for arg in arguments:
            child.params.add(arg.arg)
        for statement in node.body:
            self._visit(statement, child, identity, conditional)

    def _collect_scope_facts(self, scope: _Scope) -> None:
        for node in ast.walk(scope.node):
            if self.node_scope.get(id(node)) is not scope:
                continue
            conditional = id(node) in self.conditional
            if isinstance(node, ast.Name):
                self._collect_name_fact(scope, node, conditional)
            elif isinstance(node, (ast.MatchAs, ast.MatchStar, ast.MatchMapping)):
                name = node.rest if isinstance(node, ast.MatchMapping) else node.name
                if name is not None:
                    scope.bindings[name].append(_Binding("store", None, node, True))
            else:
                self._collect_statement_fact(scope, node, conditional)

    def _collect_name_fact(
        self, scope: _Scope, node: ast.Name, conditional: bool
    ) -> None:
        if isinstance(node.ctx, ast.Load):
            scope.loads[node.id].append(node)
            return
        kind = "del" if isinstance(node.ctx, ast.Del) else "store"
        if isinstance(node.ctx, ast.Store) and isinstance(
            self.parents.get(id(node)), ast.AugAssign
        ):
            kind = "aug"
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            scope.bindings[node.id].append(_Binding(kind, None, node, conditional))

    def _collect_statement_fact(
        self, scope: _Scope, node: ast.AST, conditional: bool
    ) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                self.import_modules.add(alias.name)
                name = alias.asname or alias.name.split(".")[0]
                value = alias.name if alias.asname else alias.name.split(".")[0]
                scope.bindings[name].append(_Binding("import", value, node, conditional))
            return
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                return
            path = scope.identity.rpartition("::")[0]
            package = path.removeprefix("src/").removesuffix(".py").split("/")[:-1]
            keep = len(package) - max(0, node.level - 1)
            parts = package[: max(0, keep)] if node.level else []
            parts.extend(node.module.split(".") if node.module else ())
            for alias in node.names:
                name = alias.asname or alias.name
                imported = ".".join((*parts, alias.name))
                scope.bindings[name].append(_Binding("import", imported, node, conditional))
            return
        destination = scope.globals if isinstance(node, ast.Global) else scope.nonlocals
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                destination[name].append(node)
        elif isinstance(node, ast.ExceptHandler) and isinstance(node.name, str):
            scope.bindings[node.name].extend(
                (_Binding("store", None, node, True), _Binding("del", None, node, True))
            )

    def enclosing_class(self, scope: _Scope) -> _ClassInfo | None:
        current = scope.parent
        while current is not None:
            if current.kind == "class":
                return self.classes[current.identity]
            current = current.parent
        return None
_Target = namedtuple("_Target", "label kind")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _read_allowlist(value: object) -> frozenset[str]:
    if isinstance(value, Mapping):
        _require(set(value) == {"schema_version", "targets"}, "invalid effect-free allowlist")
        _require(type(value["schema_version"]) is int and value["schema_version"] == 1,
                 "invalid effect-free allowlist schema_version")
        _require(isinstance(value["targets"], list), "effect-free allowlist targets must be an array")
        value = value["targets"]
    _require(not isinstance(value, (str, bytes)) and isinstance(value, (set, frozenset, tuple, list)),
             "invalid effect-free allowlist")
    if not all(isinstance(item, str) and item for item in value):
        if any(isinstance(item, str) and not item for item in value):
            raise ValueError("effect-free allowlist target must be non-empty")
        raise ValueError("invalid effect-free target")
    seen: set[str] = set()
    for target in value:
        if target in seen:
            raise ValueError(f"duplicate effect-free allowlist target: {target}")
        seen.add(target)
    return frozenset(value)


def _read_primitives(model: _Model, value: object) -> tuple[Mapping[str, object], ...]:
    envelope: Mapping[str, object] | None = value if isinstance(value, Mapping) else None
    if envelope is not None:
        expected_keys = {"schema_version", "reference_source_sha256", "callsite_evidence", "rows"}
        _require(set(envelope) == expected_keys, "invalid effect primitive table")
        _require(type(envelope["schema_version"]) is int and envelope["schema_version"] == 1,
                 "invalid effect primitive schema_version")
        value = envelope["rows"]
        _require(isinstance(value, list), "effect primitive rows must be an array")
    _require(not isinstance(value, (str, bytes)) and isinstance(value, (tuple, list)),
             "invalid effect primitive table")
    rows = tuple(_primitive_row(row) for row in value)
    bindings = [(row["selector_kind"], row["selector"]) for row in rows]
    duplicate = next((item for item in bindings if bindings.count(item) > 1), None)
    if duplicate is not None:
        raise ValueError(f"duplicate primitive binding: {duplicate[1]}")
    callsites = {row["selector"] for row in rows if row["selector_kind"] == "callsite"}
    entities = {row["selector"] for row in rows if row["selector_kind"] == "entity"}
    _require(not callsites & entities, "primitive selector spaces overlap")
    mismatch = next(
        (
            row
            for row in rows
            if row["selector_kind"] == "entity"
            and row["selector"] in model.index.entities
            and row["semantic_target"] != row["selector"]
        ),
        None,
    )
    if mismatch is not None:
        raise ValueError(
            f"internal entity primitive target mismatch: {mismatch['selector']}"
        )
    if envelope is not None:
        _validate_primitive_evidence(model, envelope, rows)
    return rows


def _primitive_row(row: object) -> Mapping[str, object]:
    keys = {"selector_kind", "selector", "semantic_target", "domains"}
    _require(isinstance(row, Mapping) and set(row) == keys, "invalid effect primitive row")
    _require(row["selector_kind"] in {"callsite", "entity"}, "invalid primitive selector kind")
    for key in ("selector", "semantic_target"):
        if not isinstance(row[key], str):
            raise ValueError("invalid effect primitive selector")
        if not row[key]:
            suffix = "selector" if key == "selector" else "semantic_target"
            raise ValueError(f"effect primitive {suffix} must be non-empty")
    domains = row["domains"]
    _require(not isinstance(domains, (str, bytes)) and isinstance(domains, list),
             "invalid primitive domains: string_not_array")
    _require(bool(domains), "invalid primitive domains: empty")
    _require(all(isinstance(domain, str) and domain in _EFFECT_DOMAINS for domain in domains),
             "invalid primitive domains: unknown")
    _require(len(set(domains)) == len(domains), "invalid primitive domains: duplicate")
    _require(domains == sorted(domains, key=_EFFECT_DOMAINS.index),
             "invalid primitive domains: noncanonical_order")
    return dict(row)


def _validate_primitive_evidence(model: _Model, envelope: Mapping[str, object],
                                 rows: tuple[Mapping[str, object], ...]) -> None:
    _require(envelope["reference_source_sha256"] == model.reference_source_sha256,
             "reference source evidence mismatch")
    evidence = envelope["callsite_evidence"]
    records_valid = isinstance(evidence, list) and all(
        isinstance(record, Mapping)
        and set(record) == {"selector", "owner_source_sha256", "call_ast_sha256"}
        and all(isinstance(item, str) for item in record.values()) for record in evidence)
    _require(records_valid, "invalid callsite evidence: malformed_record")
    expected = [str(row["selector"]) for row in rows if row["selector_kind"] == "callsite"]
    actual = [str(record["selector"]) for record in evidence]
    _require(len(set(actual)) == len(actual), "invalid callsite evidence: duplicate")
    _require(all(selector in actual for selector in expected), "invalid callsite evidence: missing")
    _require(all(selector in expected for selector in actual), "invalid callsite evidence: orphan")
    _require(actual == expected, "noncanonical callsite evidence order")
    for record in evidence:
        _validate_callsite_evidence(model, record)


def _validate_callsite_evidence(model: _Model, record: Mapping[str, object]) -> None:
    selector = str(record["selector"])
    owner, ordinal_text = selector.rsplit("::call:", 1)
    calls = model.calls.get(owner, ())
    ordinal = int(ordinal_text)
    _require(1 <= ordinal <= len(calls), f"callsite AST evidence mismatch: {selector}")
    call = calls[ordinal - 1]
    path, _separator, qualified = owner.rpartition("::")
    owner_sha256 = (model.index.file_sha256[path] if qualified == "@file"
                    else model.index.entities[owner].span.sha256)
    _require(record["owner_source_sha256"] == owner_sha256,
             "invalid callsite evidence: owner_source_mismatch")
    call_sha256 = hashlib.sha256(ast.dump(call, include_attributes=False).encode("utf-8")).hexdigest()
    if record["call_ast_sha256"] == call_sha256:
        return
    if record["call_ast_sha256"] == "0" * 64:
        raise ValueError("invalid callsite evidence: call_ast_mismatch")
    raise ValueError(f"callsite AST evidence mismatch: {selector}")


_LITERAL_KINDS = {type(None): "null", bool: "bool", int: "int", str: "str"}


def _callsite_evidence(
    callsite: str, owner: str, call: ast.Call
) -> CallsiteEvidence:
    positional = tuple(
        PositionalLiteralEvidence(index, kind, argument.value)
        for index, argument in enumerate(call.args)
        if isinstance(argument, ast.Constant)
        and (kind := _LITERAL_KINDS.get(type(argument.value))) is not None
    )
    keywords = tuple(
        KeywordLiteralEvidence(keyword.arg, kind, keyword.value.value)
        for keyword in call.keywords
        if keyword.arg is not None
        and isinstance(keyword.value, ast.Constant)
        and (kind := _LITERAL_KINDS.get(type(keyword.value.value))) is not None
    )
    return CallsiteEvidence(
        callsite, owner, call.lineno, call.col_offset, positional, keywords
    )


def _symbol_use_is_safe(
    model: _Model, node: ast.Name, *, receiver: bool = False
) -> bool:
    parent = model.parents.get(id(node))
    if receiver:
        if not isinstance(parent, ast.Attribute) or parent.value is not node:
            return False
        grand = model.parents.get(id(parent))
        return isinstance(grand, ast.Call) and grand.func is parent
    while isinstance(parent, ast.Tuple):
        parent = model.parents.get(id(parent))
    if isinstance(parent, ast.Call) and parent.func is node:
        return True
    if isinstance(parent, ast.Attribute) and parent.value is node:
        grand = model.parents.get(id(parent))
        return isinstance(grand, ast.Call) and grand.func is parent
    if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return True
    return isinstance(parent, (ast.keyword, ast.Subscript))


def _is_descendant(scope: _Scope, parent: _Scope) -> bool:
    current = scope.parent
    while current is not None:
        if current is parent:
            return True
        current = current.parent
    return False


class _Resolver:

    def __init__(self, index: SourceIndex, allowlist: object, primitives: object):
        self.model = _Model(index)
        self.allowlist = _read_allowlist(allowlist)
        self.primitives = _read_primitives(self.model, primitives)
        overlap = self.allowlist & {
            row["selector"] for row in self.primitives if row["selector_kind"] == "entity"
        }
        if overlap:
            raise ValueError("effect-free allowlist and entity primitive selectors overlap")
        self.aliases: dict[tuple[int, str], _Target] = {}
        self.receivers: dict[tuple[int, str], str] = {}
        self.field_receivers: dict[tuple[str, str], str] = {}
        self.bases: dict[str, tuple[str, ...] | None] = {}
        for prepare in (self._prepare_symbols, self._prepare_fields):
            prepare()

    def _normalize_target(self, label: str, kind: str) -> _Target | None:
        seen: set[str] = set()
        while label not in seen:
            seen.add(label)
            parts = label.split(".")
            cut = next((cut for cut in range(len(parts), 0, -1)
                        if ".".join(parts[:cut]) in self.model.modules), None)
            if cut is None:
                return _Target(label, kind)
            path = self.model.modules[".".join(parts[:cut])]
            rest = ".".join(parts[cut:])
            if not rest:
                return _Target(label, kind)
            identity = f"{path}::{rest}"
            scope = self.model.named_scopes.get(identity)
            if scope is not None:
                return _Target(identity, scope.kind)
            module_scope = next(scope for scope in self.model.scopes
                                if scope.identity == f"{path}::@file")
            head, *tail = rest.split(".")
            alias = self.aliases.get((id(module_scope), head))
            if alias is not None:
                if not tail:
                    return alias
                label = ".".join((alias.label, *tail))
                continue
            bindings = module_scope.bindings.get(head, ())
            if (len(bindings) != 1 or bindings[0].kind != "import"
                    or bindings[0].conditional):
                return None
            label = ".".join((str(bindings[0].value), *tail))
        return None

    def _declaration_valid(self, scope: _Scope, name: str, load: ast.Name) -> tuple[str, _Scope] | None:
        globals_ = scope.globals.get(name, ())
        nonlocals = scope.nonlocals.get(name, ())
        declarations = (*globals_, *nonlocals)
        if not declarations:
            return ("local", scope)
        if len(declarations) != 1:
            return None
        declaration = declarations[0]
        if (load.lineno, load.col_offset) < (declaration.lineno, declaration.col_offset):
            return None
        if any(binding.kind in {"store", "del", "aug"} for binding in scope.bindings.get(name, ())):
            return None
        if globals_:
            target = scope
            while target.parent is not None:
                target = target.parent
            defined = name in target.params or bool(target.bindings.get(name))
            return ("redirect", target) if defined else None
        target = scope.parent
        while target is not None:
            defined = name in target.params or bool(target.bindings.get(name))
            if target.kind != "class" and defined:
                return "redirect", target
            target = target.parent
        return None

    def _resolve_name(self, scope: _Scope, name: str, load: ast.AST) -> _Target | None:
        original = scope
        current: _Scope | None = scope
        first = True
        while current is not None:
            if first and isinstance(load, ast.Name):
                declaration = self._declaration_valid(current, name, load)
                if declaration is None:
                    return None
                if declaration[0] == "redirect":
                    current = declaration[1]
            first = False
            if name in current.params:
                return None
            bindings = current.bindings.get(name, ())
            if bindings:
                return self._binding_target(current, original, name, load, bindings)
            parent = current.parent
            if (parent is not None and parent.kind == "class" and
                    original.kind in {"function", "lambda", "comprehension"}):
                parent = parent.parent
            current = parent
        builtin_target = f"builtins.{name}"
        if name in dir(builtins) and builtin_target in self.allowlist:
            return _Target(builtin_target, "builtin")
        return None

    def _binding_target(
        self,
        current: _Scope,
        original: _Scope,
        name: str,
        load: ast.AST,
        bindings: Sequence[_Binding],
    ) -> _Target | None:
        binding = bindings[0]
        load_position = getattr(load, "lineno", 0), getattr(load, "col_offset", 0)
        available = current is not original or (
            binding.node.lineno,
            binding.node.col_offset,
        ) < load_position
        alias = self.aliases.get((id(current), name))
        if alias is not None:
            return alias if available else None
        if len(bindings) != 1 or binding.conditional:
            return None
        pending_scope = original
        while pending_scope.kind == "comprehension":
            assert pending_scope.parent is not None
            pending_scope = pending_scope.parent
        pending_identity = binding.value if binding.kind == "class" else None
        if pending_scope.identity == pending_identity:
            return None
        if binding.kind in {"function", "class"} and current is original:
            ancestor = self.model.parents.get(id(load))
            while ancestor is not None and ancestor is not binding.node:
                ancestor = self.model.parents.get(id(ancestor))
            if ancestor is binding.node:
                return None
        if not available:
            return None
        if binding.kind in {"function", "class"}:
            return _Target(str(binding.value), binding.kind)
        if binding.kind == "import":
            return self._normalize_target(str(binding.value), "import")
        return None

    def _resolve_expr(
        self,
        scope: _Scope,
        expression: ast.AST,
        dependency_kind: str | None = None,
        call_records: Mapping[int, object] | None = None,
    ) -> _Target | None:
        if isinstance(expression, ast.Call):
            record = (call_records or {}).get(id(expression))
            return (_Target(record.target, "covered-external-decorator")
                    if isinstance(record, ResolvedCall)
                    and not record.target.startswith("src/") else None)
        if dependency_kind == "base" and isinstance(expression, ast.Subscript):
            origin = self._resolve_expr(scope, expression.value)
            return (origin if origin is not None
                    and not origin.label.startswith("src/") else None)
        if isinstance(expression, ast.Name):
            return self._resolve_name(scope, expression.id, expression)
        if isinstance(expression, ast.Attribute):
            base = self._resolve_expr(scope, expression.value)
            if base is None:
                return None
            if base.label in self.model.classes:
                method = self._lookup_method(base.label, expression.attr)
                return _Target(method, "function") if method else None
            target = self._normalize_target(f"{base.label}.{expression.attr}", "import")
            if target is None:
                return None
            parent = self.model.parents.get(id(expression))
            if (isinstance(parent, ast.Attribute) and parent.value is expression
                    and target.label not in self.model.import_modules):
                return None
            return target
        return None

    def _prepare_symbols(self) -> None:
        entries = tuple(
            (scope, name, bindings)
            for scope in self.model.scopes
            for name, bindings in scope.bindings.items()
        )
        while True:
            updates = tuple(
                (key, target)
                for scope, name, bindings in entries
                if (target := self._alias_candidate(scope, name, bindings)) is not None
                for key in ((id(scope), name),)
            )
            if not updates:
                break
            self.aliases.update(updates)
        for scope, name, bindings in entries:
            receiver = self._receiver_candidate(scope, name, bindings)
            if receiver is not None:
                self.receivers[(id(scope), name)] = receiver
        for identity, info in self.model.classes.items():
            parent = info.scope.parent
            assert parent is not None
            targets = tuple(
                self._resolve_expr(parent, expression) for expression in info.bases
            )
            complete = all(
                getattr(target, "kind", None) == "class"
                and target.label in self.model.classes
                for target in targets
            )
            self.bases[identity] = (
                tuple(target.label for target in targets) if complete else None
            )

    def _alias_candidate(
        self, scope: _Scope, name: str, bindings: Sequence[_Binding]
    ) -> _Target | None:
        key = id(scope), name
        if key in self.aliases or len(bindings) != 1:
            return None
        binding = bindings[0]
        if binding.kind != "store" or binding.conditional:
            return None
        assignment = self.model.parents.get(id(binding.node))
        value = assignment.value if isinstance(
            assignment, (ast.Assign, ast.AnnAssign, ast.NamedExpr)
        ) else None
        if not isinstance(value, (ast.Name, ast.Attribute)):
            return None
        target = self._resolve_expr(scope, value)
        if target is None or target.kind not in {"function", "class", "import"}:
            return None
        loads = [node for node in scope.loads.get(name, ()) if node is not value]
        if any(not _symbol_use_is_safe(self.model, node) for node in loads):
            return None
        if any(
            _is_descendant(child, scope)
            and (
                name in child.bindings
                or name in child.globals
                or name in child.nonlocals
            )
            for child in self.model.scopes
        ):
            return None
        return target

    def _receiver_candidate(
        self, scope: _Scope, name: str, bindings: Sequence[_Binding]
    ) -> str | None:
        if (len(bindings) != 1 or bindings[0].kind != "store"
                or bindings[0].conditional):
            return None
        assignment = self.model.parents.get(id(bindings[0].node))
        value = assignment.value if isinstance(
            assignment, (ast.Assign, ast.AnnAssign, ast.NamedExpr)
        ) else None
        receiver = None
        if isinstance(assignment, ast.AnnAssign) and assignment.value is None:
            annotation = self._resolve_expr(scope, assignment.annotation)
            if getattr(annotation, "kind", None) == "class":
                receiver = annotation.label
        elif isinstance(value, ast.Call):
            target = self._resolve_expr(scope, value.func)
            if getattr(target, "kind", None) == "class":
                receiver = target.label
        if receiver not in self.model.classes:
            return None
        if any(
            not _symbol_use_is_safe(self.model, node, receiver=True)
            for node in scope.loads.get(name, ())
        ):
            return None
        descendants = (
            child for child in self.model.scopes if _is_descendant(child, scope)
        )
        if any(
            name
            in set(child.loads)
            | set(child.bindings)
            | set(child.globals)
            | set(child.nonlocals)
            for child in descendants
        ):
            return None
        return receiver

    def _lookup_method(self, class_identity: str, name: str, *, parents_only: bool = False) -> str | None:
        info = self.model.classes[class_identity]
        if not parents_only and name in info.methods:
            return info.methods[name]
        bases = self.bases.get(class_identity)
        if bases is None:
            return None
        candidates = {
            method
            for base in bases
            if (method := self._lookup_method(base, name)) is not None
        }
        return next(iter(candidates)) if len(candidates) == 1 else None

    def _class_attributes(self, identity: str, field: str | None = None) -> list[ast.Attribute]:
        root = self.model.classes[identity].scope.node
        attributes: list[ast.Attribute] = []
        for node in ast.walk(root):
            if not isinstance(node, ast.Attribute):
                continue
            owner_scope = self.model.node_scope.get(id(node))
            if (owner_scope is None or self.model.enclosing_class(owner_scope)
                    is not self.model.classes[identity]):
                continue
            pair = (
                (node.value.id, node.attr)
                if isinstance(node.value, ast.Name)
                and node.value.id in {"self", "cls"}
                else None
            )
            if pair and (field is None or pair[1] == field):
                attributes.append(node)
        return attributes

    def _injection(self, identity: str, field: str, stores: list[ast.Attribute]) -> str | None:
        info = self.model.classes[identity]
        init_identity = info.methods.get("__init__")
        if init_identity is None or len(stores) != 1:
            return None
        init_scope = self.model.named_scopes[init_identity]
        store = stores[0]
        if self.model.node_scope.get(id(store)) is not init_scope or id(store) in self.model.conditional:
            return None
        assignment = self.model.parents.get(id(store))
        if not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1:
            return None
        if not isinstance(assignment.value, ast.Name):
            return None
        parameter = assignment.value.id
        if parameter not in init_scope.params:
            return None
        function = init_scope.node
        assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        arguments = (
            *function.args.posonlyargs,
            *function.args.args,
            *((function.args.vararg,) if function.args.vararg is not None else ()),
            *function.args.kwonlyargs,
            *((function.args.kwarg,) if function.args.kwarg is not None else ()),
        )
        argument = next((arg for arg in arguments if arg.arg == parameter), None)
        if argument is None or not isinstance(argument.annotation, (ast.Name, ast.Attribute)):
            return None
        parent_scope = init_scope.parent
        assert parent_scope is not None
        annotation = self._resolve_expr(parent_scope, argument.annotation)
        if annotation is None or annotation.kind != "class" or annotation.label not in self.model.classes:
            return None
        escapes = self._injection_escapes(
            identity, field, init_scope, assignment, parameter, store
        )
        return None if escapes else annotation.label

    def _injection_escapes(
        self,
        identity: str,
        field: str,
        init_scope: _Scope,
        assignment: ast.Assign,
        parameter: str,
        store: ast.Attribute,
    ) -> bool:
        scopes = tuple(
            scope
            for scope in self.model.scopes
            if scope is init_scope or _is_descendant(scope, init_scope)
        )
        parameter_escapes = any(
            any(load is not assignment.value for load in scope.loads.get(parameter, ()))
            or (
                bool(scope.bindings.get(parameter))
                if scope is init_scope
                else parameter
                in set(scope.globals) | set(scope.nonlocals) | set(scope.bindings)
            )
            for scope in scopes
        )
        attributes = (
            attribute
            for attribute in self._class_attributes(identity, field)
            if attribute is not store
        )
        field_escapes = any(
            not all(
                (
                    isinstance(attribute.ctx, ast.Load),
                    isinstance(
                        parent := self.model.parents.get(id(attribute)), ast.Attribute
                    ),
                    getattr(parent, "value", None) is attribute,
                    isinstance(
                        grand := self.model.parents.get(id(parent)), ast.Call
                    ),
                    getattr(grand, "func", None) is parent,
                )
            )
            for attribute in attributes
        )
        subclass_mutates = any(
            all(
                (
                    child != identity,
                    self._inherits(child, identity),
                    any(
                        not isinstance(attribute.ctx, ast.Load)
                        for attribute in self._class_attributes(child, field)
                    ),
                )
            )
            for child in self.model.classes
        )
        return any((parameter_escapes, field_escapes, subclass_mutates))

    def _inherits(self, child: str, parent: str) -> bool:
        bases = self.bases.get(child)
        if bases is None:
            return False
        return parent in bases or any(self._inherits(base, parent) for base in bases)

    def _prepare_fields(self) -> None:
        for identity in self.model.classes:
            attributes = self._class_attributes(identity)
            fields = {node.attr for node in attributes}
            for field_name in fields:
                matching = [node for node in attributes if node.attr == field_name]
                stores = [node for node in matching if isinstance(node.ctx, ast.Store)]
                if any(isinstance(node.ctx, ast.Del) for node in matching) or not stores:
                    continue
                constructors = [
                    target.label
                    for store in stores
                    if id(store) not in self.model.conditional
                    if isinstance(parent := self.model.parents.get(id(store)),
                                  (ast.Assign, ast.AnnAssign))
                    if isinstance(parent.value, ast.Call)
                    if (target := self._resolve_expr(self.model.node_scope[id(store)],
                                                     parent.value.func))
                    if target.kind == "class" and target.label in self.model.classes
                ]
                if len(constructors) == len(stores) and len(set(constructors)) == 1:
                    self.field_receivers[(identity, field_name)] = constructors[0]
                    continue
                injected = self._injection(identity, field_name, stores)
                if injected is not None:
                    self.field_receivers[(identity, field_name)] = injected

    def _resolve_attribute_call(self, scope: _Scope, expression: ast.Attribute) -> _Target | None:
        class_info = self.model.enclosing_class(scope)
        class_identity = getattr(getattr(class_info, "scope", None), "identity", "")
        value = expression.value
        if isinstance(value, ast.Attribute):
            pair = (
                (value.value.id, value.attr)
                if isinstance(value.value, ast.Name)
                and value.value.id in {"self", "cls"}
                else None
            )
            field = dict((pair,) if pair else ()).get("self")
            receiver = self.field_receivers.get((class_identity, field))
            if receiver is None:
                return self._resolve_expr(scope, expression)
            method = self._lookup_method(receiver, expression.attr)
            return _Target(method, "function") if method else None
        receiver = None
        parents_only = False
        if isinstance(value, ast.Name):
            class_receiver = bool(class_identity) and value.id in {"self", "cls"}
            if class_receiver and self._lexical_parameter(
                scope, value.id, class_info.scope
            ):
                receiver = class_identity
            else:
                receiver = self.receivers.get((id(scope), value.id))
        elif isinstance(value, ast.Call):
            is_super = all((
                isinstance(value.func, ast.Name), getattr(value.func, "id", None) == "super",
                class_info is not None, not value.args, not value.keywords,
            ))
            if is_super:
                target = self._resolve_name(scope, "super", value.func)
                if getattr(target, "label", None) == "builtins.super":
                    receiver = class_identity
                    parents_only = True
            else:
                target = self._resolve_expr(scope, value.func)
                if all((getattr(target, "kind", None) == "class",
                        getattr(target, "label", None) in self.model.classes)):
                    receiver = target.label
        if receiver is None:
            return self._resolve_expr(scope, expression)
        method = self._lookup_method(
            receiver, expression.attr, parents_only=parents_only
        )
        return _Target(method, "function") if method else None

    @staticmethod
    def _lexical_parameter(scope: _Scope, name: str, class_scope: _Scope) -> bool:
        current: _Scope | None = scope
        while current is not None and current.kind != "class":
            if name in current.params or current.bindings.get(name):
                return name in current.params and current.parent is class_scope
            current = current.parent
        return False

    def result(self) -> ResolutionIndex:
        records: dict[str, object] = {}
        call_records: dict[int, object] = {}
        call_evidence: dict[str, CallsiteEvidence] = {}
        callsite_primitives = {str(row["selector"]): row for row in self.primitives
                               if row["selector_kind"] == "callsite"}
        entity_primitives = {str(row["selector"]): row for row in self.primitives
                             if row["selector_kind"] == "entity"}
        used_rows: set[tuple[str, str]] = {
            ("entity", selector)
            for selector in entity_primitives
            if selector in self.model.index.entities
        }
        for owner, calls in self.model.calls.items():
            if len(calls) > 9_999:
                raise ValueError(f"owner exceeds 9,999 callsites: {owner}")
            for ordinal, call in enumerate(calls, 1):
                callsite = f"{owner}::call:{ordinal:04d}"
                record = self._resolve_record(
                    callsite, call, callsite_primitives, entity_primitives, used_rows)
                records[callsite] = record
                call_records[id(call)] = record
                call_evidence[callsite] = _callsite_evidence(callsite, owner, call)
        for row in self.primitives:
            binding = str(row["selector_kind"]), str(row["selector"])
            if binding not in used_rows:
                raise ValueError(f"unused primitive row: {row['selector']}")
        alias_evidence = {
            f"{scope.identity}::{name}": target.label
            for scope in self.model.scopes
            for name in scope.bindings
            if (target := self.aliases.get((id(scope), name))) is not None
        }
        receiver_evidence = {
            f"{scope.identity}::{name}": target
            for scope in self.model.scopes
            for name in scope.bindings
            if (target := self.receivers.get((id(scope), name))) is not None
        } | {f"{identity}::self.{field}": target
             for (identity, field), target in self.field_receivers.items()}
        dependency_evidence: dict[str, object] = {}
        for scope in self.model.scopes:
            if not isinstance(scope.node, _NAMED):
                continue
            expressions = [
                ("decorator", expression)
                for expression in scope.node.decorator_list
            ]
            if isinstance(scope.node, ast.ClassDef):
                expressions.extend(
                    ("base", expression) for expression in scope.node.bases
                )
                expressions.extend(
                    ("metaclass", keyword.value)
                    for keyword in scope.node.keywords
                    if keyword.arg == "metaclass"
                )
            if len(expressions) > 9_999:
                raise ValueError(
                    f"owner exceeds 9,999 dependency references: {scope.identity}"
                )
            parent = scope.parent
            assert parent is not None
            for ordinal, (kind, expression) in enumerate(expressions, 1):
                reference = f"{scope.identity}::dependency:{ordinal:04d}"
                target = self._resolve_expr(
                    parent, expression, kind,
                    {"decorator": call_records}.get(kind))
                if target is None:
                    dependency_evidence[reference] = UnresolvedDependency(
                        reference,
                        scope.identity,
                        kind,
                        expression.lineno,
                        expression.col_offset,
                        ast.dump(expression, include_attributes=False),
                    )
                else:
                    dependency_evidence[reference] = ResolvedDependency(
                        reference, scope.identity, kind, target.label
                    )
        return ResolutionIndex(
            MappingProxyType(records),
            MappingProxyType(alias_evidence),
            MappingProxyType(receiver_evidence),
            MappingProxyType(dependency_evidence),
            self.model.reference_source_sha256,
            MappingProxyType(call_evidence),
        )

    def _resolve_record(self, callsite: str, call: ast.Call,
                        callsite_primitives: Mapping[str, Mapping[str, object]],
                        entity_primitives: Mapping[str, Mapping[str, object]],
                        used_rows: set[tuple[str, str]]) -> object:
        scope = self.model.node_scope[id(call)]
        target = None
        if isinstance(call.func, ast.Attribute):
            target = self._resolve_attribute_call(scope, call.func)
        elif isinstance(call.func, ast.Name):
            target = self._resolve_name(scope, call.func.id, call.func)
        primitive = callsite_primitives.get(callsite)
        if target is None and primitive is not None:
            used_rows.add(("callsite", callsite))
            target = _Target(str(primitive["semantic_target"]), "primitive")
        elif primitive is not None:
            external = target.kind != "builtin" and not target.label.startswith("src/")
            if not all((external, primitive["semantic_target"] == target.label)):
                raise ValueError(f"stale callsite primitive row: {callsite}")
            used_rows.add(("callsite", callsite))
        if target is None:
            return UnresolvedCall(callsite, call.lineno, call.col_offset,
                                  ast.dump(call, include_attributes=False))
        external = all((target.kind not in {"primitive", "builtin"},
                        not target.label.startswith("src/")))
        if external and target.label in entity_primitives:
            used_rows.add(("entity", target.label))
        covered = any((not external, target.label in self.allowlist,
                       target.label in entity_primitives, ("callsite", callsite) in used_rows))
        if not covered:
            raise ValueError(f"external target lacks exact effect coverage: {target.label}")
        return ResolvedCall(callsite, target.label)


def resolve_calls(index, allowlist, primitives):
    if not isinstance(index, SourceIndex):
        raise TypeError("index must be SourceIndex")
    return _Resolver(index, allowlist, primitives).result()
