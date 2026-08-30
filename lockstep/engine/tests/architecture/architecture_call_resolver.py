"""Closed, conservative call resolution for the architecture analyzer."""

from __future__ import annotations

import ast
import builtins
from collections import defaultdict
from dataclasses import dataclass
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
class ResolutionIndex:
    calls: Mapping[str, object]
    aliases: Mapping[str, str]
    receivers: Mapping[str, str]


@dataclass(slots=True)
class _Binding:
    kind: str
    value: object
    node: ast.AST
    conditional: bool = False


class _Scope:
    def __init__(self, kind: str, parent: _Scope | None, identity: str, node: ast.AST):
        self.kind = kind
        self.parent = parent
        self.identity = identity
        self.node = node
        self.bindings: dict[str, list[_Binding]] = defaultdict(list)
        self.params: set[str] = set()
        self.globals: dict[str, list[ast.Global]] = defaultdict(list)
        self.nonlocals: dict[str, list[ast.Nonlocal]] = defaultdict(list)
        self.loads: dict[str, list[ast.Name]] = defaultdict(list)
        self.children: list[_Scope] = []
        if parent is not None:
            parent.children.append(self)


@dataclass(slots=True)
class _ClassInfo:
    scope: _Scope
    methods: dict[str, str]
    bases: list[ast.expr]


_NAMED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_CONDITIONAL = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.TryStar,
    ast.With,
    ast.AsyncWith,
    ast.Match,
    ast.comprehension,
    ast.IfExp,
    ast.BoolOp,
    ast.Lambda,
)


def _arg_nodes(arguments: ast.arguments) -> tuple[ast.arg, ...]:
    nodes = [*arguments.posonlyargs, *arguments.args]
    if arguments.vararg is not None:
        nodes.append(arguments.vararg)
    nodes.extend(arguments.kwonlyargs)
    if arguments.kwarg is not None:
        nodes.append(arguments.kwarg)
    return tuple(nodes)


def _assigned_names(node: ast.AST) -> tuple[str, ...]:
    found: list[str] = []
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del)):
            found.append(item.id)
    return tuple(found)


