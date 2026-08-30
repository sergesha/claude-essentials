"""Deterministic god-object candidate policy over frozen analyzer evidence."""
from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
import symtable
from types import MappingProxyType

from architecture_call_resolver import ResolvedCall, ResolvedDependency, ResolutionIndex, UnresolvedCall
from architecture_domain_lifecycle import SemanticIndex
from architecture_legacy_metrics import LegacyMetrics
from architecture_source_index import SourceIndex


@dataclass(frozen=True, slots=True)
class FunctionMetrics:
    cyclomatic: int
    cognitive: int
    max_nesting: int
    legacy_syntactic_fanout: int
    resolved_fanout: int
    direct_domains: tuple[str, ...]
    propagated_domains: tuple[str, ...]
    direct_transitions: tuple[str, ...]
    propagated_transitions: tuple[str, ...]
    propagated_lifecycle_clusters: tuple[str, ...]
    unresolved_callsites: tuple[str, ...]
    signals: Mapping[str, bool]
    composite_score: int
    hard_triggers: tuple[str, ...]
    candidate: bool


@dataclass(frozen=True, slots=True)
class OneHopMetrics:
    root: str
    members: tuple[str, ...]
    helper_count: int
    summed_cyclomatic: int
    summed_cognitive: int
    max_nesting: int
    legacy_syntactic_fanout_union: int
    resolved_fanout_union: int
    propagated_domains: tuple[str, ...]
    propagated_transitions: tuple[str, ...]
    propagated_lifecycle_clusters: tuple[str, ...]
    signals: Mapping[str, bool]
    composite_score: int
    hard_triggers: tuple[str, ...]
    candidate: bool


@dataclass(frozen=True, slots=True)
class ClassMetrics:
    method_count: int
    public_method_count: int
    mutable_fields: tuple[str, ...]
    mutable_field_count: int
    cohesion_components: int
    bases: tuple[str, ...]
    propagated_domains: tuple[str, ...]
    propagated_transitions: tuple[str, ...]
    propagated_lifecycle_clusters: tuple[str, ...]
    signals: Mapping[str, bool]
    composite_score: int
    hard_triggers: tuple[str, ...]
    candidate: bool


@dataclass(frozen=True, slots=True)
class FileMetrics:
    definition_count: int
    class_count: int
    subsystem_imports: tuple[str, ...]
    subsystem_import_count: int
    definition_dependency_components: int
    propagated_domains: tuple[str, ...]
    propagated_transitions: tuple[str, ...]
    propagated_lifecycle_clusters: tuple[str, ...]
    signals: Mapping[str, bool]
    composite_score: int
    hard_triggers: tuple[str, ...]
    candidate: bool


@dataclass(frozen=True, slots=True)
class ArchitectureReport:
    functions: Mapping[str, FunctionMetrics]
    one_hops: Mapping[str, OneHopMetrics]
    classes: Mapping[str, ClassMetrics]
    files: Mapping[str, FileMetrics]
    unresolved_callsites: tuple[str, ...]
    allowlist_digest: str
    primitive_digest: str
    lifecycle_digest: str
    schema_digest: str
    threshold_digest: str
    analyzer_version: str
    rule_version: str


_DOMAINS = ("decode/validate", "planning/transformation", "filesystem-read", "filesystem-write", "durable-state", "synchronization", "external-process/provider", "authority/commitment", "lifecycle-control", "projection/output")
_MUTATORS = frozenset(("append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse", "update", "setdefault", "add", "discard", "difference_update", "intersection_update", "symmetric_difference_update"))
_TRUE_DUNDER = re.compile(r"^__.*__$")
_ROOT = Path(__file__).parent


def _rules():
    return json.loads((_ROOT / "architecture_thresholds.json").read_bytes())


def _vocabulary():
    rows = json.loads((_ROOT / "architecture_lifecycle.json").read_bytes())["transitions"]
    return (tuple(row["transition_id"] for row in rows),
            tuple(dict.fromkeys(row["cluster"] for row in rows)))


