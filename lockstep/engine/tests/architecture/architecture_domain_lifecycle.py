"""Effect-domain and lifecycle propagation for architecture evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType

from architecture_call_resolver import (
    ResolvedCall,
    ResolvedDependency,
    ResolutionIndex,
    UnresolvedCall,
    UnresolvedDependency,
    resolve_calls,
)
from architecture_source_index import ImportRecord, SourceIndex


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DOMAINS = (
    "decode/validate", "planning/transformation", "filesystem-read",
    "filesystem-write", "durable-state", "synchronization",
    "external-process/provider", "authority/commitment", "lifecycle-control",
    "projection/output",
)
_TRANSITIONS = (
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
    ("process-execution", "process.indeterminate", ("launching", "running"), "indeterminate"),
    ("process-execution", "process.cancel", ("prepared", "launching", "running"), "cancelled"),
    ("artifact/acceptance", "artifact.register", ("declared",), "registered"),
    ("artifact/acceptance", "artifact.materialize", ("registered",), "materialized"),
    ("artifact/acceptance", "consent.issue", ("pending",), "issued"),
    ("artifact/acceptance", "consent.redeem", ("issued",), "redeemed"),
    ("publication", "publication.prepare", ("absent",), "prepared"),
    ("publication", "publication.apply", ("prepared",), "applied"),
    ("publication", "publication.rollback", ("prepared",), "rolled-back"),
    ("delivery", "delivery.pending", ("absent",), "pending"),
    ("delivery", "delivery.deliver", ("pending",), "delivered"),
    ("recovery/watch", "recovery.claim", ("eligible",), "claimed"),
    ("recovery/watch", "recovery.defer", ("claimed",), "eligible"),
    ("recovery/watch", "recovery.acknowledge", ("claimed",), "acknowledged"),
    ("authoring-publication", "authoring.plan", ("absent",), "planned"),
    ("authoring-publication", "authoring.replace", ("planned",), "replaced"),
    ("authoring-publication", "authoring.directory-durable", ("replaced",), "directory-durable"),
)
_TRANSITION_ORDER = tuple(item[1] for item in _TRANSITIONS)
_CLUSTER_ORDER = tuple(dict.fromkeys(item[0] for item in _TRANSITIONS))
_TYPE_LABELS = {type(None): "null", bool: "bool", int: "int", str: "str"}


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _checked_digest(value: str, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class SemanticDigestInputs:
    allowlist_digest: str
    schema_digest: str
    threshold_digest: str
    analyzer_version: str
    rule_version: str

    def __post_init__(self) -> None:
        for name in ("allowlist_digest", "schema_digest", "threshold_digest"):
            _checked_digest(getattr(self, name), name)
        for name in ("analyzer_version", "rule_version"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty version")


@dataclass(frozen=True, slots=True)
class EntitySemantics:
    identity: str
    direct_domains: tuple[str, ...]
    propagated_domains: tuple[str, ...]
    direct_transitions: tuple[str, ...]
    propagated_transitions: tuple[str, ...]
    propagated_lifecycle_clusters: tuple[str, ...]
    semantic_dependency_sha256: str


@dataclass(frozen=True, slots=True)
class FileSemantics:
    identity: str
    propagated_domains: tuple[str, ...]
    propagated_transitions: tuple[str, ...]
    propagated_lifecycle_clusters: tuple[str, ...]
    semantic_dependency_sha256: str


@dataclass(frozen=True, slots=True)
class OneHopSemantics:
    identity: str
    root: str
    members: tuple[str, ...]
    propagated_domains: tuple[str, ...]
    propagated_transitions: tuple[str, ...]
    propagated_lifecycle_clusters: tuple[str, ...]
    semantic_dependency_sha256: str


@dataclass(frozen=True, slots=True)
class SemanticIndex:
    entities: Mapping[str, EntitySemantics]
    files: Mapping[str, FileSemantics]
    primitive_digest: str
    lifecycle_digest: str
    digest_inputs: SemanticDigestInputs

    def build_one_hop(self, *, root: str, members: Sequence[str]) -> OneHopSemantics:
        checked = tuple(members)
        _require(bool(checked), "one-hop members must be non-empty")
        _require(checked[0] == root, "one-hop root must be first")
        _require(len(set(checked)) == len(checked), "duplicate one-hop member")
        _require(all(item in self.entities for item in checked), "unknown one-hop member")
        root_path = root.rpartition("::")[0]
        _require(all(item.rpartition("::")[0] == root_path for item in checked),
                 "mixed-file one-hop members")
        _require(checked[1:] == tuple(sorted(checked[1:])),
                 "one-hop members are not stable-identity sorted")
        domains = _ordered_union(_DOMAINS,
            *(self.entities[item].propagated_domains for item in checked))
        transitions = _ordered_union(_TRANSITION_ORDER,
            *(self.entities[item].propagated_transitions for item in checked))
        clusters = _ordered_union(_CLUSTER_ORDER,
            *(self.entities[item].propagated_lifecycle_clusters for item in checked))
        identity = root + "::@one_hop"
        payload = {
            "schema": "lockstep.architecture-one-hop-semantics/v1",
            "identity": identity, "root": root,
            "members": [{"identity": item, "semantic_dependency_sha256":
                         self.entities[item].semantic_dependency_sha256}
                        for item in checked],
            "propagated_domains": list(domains),
            "propagated_transitions": list(transitions),
            "propagated_lifecycle_clusters": list(clusters),
            "rule_inputs": _rule_inputs(self.digest_inputs, self.primitive_digest,
                                        self.lifecycle_digest),
        }
        return OneHopSemantics(identity, root, checked, domains, transitions,
                               clusters, _digest(payload))


def _resolve_and_propagate(index, allowlist, primitives, lifecycle, digest_inputs):
    resolutions = resolve_calls(index, allowlist, primitives)
    semantics = propagate_semantics(
        index, resolutions, primitives, lifecycle, digest_inputs=digest_inputs)
    return resolutions, semantics


def _ordered_union(order: Sequence[str], *values: Sequence[str]) -> tuple[str, ...]:
    present = {item for collection in values for item in collection}
    return tuple(item for item in order if item in present)


def _rule_inputs(inputs: SemanticDigestInputs, primitive_digest: str,
                 lifecycle_digest: str) -> dict[str, str]:
    return {"allowlist_digest": inputs.allowlist_digest,
            "primitive_digest": primitive_digest,
            "lifecycle_digest": lifecycle_digest,
            "schema_digest": inputs.schema_digest,
            "threshold_digest": inputs.threshold_digest,
            "analyzer_version": inputs.analyzer_version,
            "rule_version": inputs.rule_version}


def _validate_primitives(index: SourceIndex, resolutions: ResolutionIndex,
                         value: object):
    _require(isinstance(value, Mapping), "invalid primitive envelope")
    _require(set(value) == {"schema_version", "reference_source_sha256",
                            "callsite_evidence", "rows"},
             "invalid primitive envelope keys")
    rows = resolutions.validate_primitives(index, value)
    expected = tuple(sorted(rows, key=lambda row:
                            (str(row["selector_kind"]), str(row["selector"]))))
    _require(rows == expected, "noncanonical primitive row order")
    for row in rows:
        selector = str(row["selector"])
        target = str(row["semantic_target"])
        if row["selector_kind"] == "callsite":
            _require(selector in resolutions.calls, "orphan primitive callsite")
            record = resolutions.calls[selector]
            _require(isinstance(record, ResolvedCall)
                     and record.target == target, "stale primitive semantic target")
        else:
            _require(selector == target, "stale primitive semantic target")
            used = selector in index.entities or any(
                isinstance(record, ResolvedCall) and record.target == selector
                for record in resolutions.calls.values())
            _require(used, "orphan primitive entity")
    return rows, _digest(value), str(value["reference_source_sha256"])


def _validate_transition_record(record: object, expected: tuple[object, ...]) -> None:
    _require(isinstance(record, Mapping), "invalid lifecycle transition")
    _require(set(record) == {"cluster", "transition_id", "from", "to"},
             "invalid lifecycle transition keys")
    cluster, transition_id, from_states, to_state = expected
    _require(record == {"cluster": cluster, "transition_id": transition_id,
                        "from": list(from_states), "to": to_state},
             "lifecycle transition vocabulary drift")


def _literal_entry(entry: object, *, positional: bool) -> tuple[object, ...]:
    keys = {"index", "type", "value"} if positional else {"name", "type", "value"}
    _require(isinstance(entry, Mapping) and set(entry) == keys, "invalid literal entry")
    label, value = entry["type"], entry["value"]
    _require(label in _TYPE_LABELS.values(), "invalid literal type")
    _require(_TYPE_LABELS.get(type(value)) == label, "literal JSON type mismatch")
    binding = entry["index"] if positional else entry["name"]
    if positional:
        _require(type(binding) is int and binding >= 0, "invalid positional index")
    else:
        _require(isinstance(binding, str) and binding, "invalid keyword name")
    return binding, label, value


def _validate_discriminant(value: object):
    _require(isinstance(value, Mapping), "invalid lifecycle discriminant")
    _require(set(value) == {"kind", "positional", "keywords"},
             "invalid lifecycle discriminant keys")
    _require(value["kind"] == "literal-arguments", "invalid lifecycle discriminant kind")
    _require(isinstance(value["positional"], list)
             and isinstance(value["keywords"], list), "invalid literal arrays")
    positional = tuple(_literal_entry(item, positional=True)
                       for item in value["positional"])
    keywords = tuple(_literal_entry(item, positional=False)
                     for item in value["keywords"])
    _require(bool(positional or keywords), "empty literal discriminant")
    _require(tuple(item[0] for item in positional)
             == tuple(sorted({item[0] for item in positional})),
             "noncanonical positional literal order")
    _require(tuple(item[0] for item in keywords)
             == tuple(sorted({item[0] for item in keywords})),
             "noncanonical keyword literal order")
    return positional, keywords


def _validate_lifecycle(value: object, index: SourceIndex,
                        resolutions: ResolutionIndex):
    _require(isinstance(value, Mapping), "invalid lifecycle object")
    _require(set(value) == {"schema", "transitions", "rows"},
             "invalid lifecycle keys")
    _require(value["schema"] == "lockstep.architecture-lifecycle/v1",
             "invalid lifecycle schema")
    transitions = value["transitions"]
    _require(isinstance(transitions, list)
             and len(transitions) == len(_TRANSITIONS), "invalid lifecycle transitions")
    for record, expected in zip(transitions, _TRANSITIONS, strict=True):
        _validate_transition_record(record, expected)
    _require(isinstance(value["rows"], list), "lifecycle rows must be an array")
    rows = tuple(_validate_lifecycle_row(row, index, resolutions)
                 for row in value["rows"])
    sort_keys = tuple((str(row["binding_kind"]), str(row["binding"]),
                       str(row["target"]), _canonical(row["discriminant"]),
                       str(row["transition_id"])) for row in rows)
    _require(sort_keys == tuple(sorted(sort_keys)), "noncanonical lifecycle row order")
    bindings = tuple((row["binding_kind"], row["binding"]) for row in rows)
    _require(len(set(bindings)) == len(bindings), "duplicate lifecycle binding")
    return rows, _digest(value)


def _validate_lifecycle_row(row: object, index: SourceIndex,
                            resolutions: ResolutionIndex):
    keys = {"binding_kind", "binding", "target", "discriminant", "transition_id"}
    _require(isinstance(row, Mapping) and set(row) == keys, "invalid lifecycle row")
    kind, binding, target = row["binding_kind"], row["binding"], row["target"]
    _require(kind in {"entity", "callsite"}, "invalid lifecycle binding kind")
    _require(isinstance(binding, str) and binding, "invalid lifecycle binding")
    _require(isinstance(target, str) and target, "invalid lifecycle target")
    _require(row["transition_id"] in _TRANSITION_ORDER, "unknown lifecycle transition")
    if kind == "entity":
        _require(target == binding, "lifecycle entity target mismatch")
        _require(row["discriminant"] == {"kind": "none"}, "invalid entity discriminant")
        external_used = any(isinstance(record, ResolvedCall) and record.target == binding
                            for record in resolutions.calls.values())
        _require(binding in index.entities or external_used, "orphan lifecycle entity")
        return dict(row)
    _require(binding in resolutions.calls, "orphan lifecycle callsite")
    record = resolutions.calls[binding]
    _require(isinstance(record, ResolvedCall), "unresolved lifecycle callsite")
    _require(record.target == target, "lifecycle callsite target mismatch")
    positional, keywords = _validate_discriminant(row["discriminant"])
    evidence = resolutions.call_evidence[binding]
    actual_positional = {item.index: (item.index, item.type, item.value)
                         for item in evidence.positional}
    actual_keywords = {item.name: (item.name, item.type, item.value)
                       for item in evidence.keywords}
    _require(all(actual_positional.get(item[0]) == item for item in positional),
             "lifecycle literal positional mismatch")
    _require(all(actual_keywords.get(item[0]) == item for item in keywords),
             "lifecycle literal keyword mismatch")
    return dict(row)


def _owner(callsite: str) -> str:
    return callsite.rsplit("::call:", 1)[0]


def _binding_owners(kind: object, binding: object, index: SourceIndex,
                    resolutions: ResolutionIndex) -> tuple[str, ...]:
    selector = str(binding)
    if kind == "callsite":
        return (_owner(selector),)
    if selector in index.entities:
        return (selector,)
    return tuple(dict.fromkeys(_owner(record.callsite)
        for record in resolutions.calls.values()
        if isinstance(record, ResolvedCall) and record.target == selector))


def _direct_labels(index: SourceIndex, resolutions: ResolutionIndex,
                   primitive_rows: Sequence[Mapping[str, object]],
                   lifecycle_rows: Sequence[Mapping[str, object]]):
    vertices = (*index.entities, *(f"{path}::@file" for path in index.files))
    domains = {identity: set() for identity in vertices}
    transitions = {identity: set() for identity in vertices}
    for row in primitive_rows:
        for owner in _binding_owners(row["selector_kind"], row["selector"],
                                     index, resolutions):
            domains[owner].update(row["domains"])
    for row in lifecycle_rows:
        for owner in _binding_owners(row["binding_kind"], row["binding"],
                                     index, resolutions):
            transitions[owner].add(str(row["transition_id"]))
    return domains, transitions


def _propagate(vertices: Sequence[str], resolutions: ResolutionIndex,
               direct_domains: Mapping[str, set[str]],
               direct_transitions: Mapping[str, set[str]]):
    edges = {vertex: set() for vertex in vertices}
    for record in resolutions.calls.values():
        if isinstance(record, ResolvedCall):
            owner = _owner(record.callsite)
            if owner in edges and record.target in edges:
                edges[owner].add(record.target)
    domains = {vertex: set(direct_domains[vertex]) for vertex in vertices}
    transitions = {vertex: set(direct_transitions[vertex]) for vertex in vertices}
    changed = True
    while changed:
        changed = False
        for owner in reversed(tuple(vertices)):
            before = len(domains[owner]), len(transitions[owner])
            for callee in edges[owner]:
                domains[owner].update(domains[callee])
                transitions[owner].update(transitions[callee])
            changed |= before != (len(domains[owner]), len(transitions[owner]))
    return domains, transitions


def _import_payload(record: ImportRecord) -> dict[str, object]:
    return {"identity": record.identity, "owner": record.owner, "kind": record.kind,
            "module": record.module, "level": record.level,
            "aliases": [dict(alias) for alias in record.aliases],
            "targets": list(record.targets), "span_sha256": record.span_sha256,
            "import_semantic_sha256": record.import_semantic_sha256}


def _bindings(values: Mapping[str, str], owner: str) -> list[dict[str, str]]:
    return [{"binding": binding, "target": target}
            for binding, target in sorted(values.items())
            if binding.rsplit("::", 1)[0] == owner]


def _calls(resolutions: ResolutionIndex, owner: str) -> list[dict[str, str]]:
    return [{"callsite": record.callsite, "target": record.target}
            for record in resolutions.calls.values()
            if isinstance(record, ResolvedCall) and _owner(record.callsite) == owner]


def _dependencies(resolutions: ResolutionIndex, owner: str) -> list[dict[str, str]]:
    return [{"reference": record.reference, "owner": record.owner,
             "kind": record.kind, "target": record.target}
            for record in resolutions.dependencies.values()
            if isinstance(record, ResolvedDependency) and record.owner == owner]


def _entity_payload(identity: str, index: SourceIndex, resolutions: ResolutionIndex,
                    direct_domains: Sequence[str], domains: Sequence[str],
                    direct_transitions: Sequence[str], transitions: Sequence[str],
                    clusters: Sequence[str], rule_inputs: Mapping[str, str]):
    return {"schema": "lockstep.architecture-entity-semantics/v1",
            "identity": identity, "source_sha256": index.entities[identity].span.sha256,
            "imports": [_import_payload(record) for record in index.imports.values()
                        if record.owner == identity],
            "aliases": _bindings(resolutions.aliases, identity),
            "receivers": _bindings(resolutions.receivers, identity),
            "calls": _calls(resolutions, identity),
            "dependencies": _dependencies(resolutions, identity),
            "containment": [child for child, entity in index.entities.items()
                            if entity.parent == identity],
            "direct_domains": list(direct_domains),
            "propagated_domains": list(domains),
            "direct_transitions": list(direct_transitions),
            "propagated_transitions": list(transitions),
            "propagated_lifecycle_clusters": list(clusters),
            "rule_inputs": dict(rule_inputs)}


def _file_payload(path: str, index: SourceIndex, resolutions: ResolutionIndex,
                  entities: Mapping[str, EntitySemantics], domains: Sequence[str],
                  transitions: Sequence[str], clusters: Sequence[str],
                  rule_inputs: Mapping[str, str]):
    owner = f"{path}::@file"
    return {"schema": "lockstep.architecture-file-semantics/v1",
            "identity": owner, "file_sha256": index.file_sha256[path],
            "definitions": [{"identity": identity,
                "semantic_dependency_sha256": record.semantic_dependency_sha256}
                for identity, record in entities.items()
                if identity.rpartition("::")[0] == path],
            "imports": [{"identity": record.identity,
                "import_semantic_sha256": record.import_semantic_sha256}
                for record in index.imports.values()
                if record.identity.rpartition("::")[0] == path],
            "aliases": _bindings(resolutions.aliases, owner),
            "receivers": _bindings(resolutions.receivers, owner),
            "calls": _calls(resolutions, owner),
            "dependencies": _dependencies(resolutions, owner),
            "propagated_domains": list(domains),
            "propagated_transitions": list(transitions),
            "propagated_lifecycle_clusters": list(clusters),
            "rule_inputs": dict(rule_inputs)}


def _validate_inputs(index: SourceIndex, resolutions: ResolutionIndex,
                     primitive_source: str) -> None:
    _require(isinstance(index, SourceIndex), "index must be SourceIndex")
    _require(isinstance(resolutions, ResolutionIndex), "resolutions must be ResolutionIndex")
    _require(not any(isinstance(record, UnresolvedCall)
                     for record in resolutions.calls.values()),
             "unresolved call blocks semantic propagation")
    _require(not any(isinstance(record, UnresolvedDependency)
                     for record in resolutions.dependencies.values()),
             "unresolved dependency blocks semantic propagation")
    population = [{"path": path, "source_sha256": index.file_sha256[path]}
                  for path in sorted(index.files)]
    expected = _digest(population)
    _require(resolutions.reference_source_sha256 == expected
             and primitive_source == expected, "source population mismatch")


def propagate_semantics(index: SourceIndex, resolutions: ResolutionIndex,
                        primitives: object, lifecycle: object, *,
                        digest_inputs: SemanticDigestInputs) -> SemanticIndex:
    if not isinstance(digest_inputs, SemanticDigestInputs):
        raise TypeError("digest_inputs must be SemanticDigestInputs")
    primitive_rows, primitive_digest, primitive_source = _validate_primitives(
        index, resolutions, primitives)
    _validate_inputs(index, resolutions, primitive_source)
    lifecycle_rows, lifecycle_digest = _validate_lifecycle(lifecycle, index, resolutions)
    vertices = (*index.entities, *(f"{path}::@file" for path in index.files))
    direct_domains, direct_transitions = _direct_labels(
        index, resolutions, primitive_rows, lifecycle_rows)
    domains, transitions = _propagate(vertices, resolutions, direct_domains,
                                      direct_transitions)
    rules = _rule_inputs(digest_inputs, primitive_digest, lifecycle_digest)
    entities: dict[str, EntitySemantics] = {}
    for identity in index.entities:
        direct_d = _ordered_union(_DOMAINS, direct_domains[identity])
        propagated_d = _ordered_union(_DOMAINS, domains[identity])
        direct_t = _ordered_union(_TRANSITION_ORDER, direct_transitions[identity])
        propagated_t = _ordered_union(_TRANSITION_ORDER, transitions[identity])
        clusters = _ordered_union(_CLUSTER_ORDER,
            tuple(cluster for cluster, transition, _from, _to in _TRANSITIONS
                  if transition in transitions[identity]))
        payload = _entity_payload(identity, index, resolutions, direct_d, propagated_d,
                                  direct_t, propagated_t, clusters, rules)
        entities[identity] = EntitySemantics(identity, direct_d, propagated_d,
            direct_t, propagated_t, clusters, _digest(payload))
    files: dict[str, FileSemantics] = {}
    for path in index.files:
        owner = f"{path}::@file"
        contained = tuple(identity for identity in index.entities
                          if identity.rpartition("::")[0] == path)
        file_d = _ordered_union(_DOMAINS, domains[owner],
                                *(domains[item] for item in contained))
        file_t = _ordered_union(_TRANSITION_ORDER, transitions[owner],
                                *(transitions[item] for item in contained))
        file_c = _ordered_union(_CLUSTER_ORDER,
            tuple(cluster for cluster, transition, _from, _to in _TRANSITIONS
                  if transition in file_t))
        payload = _file_payload(path, index, resolutions, entities, file_d, file_t,
                                file_c, rules)
        files[owner] = FileSemantics(owner, file_d, file_t, file_c, _digest(payload))
    return SemanticIndex(MappingProxyType(entities), MappingProxyType(files),
                         primitive_digest, lifecycle_digest, digest_inputs)