class _Model:
    """Private AST model reparsed from the exact indexed source bytes."""

    def __init__(self, index: SourceIndex):
        self.index = index
        self.trees: dict[str, ast.Module] = {}
        self.scopes: list[_Scope] = []
        self.node_scope: dict[int, _Scope] = {}
        self.parents: dict[int, ast.AST] = {}
        self.conditional: set[int] = set()
        self.calls: dict[str, list[ast.Call]] = defaultdict(list)
        self.classes: dict[str, _ClassInfo] = {}
        self.named_scopes: dict[str, _Scope] = {}
        self.modules: dict[str, _Scope] = {}
        for path in sorted(index.files):
            tree = ast.parse(index.files[path], filename=path)
            self.trees[path] = tree
            module = _Scope("module", None, f"{path}::@file", tree)
            self.scopes.append(module)
            self.modules[path] = module
            self._visit_sequence(tree.body, module, module.identity, False)
        for scope in self.scopes:
            self._collect_scope_facts(scope)

    def _mark(self, node: ast.AST, scope: _Scope, conditional: bool) -> None:
        self.node_scope[id(node)] = scope
        if conditional:
            self.conditional.add(id(node))

    def _visit_sequence(
        self, nodes: Sequence[ast.AST], scope: _Scope, owner: str, conditional: bool
    ) -> None:
        for node in nodes:
            self._visit(node, scope, owner, conditional)

    def _visit(self, node: ast.AST, scope: _Scope, owner: str, conditional: bool) -> None:
        self._mark(node, scope, conditional)
        if isinstance(node, _NAMED):
            self._register_named(node, scope, conditional)
            return
        if isinstance(node, ast.Lambda):
            child = _Scope("lambda", scope, owner, node)
            self.scopes.append(child)
            self._mark(node.args, child, True)
            for arg in _arg_nodes(node.args):
                child.params.add(arg.arg)
            for field, value in ast.iter_fields(node):
                if field == "body":
                    self._child(value, child, owner, True, node)
                elif field != "args":
                    self._child(value, scope, owner, conditional, node)
            return
        if isinstance(node, ast.Call):
            self.calls[owner].append(node)
        child_conditional = conditional or isinstance(node, _CONDITIONAL)
        if isinstance(node, ast.NamedExpr):
            child_conditional = True
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
            self.parents[id(child)] = parent
            self._visit(child, scope, owner, conditional)
        elif isinstance(child, list):
            for item in child:
                self._child(item, scope, owner, conditional, parent)

    def _register_named(self, node: ast.AST, parent: _Scope, conditional: bool) -> None:
        assert isinstance(node, _NAMED)
        identity = f"{parent.identity.rsplit('::', 1)[0]}::{self._qualname(parent, node.name)}"
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
            self._visit_sequence(node.body, child, identity, conditional)
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
        for arg in _arg_nodes(node.args):
            child.params.add(arg.arg)
        self._visit_sequence(node.body, child, identity, conditional)

    def _qualname(self, parent: _Scope, name: str) -> str:
        suffix = parent.identity.split("::", 1)[1]
        return name if suffix == "@file" else f"{suffix}.{name}"

    def _collect_scope_facts(self, scope: _Scope) -> None:
        root = scope.node
        for node in ast.walk(root):
            if self.node_scope.get(id(node)) is not scope:
                continue
            conditional = id(node) in self.conditional
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load):
                    scope.loads[node.id].append(node)
                elif isinstance(node.ctx, ast.Store):
                    kind = "aug" if isinstance(self.parents.get(id(node)), ast.AugAssign) else "store"
                    scope.bindings[node.id].append(_Binding(kind, None, node, conditional))
                elif isinstance(node.ctx, ast.Del):
                    scope.bindings[node.id].append(_Binding("del", None, node, conditional))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    value = alias.name if alias.asname else alias.name.split(".")[0]
                    scope.bindings[name].append(_Binding("import", value, node, conditional))
            elif isinstance(node, ast.ImportFrom):
                if any(alias.name == "*" for alias in node.names):
                    continue
                module = "." * node.level + (node.module or "")
                for alias in node.names:
                    name = alias.asname or alias.name
                    scope.bindings[name].append(
                        _Binding("import", f"{module}.{alias.name}".strip("."), node, conditional)
                    )
            elif isinstance(node, ast.Global):
                for name in node.names:
                    scope.globals[name].append(node)
            elif isinstance(node, ast.Nonlocal):
                for name in node.names:
                    scope.nonlocals[name].append(node)
            elif isinstance(node, ast.ExceptHandler) and isinstance(node.name, str):
                scope.bindings[node.name].append(_Binding("store", None, node, True))
                scope.bindings[node.name].append(_Binding("del", None, node, True))
            elif isinstance(node, ast.Match):
                for case in node.cases:
                    for name in _assigned_names(case.pattern):
                        scope.bindings[name].append(_Binding("store", None, case.pattern, True))

    def enclosing_class(self, scope: _Scope) -> _ClassInfo | None:
        current = scope.parent
        while current is not None:
            if current.kind == "class":
                return self.classes[current.identity]
            current = current.parent
        return None

    def module_scope(self, scope: _Scope) -> _Scope:
        while scope.parent is not None:
            scope = scope.parent
        return scope

    def path_for(self, scope: _Scope) -> str:
        return scope.identity.split("::", 1)[0]


@dataclass(frozen=True, slots=True)
class _Target:
    label: str
    kind: str