def _nodes(index: SourceIndex):
    found = {}
    for path, source in index.files.items():
        def visit(node, parents=()):
            nested = parents
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                nested = (*parents, node.name)
                found[f"{path}::{'.'.join(nested)}"] = node
            for child in ast.iter_child_nodes(node):
                visit(child, nested)
        visit(ast.parse(source, filename=path))
    return found


def _owner(callsite):
    return callsite.rsplit("::call:", 1)[0]


def _calls(resolutions):
    return tuple((_owner(key), value) for key, value in resolutions.calls.items())


def _ordered(order, *values):
    present = {item for group in values for item in group}
    return tuple(item for item in order if item in present)


def _decision(kind, signals, hard, rules):
    score = sum(signals.values())
    mixing = signals["domain_mixing"] or signals["lifecycle_mixing"]
    if kind == "class":
        mixing = mixing or signals["cohesion_components"]
    if kind == "file":
        mixing = mixing or signals["definition_dependency_components"]
    return score, bool(hard or (score >= rules["kinds"][kind]["minimum_signals"] and mixing))


def _function(identity, metric, semantic, calls, rules):
    owned = tuple(record for owner, record in calls if owner == identity)
    resolved = {record.target for record in owned if isinstance(record, ResolvedCall)}
    unresolved = tuple(record.callsite for record in owned if isinstance(record, UnresolvedCall))
    policy = rules["kinds"]["function"]
    threshold = policy["signals"]
    signals = MappingProxyType({
        "cyclomatic": metric.cyclomatic >= threshold["cyclomatic"],
        "cognitive": metric.cognitive >= threshold["cognitive"],
        "nesting": metric.max_nesting >= threshold["nesting"],
        "legacy_syntactic_fanout": metric.legacy_syntactic_fanout >= threshold["legacy_syntactic_fanout"],
        "domain_mixing": len(semantic.propagated_domains) >= threshold["domain_mixing"],
        "lifecycle_mixing": len(semantic.propagated_lifecycle_clusters) >= threshold["lifecycle_mixing"],
    })
    hard_inputs = (("cyclomatic_gt_15", metric.cyclomatic),
                   ("cognitive_gt_25", metric.cognitive),
                   ("nesting_gt_4", metric.max_nesting),
                   ("legacy_syntactic_fanout_gt_24", metric.legacy_syntactic_fanout))
    hard = tuple(name for name, value in hard_inputs if value > policy["hard"][name])
    score, candidate = _decision("function", signals, hard, rules)
    return FunctionMetrics(metric.cyclomatic, metric.cognitive, metric.max_nesting,
        metric.legacy_syntactic_fanout, len(resolved), semantic.direct_domains,
        semantic.propagated_domains, semantic.direct_transitions, semantic.propagated_transitions,
        semantic.propagated_lifecycle_clusters, unresolved, signals, score, hard, candidate)


def _components(vertices, edges):
    remaining, answer = set(vertices), []
    while remaining:
        seed = min(remaining)
        component, pending = {seed}, [seed]
        while pending:
            current = pending.pop()
            adjacent = set(edges.get(current, ())) | {owner for owner, targets in edges.items() if current in targets}
            for item in adjacent & remaining - component:
                component.add(item)
                pending.append(item)
        remaining -= component
        answer.append(component)
    return tuple(answer)


def _strong_components(vertices, edges):
    vertices = set(vertices)
    visited, order = set(), []
    def forward(vertex):
        visited.add(vertex)
        for target in edges.get(vertex, set()) & vertices - visited:
            forward(target)
        order.append(vertex)
    for vertex in sorted(vertices):
        if vertex not in visited:
            forward(vertex)
    reverse = defaultdict(set)
    for owner in vertices:
        for target in edges.get(owner, set()) & vertices:
            reverse[target].add(owner)
    visited, result = set(), []
    def backward(vertex, component):
        visited.add(vertex)
        component.add(vertex)
        for target in reverse.get(vertex, set()) - visited:
            backward(target, component)
    for vertex in reversed(order):
        if vertex not in visited:
            component = set()
            backward(vertex, component)
            result.append(component)
    return tuple(result)


