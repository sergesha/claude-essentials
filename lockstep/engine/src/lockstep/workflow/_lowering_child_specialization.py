"""Specialize compiled child-workflow state, descriptors, nodes, and edges."""

from __future__ import annotations

import hashlib
from typing import Any

from lockstep.runtime.effects.descriptors import parse_effect_descriptor

from ._lowering_conditions import _rewrite_condition_references
from ._lowering_identity import (
    _fragment_state_namespace,
    _specialized_state_key,
)
from .canonical import canonical_yaml, plain


def _edge_targets(edge: dict[str, Any]) -> tuple[Any, ...]:
    targets = edge.get("to")
    return tuple(targets) if isinstance(targets, list) else (targets,)


def _specialized_fragment_digest(
    original: dict[str, Any],
    specialized: dict[str, Any],
    expected_digest: str,
    call_namespace: str,
) -> str | None:
    """Recover and re-digest an exact compiler-owned fragment projection."""
    original_nodes = original.get("nodes", {})
    original_state = original.get("state", {})
    original_edges = original.get("edges", [])
    if not isinstance(original_nodes, dict) or not isinstance(original_state, dict):
        return None
    candidates: set[str] = set()
    for node_name in original_nodes:
        if not isinstance(node_name, str):
            continue
        dots = [index for index, character in enumerate(node_name) if character == "."]
        candidates.update(node_name[:index] for index in dots)
    for fragment_namespace in sorted(candidates):
        node_prefix = fragment_namespace + "."
        state_prefix = f"fragment_{_fragment_state_namespace(fragment_namespace)}_"
        state_names = {
            key
            for key in original_state
            if isinstance(key, str) and key.startswith(state_prefix)
        }
        node_names = {
            key
            for key in original_nodes
            if isinstance(key, str) and key.startswith(node_prefix)
        }
        projection = {
            "state": {
                key: original_state[key] for key in original_state if key in state_names
            },
            "nodes": {
                key: original_nodes[key] for key in original_nodes if key in node_names
            },
            "edges": [
                edge
                for edge in original_edges
                if isinstance(edge, dict)
                and (
                    edge.get("from") in node_names
                    or any(target in node_names for target in _edge_targets(edge))
                )
            ],
        }
        if hashlib.sha256(canonical_yaml(projection)).hexdigest() != expected_digest:
            continue
        specialized_state = specialized.get("state", {})
        specialized_nodes = specialized.get("nodes", {})
        specialized_edges = specialized.get("edges", [])
        mapped_state = {
            _specialized_state_key(call_namespace, key) for key in state_names
        }
        mapped_nodes = {f"{call_namespace}.{key}" for key in node_names}
        transformed = {
            "state": {
                key: specialized_state[key]
                for key in specialized_state
                if key in mapped_state
            },
            "nodes": {
                key: specialized_nodes[key]
                for key in specialized_nodes
                if key in mapped_nodes
            },
            "edges": [
                edge
                for edge in specialized_edges
                if isinstance(edge, dict)
                and (
                    edge.get("from") in mapped_nodes
                    or any(target in mapped_nodes for target in _edge_targets(edge))
                )
            ],
        }
        return hashlib.sha256(canonical_yaml(transformed)).hexdigest()
    return None


def _rewrite_child_state_template(
    value: Any,
    key_map: dict[str, str],
) -> Any:
    if isinstance(value, dict):
        return {
            key: _rewrite_child_state_template(item, key_map)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_child_state_template(item, key_map) for item in value]
    if not isinstance(value, str):
        return value
    rewritten = value
    for original, qualified in sorted(
        key_map.items(), key=lambda item: len(item[0]), reverse=True
    ):
        rewritten = rewritten.replace(f"{{state.{original}", f"{{state.{qualified}")
    return rewritten