class _Resolver:
    def __init__(self, index: SourceIndex, allowlist: object, primitives: object):
        self.model = _Model(index)
        self.allowlist = self._read_allowlist(allowlist)
        self.primitives = self._read_primitives(primitives)
        overlap = self.allowlist & {
            row["selector"] for row in self.primitives if row["selector_kind"] == "entity"
        }
        if overlap:
            raise ValueError("effect-free allowlist and entity primitive selectors overlap")
        self.aliases: dict[tuple[int, str], _Target] = {}
        self.receivers: dict[tuple[int, str], str] = {}
        self.field_receivers: dict[tuple[str, str], str] = {}
        self.bases: dict[str, tuple[str, ...] | None] = {}
        self._prepare_aliases()
        self._prepare_receivers()
        self._prepare_bases()
        self._prepare_fields()

    @staticmethod
    def _read_allowlist(value: object) -> frozenset[str]:
        if isinstance(value, Mapping):
            if set(value) != {"schema_version", "targets"} or value["schema_version"] != 1:
                raise ValueError("invalid effect-free allowlist")
            value = value["targets"]
        if isinstance(value, (str, bytes)) or not isinstance(value, (set, frozenset, tuple, list)):
            raise ValueError("invalid effect-free allowlist")
        if not all(isinstance(item, str) for item in value):
            raise ValueError("invalid effect-free target")
        return frozenset(value)

    @staticmethod
    def _read_primitives(value: object) -> tuple[Mapping[str, object], ...]:
        if isinstance(value, Mapping):
            if set(value) != {"schema_version", "rows"} or value["schema_version"] != 1:
                raise ValueError("invalid effect primitive table")
            value = value["rows"]
        if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
            raise ValueError("invalid effect primitive table")
        rows: list[Mapping[str, object]] = []
        for row in value:
            if not isinstance(row, Mapping) or set(row) != {
                "selector_kind", "selector", "semantic_target", "domains"
            }:
                raise ValueError("invalid effect primitive row")
            if row["selector_kind"] not in {"callsite", "entity"}:
                raise ValueError("invalid primitive selector kind")
            if not isinstance(row["selector"], str) or not isinstance(row["semantic_target"], str):
                raise ValueError("invalid effect primitive selector")
            rows.append(dict(row))
        callsites = {r["selector"] for r in rows if r["selector_kind"] == "callsite"}
        entities = {r["selector"] for r in rows if r["selector_kind"] == "entity"}
        if callsites & entities:
            raise ValueError("primitive selector spaces overlap")
        return tuple(rows)

    def _module_entity(self, symbol: str) -> str | None:
        parts = symbol.split(".")
        for cut in range(len(parts), 0, -1):
            module = "/".join(parts[:cut])
            candidates = (f"src/{module}.py", f"{module}.py", f"src/{module}/__init__.py")
            for path in candidates:
                if path not in self.model.trees:
                    continue
                rest = ".".join(parts[cut:])
                return f"{path}::{rest or '@file'}"
        return None

    def _normalize_target(self, label: str, kind: str) -> _Target:
        entity = self._module_entity(label)
        if entity is not None and not entity.endswith("::@file"):
            scope = self.model.named_scopes.get(entity)
            return _Target(entity, scope.kind if scope else kind)
        return _Target(label, kind)

    def _declaration_valid(self, scope: _Scope, name: str, load: ast.Name) -> tuple[str, _Scope] | None:
        globals_ = scope.globals.get(name, ())
        nonlocals = scope.nonlocals.get(name, ())
        if globals_ and nonlocals or len(globals_) > 1 or len(nonlocals) > 1:
            return None
        declarations = globals_ or nonlocals
        if not declarations:
            return ("local", scope)
        declaration = declarations[0]
        if (load.lineno, load.col_offset) < (declaration.lineno, declaration.col_offset):
            return None
        if any(binding.kind in {"store", "del", "aug"} for binding in scope.bindings.get(name, ())):
            return None
        if globals_:
            target = self.model.module_scope(scope)
            return ("redirect", target) if self._scope_defines(target, name) else None
        target = scope.parent
        while target is not None:
            if target.kind != "class" and self._scope_defines(target, name):
                return "redirect", target
            target = target.parent
        return None

    @staticmethod
    def _scope_defines(scope: _Scope, name: str) -> bool:
        return name in scope.params or bool(scope.bindings.get(name))

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
                alias = self.aliases.get((id(current), name))
                if alias is not None:
                    binding = bindings[0]
                    if current is not original or (binding.node.lineno, binding.node.col_offset) < (
                        getattr(load, "lineno", 0), getattr(load, "col_offset", 0)
                    ):
                        return alias
                    return None
                if len(bindings) != 1:
                    return None
                binding = bindings[0]
                if binding.conditional:
                    return None
                if binding.kind in {"function", "class"}:
                    if current is not original or (binding.node.lineno, binding.node.col_offset) < (
                        getattr(load, "lineno", 0), getattr(load, "col_offset", 0)
                    ):
                        return _Target(str(binding.value), binding.kind)
                    return None
                if binding.kind == "import":
                    if current is not original or (binding.node.lineno, binding.node.col_offset) < (
                        getattr(load, "lineno", 0), getattr(load, "col_offset", 0)
                    ):
                        return self._normalize_target(str(binding.value), "import")
                    return None
                return None
            parent = current.parent
            if parent is not None and parent.kind == "class" and original.kind in {"function", "lambda"}:
                parent = parent.parent
            current = parent
        builtin_target = f"builtins.{name}"
        if name in dir(builtins) and builtin_target in self.allowlist:
            return _Target(builtin_target, "builtin")
        return None

    def _resolve_expr(self, scope: _Scope, expression: ast.AST) -> _Target | None:
        if isinstance(expression, ast.Name):
            return self._resolve_name(scope, expression.id, expression)
        if isinstance(expression, ast.Attribute):
            base = self._resolve_expr(scope, expression.value)
            if base is None:
                return None
            if base.label in self.model.classes:
                method = self._lookup_method(base.label, expression.attr)
                return _Target(method, "function") if method else None
            return self._normalize_target(f"{base.label}.{expression.attr}", "import")
        return None

    def _assignment(self, binding: _Binding) -> tuple[ast.AST, ast.AST | None]:
        node: ast.AST = binding.node
        parent = self.model.parents.get(id(node))
        if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            value = parent.value
            return parent, value
        return node, None

    def _symbol_use_is_safe(self, name: str, node: ast.Name) -> bool:
        parent = self.model.parents.get(id(node))
        if isinstance(parent, ast.Call) and parent.func is node:
            return True
        if isinstance(parent, ast.Attribute) and parent.value is node:
            grand = self.model.parents.get(id(parent))
            return isinstance(grand, ast.Call) and grand.func is parent
        if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            return True
        # Decorator/base nodes have no distinguishing parent field; all direct uses are safe.
        return isinstance(parent, (ast.keyword, ast.Subscript))

    def _prepare_aliases(self) -> None:
        changed = True
        while changed:
            changed = False
            for scope in self.model.scopes:
                for name, bindings in scope.bindings.items():
                    key = (id(scope), name)
                    if key in self.aliases or len(bindings) != 1:
                        continue
                    binding = bindings[0]
                    if binding.kind != "store" or binding.conditional:
                        continue
                    assignment, value = self._assignment(binding)
                    if value is None or not isinstance(value, (ast.Name, ast.Attribute)):
                        continue
                    target = self._resolve_expr(scope, value)
                    if target is None or target.kind not in {"function", "class", "import"}:
                        continue
                    loads = [n for n in scope.loads.get(name, ()) if n is not value]
                    if any(not self._symbol_use_is_safe(name, node) for node in loads):
                        continue
                    # Closure reads/writes make an alias non-immutable.
                    if any(self._descendant_writes(child, name) for child in scope.children):
                        continue
                    self.aliases[key] = target
                    changed = True

    def _descendant_mentions(self, scope: _Scope, name: str) -> bool:
        if name in scope.loads or name in scope.bindings or name in scope.globals or name in scope.nonlocals:
            return True
        return any(self._descendant_mentions(child, name) for child in scope.children)

    def _descendant_writes(self, scope: _Scope, name: str) -> bool:
        if name in scope.bindings or name in scope.globals or name in scope.nonlocals:
            return True
        return any(self._descendant_writes(child, name) for child in scope.children)

    def _constructor_target(self, scope: _Scope, value: ast.AST) -> str | None:
        if not isinstance(value, ast.Call):
            return None
        target = self._resolve_expr(scope, value.func)
        if target is None or target.kind != "class" or target.label not in self.model.classes:
            return None
        return target.label

    def _receiver_use_is_safe(self, node: ast.Name) -> bool:
        parent = self.model.parents.get(id(node))
        if not isinstance(parent, ast.Attribute) or parent.value is not node:
            return False
        grand = self.model.parents.get(id(parent))
        return isinstance(grand, ast.Call) and grand.func is parent

    def _prepare_receivers(self) -> None:
        for scope in self.model.scopes:
            for name, bindings in scope.bindings.items():
                if len(bindings) != 1 or bindings[0].kind != "store" or bindings[0].conditional:
                    continue
                binding = bindings[0]
                assignment, value = self._assignment(binding)
                receiver: str | None = None
                if isinstance(assignment, ast.AnnAssign) and assignment.value is None:
                    annotation = self._resolve_expr(scope, assignment.annotation)
                    if annotation and annotation.kind == "class" and annotation.label in self.model.classes:
                        receiver = annotation.label
                elif value is not None:
                    receiver = self._constructor_target(scope, value)
                if receiver is None:
                    continue
                if any(not self._receiver_use_is_safe(node) for node in scope.loads.get(name, ())):
                    continue
                if any(self._descendant_mentions(child, name) for child in scope.children):
                    continue
                self.receivers[(id(scope), name)] = receiver

    def _prepare_bases(self) -> None:
        for identity, info in self.model.classes.items():
            resolved: list[str] = []
            parent = info.scope.parent
            assert parent is not None
            for expression in info.bases:
                target = self._resolve_expr(parent, expression)
                if target is None or target.kind != "class" or target.label not in self.model.classes:
                    self.bases[identity] = None
                    break
                resolved.append(target.label)
            else:
                self.bases[identity] = tuple(resolved)

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

    @staticmethod
    def _self_attribute(node: ast.AST) -> tuple[str, str] | None:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in {"self", "cls"}:
                return node.value.id, node.attr
        return None

    def _class_attributes(self, identity: str, field: str | None = None) -> list[ast.Attribute]:
        root = self.model.classes[identity].scope.node
        attributes: list[ast.Attribute] = []
        for node in ast.walk(root):
            if not isinstance(node, ast.Attribute):
                continue
            owner_scope = self.model.node_scope.get(id(node))
            if owner_scope is None or self.model.enclosing_class(owner_scope) is not self.model.classes[identity]:
                continue
            pair = self._self_attribute(node)
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
        argument = next((arg for arg in _arg_nodes(function.args) if arg.arg == parameter), None)
        if argument is None or not isinstance(argument.annotation, (ast.Name, ast.Attribute)):
            return None
        parent_scope = init_scope.parent
        assert parent_scope is not None
        annotation = self._resolve_expr(parent_scope, argument.annotation)
        if annotation is None or annotation.kind != "class" or annotation.label not in self.model.classes:
            return None
        # Parameter may occur only as the exact assignment RHS.
        for scope in self.model.scopes:
            if scope is init_scope or self._is_descendant(scope, init_scope):
                for load in scope.loads.get(parameter, ()):
                    if load is assignment.value:
                        continue
                    return None
                if scope is init_scope:
                    parameter_bindings = scope.bindings.get(parameter, ())
                    if parameter_bindings:
                        return None
                elif parameter in scope.globals or parameter in scope.nonlocals or parameter in scope.bindings:
                    return None
        # Field reads are terminal method receivers only; every mutation is forbidden.
        for attribute in self._class_attributes(identity, field):
            if attribute is store:
                continue
            if not isinstance(attribute.ctx, ast.Load):
                return None
            parent = self.model.parents.get(id(attribute))
            if not isinstance(parent, ast.Attribute) or parent.value is not attribute:
                return None
            grand = self.model.parents.get(id(parent))
            if not isinstance(grand, ast.Call) or grand.func is not parent:
                return None
        # A resolved subclass may not mutate the injected field.
        for child in self.model.classes:
            if child == identity or not self._inherits(child, identity):
                continue
            if any(not isinstance(attribute.ctx, ast.Load) for attribute in self._class_attributes(child, field)):
                return None
        return annotation.label

    @staticmethod
    def _is_descendant(scope: _Scope, parent: _Scope) -> bool:
        current = scope.parent
        while current is not None:
            if current is parent:
                return True
            current = current.parent
        return False

    def _inherits(self, child: str, parent: str) -> bool:
        bases = self.bases.get(child)
        if bases is None:
            return False
        return parent in bases or any(self._inherits(base, parent) for base in bases)

    def _prepare_fields(self) -> None:
        for identity in self.model.classes:
            attributes = self._class_attributes(identity)
            fields = {pair[1] for node in attributes if (pair := self._self_attribute(node))}
            for field in fields:
                matching = [node for node in attributes if self._self_attribute(node)[1] == field]
                stores = [node for node in matching if isinstance(node.ctx, ast.Store)]
                deletes = [node for node in matching if isinstance(node.ctx, ast.Del)]
                if deletes or not stores:
                    continue
                constructors: list[str] = []
                valid = True
                for store in stores:
                    if id(store) in self.model.conditional:
                        valid = False
                        break
                    parent = self.model.parents.get(id(store))
                    if not isinstance(parent, (ast.Assign, ast.AnnAssign)):
                        valid = False
                        break
                    value = parent.value
                    constructor = self._constructor_target(self.model.node_scope[id(store)], value) if value else None
                    if constructor is None:
                        valid = False
                        break
                    constructors.append(constructor)
                if valid and len(set(constructors)) == 1:
                    self.field_receivers[(identity, field)] = constructors[0]
                    continue
                injected = self._injection(identity, field, stores)
                if injected is not None:
                    self.field_receivers[(identity, field)] = injected

    def _resolve_attribute_call(self, scope: _Scope, expression: ast.Attribute) -> _Target | None:
        class_info = self.model.enclosing_class(scope)
        value = expression.value
        if isinstance(value, ast.Name):
            if class_info and value.id in {"self", "cls"} and self._lexical_parameter(scope, value.id):
                method = self._lookup_method(class_info.scope.identity, expression.attr)
                return _Target(method, "function") if method else None
            receiver = self.receivers.get((id(scope), value.id))
            if receiver is not None:
                method = self._lookup_method(receiver, expression.attr)
                return _Target(method, "function") if method else None
        if isinstance(value, ast.Call):
            if isinstance(value.func, ast.Name) and value.func.id == "super" and class_info:
                method = self._lookup_method(class_info.scope.identity, expression.attr, parents_only=True)
                return _Target(method, "function") if method else None
            receiver = self._constructor_target(scope, value)
            if receiver is not None:
                method = self._lookup_method(receiver, expression.attr)
                return _Target(method, "function") if method else None
        if isinstance(value, ast.Attribute):
            pair = self._self_attribute(value)
            if pair and class_info and pair[0] == "self":
                receiver = self.field_receivers.get((class_info.scope.identity, pair[1]))
                if receiver is not None:
                    method = self._lookup_method(receiver, expression.attr)
                    return _Target(method, "function") if method else None
            return None
        return self._resolve_expr(scope, expression)

    @staticmethod
    def _lexical_parameter(scope: _Scope, name: str) -> bool:
        current: _Scope | None = scope
        while current is not None and current.kind != "class":
            if name in current.params:
                return True
            current = current.parent
        return False

    def _resolve_call(self, scope: _Scope, call: ast.Call) -> _Target | None:
        if isinstance(call.func, ast.Attribute):
            return self._resolve_attribute_call(scope, call.func)
        if isinstance(call.func, ast.Name):
            return self._resolve_name(scope, call.func.id, call.func)
        return None

    def result(self) -> ResolutionIndex:
        records: dict[str, object] = {}
        for owner, calls in self.model.calls.items():
            if len(calls) > 9_999:
                raise ValueError(f"owner exceeds 9,999 callsites: {owner}")
            for ordinal, call in enumerate(calls, 1):
                callsite = f"{owner}::call:{ordinal:04d}"
                scope = self.model.node_scope[id(call)]
                target = self._resolve_call(scope, call)
                if target is None:
                    primitive = next(
                        (
                            row
                            for row in self.primitives
                            if row["selector_kind"] == "callsite" and row["selector"] == callsite
                        ),
                        None,
                    )
                    if primitive is not None:
                        target = _Target(str(primitive["semantic_target"]), "primitive")
                if target is None:
                    records[callsite] = UnresolvedCall(
                        callsite,
                        call.lineno,
                        call.col_offset,
                        ast.dump(call, include_attributes=False),
                    )
                else:
                    records[callsite] = ResolvedCall(callsite, target.label)
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
        }
        receiver_evidence.update(
            {f"{identity}::self.{field}": target for (identity, field), target in self.field_receivers.items()}
        )
        return ResolutionIndex(
            MappingProxyType(records),
            MappingProxyType(alias_evidence),
            MappingProxyType(receiver_evidence),
        )


def resolve_calls(index, allowlist, primitives):
    if not isinstance(index, SourceIndex):
        raise TypeError("index must be SourceIndex")
    return _Resolver(index, allowlist, primitives).result()