def _helper_allowed(root, target, index, nodes):
    name = target.rsplit(".", 1)[-1].rsplit("::", 1)[-1]
    if not name.startswith("_") or _TRUE_DUNDER.fullmatch(name):
        return False
    if root.rpartition("::")[0] != target.rpartition("::")[0]:
        return False
    root_parent, target_parent = index.entities[root].parent, index.entities[target].parent
    return (target_parent == root_parent if isinstance(nodes.get(root_parent), ast.ClassDef)
            else not isinstance(nodes.get(target_parent), ast.ClassDef))


def _closure(root, functions, index, nodes, calls):
    edges, callers = defaultdict(set), defaultdict(set)
    for owner, record in calls:
        if isinstance(record, ResolvedCall) and record.target in functions:
            callers[record.target].add(owner)
            if owner in functions:
                edges[owner].add(record.target)
    eligible = {item for item in functions if _helper_allowed(root, item, index, nodes)}
    closure, pending = set(), [root]
    while pending:
        for target in edges.get(pending.pop(), set()) & eligible - closure:
            closure.add(target)
            pending.append(target)
    changed = True
    while changed:
        changed = False
        for component in _strong_components(closure, edges):
            if any(caller not in closure | {root} for member in component for caller in callers.get(member, ())):
                closure -= component
                changed = True
        reachable, pending = set(), [root]
        while pending:
            for target in edges.get(pending.pop(), set()) & closure - reachable:
                reachable.add(target)
                pending.append(target)
        if reachable != closure:
            closure, changed = reachable, True
    return (root, *sorted(closure))


def _fanout(node):
    found = set()
    def visit(member):
        if member is not node and isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(member, ast.Call):
            found.add(ast.dump(member.func, include_attributes=False))
        for child in ast.iter_child_nodes(member):
            visit(child)
    visit(node)
    return found


def _one_hop(root, members, legacy, semantics, calls, nodes, rules):
    semantic = semantics.build_one_hop(root=root, members=members)
    metrics = tuple(legacy[item] for item in members)
    fanout = set().union(*(_fanout(nodes[item]) for item in members))
    resolved = {record.target for owner, record in calls if owner in members and isinstance(record, ResolvedCall)}
    policy, count = rules["kinds"]["one_hop"], len(members) - 1
    threshold = policy["signals"]
    cyclomatic, cognitive = sum(x.cyclomatic for x in metrics), sum(x.cognitive for x in metrics)
    nesting = max((x.max_nesting for x in metrics), default=0)
    signals = MappingProxyType({
        "summed_cyclomatic": cyclomatic >= threshold["summed_cyclomatic"],
        "summed_cognitive": cognitive >= threshold["summed_cognitive"],
        "nesting": nesting >= threshold["nesting"],
        "legacy_syntactic_fanout_union": len(fanout) >= threshold["legacy_syntactic_fanout_union"],
        "domain_mixing": len(semantic.propagated_domains) >= threshold["domain_mixing"],
        "lifecycle_mixing": len(semantic.propagated_lifecycle_clusters) >= threshold["lifecycle_mixing"],
    })
    hard = tuple(name for name in policy["hard"] if count > policy["hard"][name])
    score, candidate = _decision("one_hop", signals, hard, rules)
    return OneHopMetrics(root, members, count, cyclomatic, cognitive, nesting, len(fanout),
        len(resolved), semantic.propagated_domains, semantic.propagated_transitions,
        semantic.propagated_lifecycle_clusters, signals, score, hard, candidate)


def _scoped_nodes(root):
    found = []
    def visit(node, parent=None):
        found.append((node, parent))
        for child in ast.iter_child_nodes(node):
            if child is not root and isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            visit(child, node)
    visit(root)
    return tuple(found)


def _field_attributes(scoped):
    fields, mutable = set(), set()
    for member, parent in scoped:
        if (isinstance(member, ast.Attribute) and isinstance(member.value, ast.Name)
                and member.value.id in {"self", "cls"}):
            if not (isinstance(parent, ast.Call) and parent.func is member):
                fields.add(member.attr)
            if isinstance(member.ctx, (ast.Store, ast.Del)):
                mutable.add(member.attr)
    return fields, mutable