def _specialized_child_state(
    state: dict[str, Any],
    *,
    child_contract: Any,
    namespace: str,
    scope_key: str,
    child_outcome: str,
    reserved_channels: frozenset[str],
) -> tuple[dict[str, str], dict[str, Any]]:
    key_map = {key: _specialized_state_key(namespace, key) for key in tuple(state)}
    collisions = {
        key: qualified
        for key, qualified in key_map.items()
        if key != "lockstep_outcome" and qualified in reserved_channels
    }
    if collisions:
        raise ValueError(
            "child state specialization collides with compiler-reserved "
            f"call channels: {sorted(collisions)}"
        )
    key_map["lockstep_outcome"] = child_outcome
    key_map[scope_key] = scope_key
    for key in {*child_contract.state_inputs, *child_contract.state_exports}:
        key_map[key] = _specialized_state_key(namespace, key)

    new_state = {key_map[key]: value for key, value in state.items()}
    new_state[scope_key] = "dict"
    for key, state_type in {
        **dict(child_contract.state_inputs),
        **dict(child_contract.state_exports),
    }.items():
        new_state[key_map[key]] = state_type
    return key_map, new_state


def _descriptor_matches_artifact(
    *,
    logical_id: object,
    node_resume_key: object,
    namespace: str,
    key_map: dict[str, str],
    artifact_bindings: tuple[tuple[str, str, str, str, str, str], ...],
) -> bool:
    return any(
        key_map.get(item[4], _specialized_state_key(namespace, item[4]))
        == node_resume_key
        and logical_id == item[5]
        for item in artifact_bindings
    )


def _specialize_descriptor_logical_id(
    descriptor: dict[str, Any], namespace: str
) -> None:
    logical_id = descriptor.get("logical_id")
    if isinstance(logical_id, str):
        logical_digest = hashlib.sha256(
            b"lockstep.specialized-logical-id/v1\0"
            + namespace.encode("ascii")
            + b"\0"
            + logical_id.encode("utf-8")
        ).hexdigest()
        descriptor["logical_id"] = f"child-{logical_digest}"


def _specialize_descriptor_runner(
    descriptor: dict[str, Any],
    *,
    runner: str,
    scope_key: str,
    key_map: dict[str, str],
) -> None:
    if descriptor.get("kind") == "manual" and descriptor.get("runner") is None:
        descriptor["kind"] = "managed"
        descriptor["runner"] = {
            "selector": runner,
            "required_capabilities": ["workspace", "bounded_result", "sandbox"],
        }
        descriptor["scope_state_keys"] = [scope_key]
    elif isinstance(descriptor.get("scope_state_keys"), list):
        mapped_scopes = [
            key_map.get(key, key) for key in descriptor["scope_state_keys"]
        ]
        descriptor["scope_state_keys"] = mapped_scopes if mapped_scopes else [scope_key]


def _specialize_descriptor_inputs(
    descriptor: dict[str, Any],
    *,
    namespace: str,
    key_map: dict[str, str],
) -> None:
    inputs = descriptor.get("inputs")
    if isinstance(inputs, dict):
        for selector in inputs.values():
            if isinstance(selector, dict) and isinstance(
                selector.get("state_key"), str
            ):
                state_key = selector["state_key"]
                selector["state_key"] = key_map.get(
                    state_key, _specialized_state_key(namespace, state_key)
                )


def _specialize_scope_descriptor(
    descriptor: dict[str, Any],
    *,
    namespace: str,
    scope_key: str,
    key_map: dict[str, str],
) -> None:
    if descriptor.get("kind") == "scope":
        ancestors = [
            key_map.get(key, key)
            for key in descriptor.get("ancestor_deadline_state_keys", [])
        ]
        descriptor["ancestor_deadline_state_keys"] = [scope_key, *ancestors]
        result_key = descriptor.get("result_state_key")
        if isinstance(result_key, str):
            descriptor["result_state_key"] = key_map.get(
                result_key, _specialized_state_key(namespace, result_key)
            )