def _field_aliases(scoped):
    stores = defaultdict(int)
    aliases = {}
    for member, _parent in scoped:
        if isinstance(member, ast.Name) and isinstance(member.ctx, ast.Store):
            stores[member.id] += 1
        if not isinstance(member, ast.Assign) or len(member.targets) != 1:
            continue
        target, value = member.targets[0], member.value
        if (isinstance(target, ast.Name) and isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name) and value.value.id in {"self", "cls"}):
            aliases[target.id] = value.attr
    return {name: field for name, field in aliases.items() if stores[name] == 1}


def _direct_field_evidence(scoped):
    fields, mutable = _field_attributes(scoped)
    aliases = _field_aliases(scoped)
    return fields, mutable, aliases


def _mutated_alias_fields(scoped, aliases):
    mutable = set()
    for member, _parent in scoped:
        if not isinstance(member, ast.Call) or not isinstance(member.func, ast.Attribute):
            continue
        receiver = member.func.value
        field = (receiver.attr if isinstance(receiver, ast.Attribute)
                 and isinstance(receiver.value, ast.Name) and receiver.value.id in {"self", "cls"}
                 else aliases.get(receiver.id) if isinstance(receiver, ast.Name) else None)
        if field is not None and member.func.attr in _MUTATORS:
            mutable.add(field)
    return mutable


def _field_evidence(node):
    scoped = _scoped_nodes(node)
    fields, mutable, aliases = _direct_field_evidence(scoped)
    mutable |= _mutated_alias_fields(scoped, aliases)
    return fields, mutable


def _class_lambdas(identity, node, index):
    answer = []
    for member in ast.walk(node):
        if not isinstance(member, ast.Lambda):
            continue
        evidence = (identity.rpartition("::")[0], member.lineno, member.col_offset,
                    member.end_lineno, member.end_col_offset)
        if index.lambda_owners.get(evidence) == identity:
            answer.append(member)
    return tuple(answer)


def _lambda_call_owners(identity, labels, lambdas):
    owners = {}
    for label, node in zip(labels, lambdas):
        for member, _parent in _scoped_nodes(node):
            if isinstance(member, ast.Call):
                owners[(member.lineno, member.col_offset)] = f"{identity}.{label}"
    return owners


def _cohesion(identity, vertices, calls, resolutions, labels, lambdas):
    edges = defaultdict(set)
    names = tuple(vertices)
    for position, left in enumerate(names):
        for right in names[position + 1:]:
            if vertices[left] & vertices[right]:
                edges[left].add(right)
    lambda_owners = _lambda_call_owners(identity, labels, lambdas)
    for owner, record in calls:
        if not isinstance(record, ResolvedCall) or record.target not in vertices:
            continue
        source = owner
        if owner == identity:
            evidence = resolutions.call_evidence.get(record.callsite)
            if evidence is not None:
                source = lambda_owners.get((evidence.line, evidence.column), owner)
        if source in vertices:
            edges[source].add(record.target)
    return len(_components(vertices, edges))


def _class_semantics(identity, methods, semantics):
    members = (identity, *methods)
    transitions_order, clusters_order = _vocabulary()
    domains = _ordered(_DOMAINS, *(semantics.entities[item].propagated_domains
                                    for item in members))
    transitions = _ordered(transitions_order,
        *(semantics.entities[item].propagated_transitions for item in members))
    clusters = _ordered(clusters_order,
        *(semantics.entities[item].propagated_lifecycle_clusters for item in members))
    return domains, transitions, clusters


def _class(identity, node, index, legacy, semantics, resolutions, calls, nodes, rules):
    methods = tuple(item for item in index.entities if index.entities[item].parent == identity and item in legacy)
    vertices, mutable = {}, set()
    for method in methods:
        fields, changed = _field_evidence(nodes[method])
        vertices[method], mutable = fields, mutable | changed
    labels = index.class_lambda_evidence.get(identity, ())
    lambdas = _class_lambdas(identity, node, index)
    for label, lambda_node in zip(labels, lambdas):
        fields, changed = _field_evidence(lambda_node)
        vertices[f"{identity}.{label}"], mutable = fields, mutable | changed
    cohesion = _cohesion(identity, vertices, calls, resolutions, labels, lambdas)
    bases = tuple(dict.fromkeys(record.target for record in resolutions.dependencies.values()
        if isinstance(record, ResolvedDependency) and record.owner == identity
        and record.kind == "base" and record.target in index.entities))
    domains, transitions, clusters = _class_semantics(identity, methods, semantics)
    policy = rules["kinds"]["class"]
    threshold = policy["signals"]
    public = sum(not item.rsplit(".", 1)[-1].startswith("_") for item in methods)
    signals = MappingProxyType({
        "method_count": len(methods) >= threshold["method_count"],
        "public_method_count": public >= threshold["public_method_count"],
        "mutable_field_count": len(mutable) >= threshold["mutable_field_count"],
        "cohesion_components": cohesion >= threshold["cohesion_components"],
        "domain_mixing": len(domains) >= threshold["domain_mixing"],
        "lifecycle_mixing": len(clusters) >= threshold["lifecycle_mixing"],
    })
    hard = tuple(name for name, value in (("method_count_gt_24", len(methods)),
        ("mutable_field_count_gt_24", len(mutable))) if value > policy["hard"][name])
    score, candidate = _decision("class", signals, hard, rules)
    fields = tuple(f"self.{name}" for name in sorted(mutable))
    return ClassMetrics(len(methods), public, fields, len(fields), cohesion, bases,
        domains, transitions, clusters, signals, score, hard, candidate)


def _top(identity, path, index):
    file_owner, current = f"{path}::@file", identity
    while current in index.entities and index.entities[current].parent != file_owner:
        current = index.entities[current].parent
    return current if current in index.entities else None


def _subsystems(path, index):
    labels = set()
    for record in index.imports.values():
        if record.identity.rpartition("::")[0] != path:
            continue
        for target in record.targets:
            parts = target.lstrip(".").split(".")
            if parts and parts[0]:
                labels.add(parts[1] if parts[0] == "lockstep" and len(parts) > 1 else parts[0])
    return tuple(sorted(labels))


def _import_bindings(module):
    bindings = {}
    for node in module.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] = "@import:" + alias.name
        elif isinstance(node, ast.ImportFrom):
            module_name = "." * node.level + (node.module or "")
            for alias in node.names:
                bindings[alias.asname or alias.name] = "@import:" + module_name + "." + alias.name
    return bindings


def _module_reference_bindings(path, index, nodes, vertices, resolutions):
    module = ast.parse(index.files[path], filename=path)
    bindings = {nodes[item].name: item for item in vertices}
    bindings.update(_import_bindings(module))
    prefix = f"{path}::@file::"
    for binding, target in resolutions.aliases.items():
        if binding.startswith(prefix):
            bindings[binding.removeprefix(prefix)] = target
    return bindings