def _specialize_child_descriptor(
    raw_descriptor: dict[str, Any],
    *,
    namespace: str,
    runner: str,
    scope_key: str,
    key_map: dict[str, str],
    new_state: dict[str, Any],
    node_resume_key: object,
    artifact_bindings: tuple[tuple[str, str, str, str, str, str], ...],
    inside_parallel_branch: bool,
) -> tuple[dict[str, Any], bool]:
    descriptor = plain(raw_descriptor)
    if inside_parallel_branch and descriptor.get("kind") == "decide":
        raise ValueError("parallel child may not hide a decision descriptor")
    matching_artifact = _descriptor_matches_artifact(
        logical_id=descriptor.get("logical_id"),
        node_resume_key=node_resume_key,
        namespace=namespace,
        key_map=key_map,
        artifact_bindings=artifact_bindings,
    )
    _specialize_descriptor_logical_id(descriptor, namespace)
    _specialize_descriptor_runner(
        descriptor, runner=runner, scope_key=scope_key, key_map=key_map
    )
    _specialize_descriptor_inputs(descriptor, namespace=namespace, key_map=key_map)
    _specialize_scope_descriptor(
        descriptor,
        namespace=namespace,
        scope_key=scope_key,
        key_map=key_map,
    )
    parse_effect_descriptor(descriptor, known_state_keys=set(new_state))
    return descriptor, matching_artifact


def _specialize_child_node(
    raw_node: object,
    *,
    namespace: str,
    runner: str,
    scope_key: str,
    key_map: dict[str, str],
    new_state: dict[str, Any],
    artifact_bindings: tuple[tuple[str, str, str, str, str, str], ...],
    inside_parallel_branch: bool,
) -> dict[str, Any]:
    if not isinstance(raw_node, dict):
        raise ValueError("resolved child node must be a mapping")  # noqa: TRY004
    node = plain(raw_node)
    output = node.get("output")
    if isinstance(output, dict):
        node["output"] = {
            key_map.get(key, key): _rewrite_child_state_template(value, key_map)
            for key, value in output.items()
        }
    for field in ("state_key", "resume_key"):
        value = node.get(field)
        if isinstance(value, str):
            node[field] = key_map.get(value, _specialized_state_key(namespace, value))
            new_state.setdefault(node[field], "dict")
    message = node.get("message")
    descriptor = message.get("lockstep_effect") if isinstance(message, dict) else None
    if isinstance(descriptor, dict):
        rewritten, matching_artifact = _specialize_child_descriptor(
            descriptor,
            namespace=namespace,
            runner=runner,
            scope_key=scope_key,
            key_map=key_map,
            new_state=new_state,
            node_resume_key=node.get("resume_key"),
            artifact_bindings=artifact_bindings,
            inside_parallel_branch=inside_parallel_branch,
        )
        if matching_artifact:
            message["artifact_contract"] = {}
        message["lockstep_effect"] = rewritten
    if isinstance(message, dict):
        for message_key, message_value in tuple(message.items()):
            if message_key != "lockstep_effect":
                message[message_key] = _rewrite_child_state_template(
                    message_value, key_map
                )
    return node


def _specialize_child_edges(
    raw_edges: object,
    *,
    node_map: dict[str, str],
    key_map: dict[str, str],
) -> list[dict[str, Any]]:
    rewritten_edges: list[dict[str, Any]] = []
    for raw_edge in raw_edges:
        edge = plain(raw_edge)
        source = edge.get("from")
        if source not in {"START", "END"}:
            edge["from"] = node_map[source]
        targets = edge.get("to")
        if isinstance(targets, list):
            edge["to"] = [
                target if target in {"START", "END"} else node_map[target]
                for target in targets
            ]
        elif targets not in {"START", "END"}:
            edge["to"] = node_map[targets]
        if "condition" in edge:
            edge["condition"] = _rewrite_condition_references(
                edge["condition"], key_map
            )
        rewritten_edges.append(edge)
    return rewritten_edges


def _specialize_child_loops(
    document: dict[str, Any],
    node_map: dict[str, str],
) -> None:
    for field in ("loop_limits", "loop_exits"):
        raw = document.get(field)
        if isinstance(raw, dict):
            document[field] = {
                node_map.get(key, key): (
                    node_map.get(value, value) if field == "loop_exits" else value
                )
                for key, value in raw.items()
            }