def _scope_name(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    return {ast.Lambda: "lambda", ast.ListComp: "listcomp",
            ast.SetComp: "setcomp", ast.DictComp: "dictcomp",
            ast.GeneratorExp: "genexpr"}.get(type(node))


def _child_table(table, node):
    name = _scope_name(node)
    matches = tuple(child for child in table.get_children()
                    if child.get_name() == name and child.get_lineno() == node.lineno)
    return matches[0] if matches else table


def _module_loads(root, bindings, module_table):
    found = set()
    scope_nodes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                   ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    def visit(node, table):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            try:
                symbol = table.lookup(node.id)
            except KeyError:
                symbol = None
            if (table.get_type() == "module" or symbol is None or symbol.is_global()) and node.id in bindings:
                found.add(bindings[node.id])
            return
        child_table = _child_table(table, node) if isinstance(node, scope_nodes) else table
        for child in ast.iter_child_nodes(node):
            visit(child, child_table)
    visit(root, module_table)
    return found


def _reference_edges(path, index, nodes, vertices, resolutions):
    bindings = _module_reference_bindings(path, index, nodes, vertices, resolutions)
    module_table = symtable.symtable(index.files[path].decode("utf-8"), path, "exec")
    edges, external = defaultdict(set), defaultdict(set)
    for owner in vertices:
        for target in _module_loads(nodes[owner], bindings, module_table):
            if target in vertices and target != owner:
                edges[owner].add(target)
            elif isinstance(target, str) and target.startswith("@import:"):
                external[target].add(owner)
    return edges, external


def _file(path, index, nodes, semantics, resolutions, calls, rules):
    identities = tuple(item for item in index.entities if item.rpartition("::")[0] == path)
    vertices = {item for item in identities if index.entities[item].parent == f"{path}::@file"}
    edges, external = _reference_edges(path, index, nodes, vertices, resolutions)
    for owner, record in calls:
        source = _top(owner, path, index)
        if source not in vertices or not isinstance(record, ResolvedCall):
            continue
        target = _top(record.target, path, index)
        if target in vertices and target != source:
            edges[source].add(target)
        elif target is None:
            external[record.target].add(source)
    for owners in external.values():
        ordered = sorted(owners)
        for owner in ordered[1:]:
            edges[ordered[0]].add(owner)
    for record in resolutions.dependencies.values():
        if isinstance(record, ResolvedDependency):
            source, target = _top(record.owner, path, index), _top(record.target, path, index)
            if source in vertices and target in vertices and source != target:
                edges[source].add(target)
    components, subsystems = len(_components(vertices, edges)), _subsystems(path, index)
    semantic, policy = semantics.files[f"{path}::@file"], rules["kinds"]["file"]
    class_count = sum(isinstance(nodes[item], ast.ClassDef) for item in identities)
    threshold = policy["signals"]
    signals = MappingProxyType({
        "definition_count": len(identities) >= threshold["definition_count"],
        "class_count": class_count >= threshold["class_count"],
        "subsystem_import_count": len(subsystems) >= threshold["subsystem_import_count"],
        "definition_dependency_components": components >= threshold["definition_dependency_components"],
        "domain_mixing": len(semantic.propagated_domains) >= threshold["domain_mixing"],
        "lifecycle_mixing": len(semantic.propagated_lifecycle_clusters) >= threshold["lifecycle_mixing"],
    })
    hard = tuple(name for name in policy["hard"] if len(identities) > policy["hard"][name])
    score, candidate = _decision("file", signals, hard, rules)
    return FileMetrics(len(identities), class_count, subsystems, len(subsystems), components,
        semantic.propagated_domains, semantic.propagated_transitions,
        semantic.propagated_lifecycle_clusters, signals, score, hard, candidate)


def evaluate_candidates(index, legacy, semantics, resolutions):
    if (not isinstance(index, SourceIndex) or not isinstance(legacy, Mapping)
            or not isinstance(semantics, SemanticIndex) or not isinstance(resolutions, ResolutionIndex)):
        raise TypeError("invalid candidate policy inputs")
    rules, nodes, calls = _rules(), _nodes(index), _calls(resolutions)
    functions = MappingProxyType({identity: _function(identity, metric,
        semantics.entities[identity], calls, rules) for identity, metric in legacy.items()})
    function_ids = set(legacy)
    one_hops = MappingProxyType({root + "::@one_hop": _one_hop(root,
        _closure(root, function_ids, index, nodes, calls), legacy, semantics, calls,
        nodes, rules) for root in legacy})
    classes = MappingProxyType({identity: _class(identity, node, index, legacy,
        semantics, resolutions, calls, nodes, rules) for identity, node in nodes.items()
        if isinstance(node, ast.ClassDef)})
    files = MappingProxyType({f"{path}::@file": _file(path, index, nodes, semantics,
        resolutions, calls, rules) for path in index.files})
    unresolved = tuple(record.callsite for record in resolutions.calls.values()
                       if isinstance(record, UnresolvedCall))
    inputs = semantics.digest_inputs
    return ArchitectureReport(functions, one_hops, classes, files, unresolved,
        inputs.allowlist_digest, semantics.primitive_digest, semantics.lifecycle_digest,
        inputs.schema_digest, inputs.threshold_digest, inputs.analyzer_version, inputs.rule_version)
