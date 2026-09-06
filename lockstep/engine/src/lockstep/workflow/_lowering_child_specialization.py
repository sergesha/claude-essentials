"""Specialize compiled child-workflow state, descriptors, nodes, and edges."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from lockstep.runtime.effects.descriptors import parse_effect_descriptor

from ._lowering_conditions import _rewrite_condition_references
from ._lowering_identity import (
    _fragment_state_namespace,
    _specialized_state_key,
    _stable_id,
)
from .canonical import canonical_yaml, plain


def _edge_targets(edge: dict[str, Any]) -> tuple[Any, ...]:
    targets = edge.get("to")
    return tuple(targets) if isinstance(targets, list) else (targets,)


def _edge_sources(edge: dict[str, Any]) -> tuple[Any, ...]:
    sources = edge.get("from")
    return tuple(sources) if isinstance(sources, list) else (sources,)


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
                    any(source in node_names for source in _edge_sources(edge))
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
                    any(source in mapped_nodes for source in _edge_sources(edge))
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
        descriptor.pop("parallel", None)
        descriptor["runner"] = {
            "selector": runner,
            "required_capabilities": [
                "bounded_result",
                "credentials",
                "network",
                "sandbox",
                "workspace",
            ],
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
) -> tuple[dict[str, Any], bool, str | None]:
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
    if matching_artifact and descriptor.get("artifacts") == []:
        bindings = [
            item
            for item in artifact_bindings
            if key_map.get(item[4], _specialized_state_key(namespace, item[4]))
            == node_resume_key
            and descriptor.get("logical_id") == item[5]
        ]
        descriptor["artifacts"] = [
            {
                "name": binding[1],
                "source_path": binding[2],
                "media_type": binding[3],
                "required": True,
            }
            for binding in bindings
        ]
    managed_logical_id = (
        descriptor.get("logical_id")
        if descriptor.get("kind") == "manual" and descriptor.get("runner") is None
        else None
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
    return descriptor, matching_artifact, managed_logical_id


def _specialize_child_node_state(
    node: dict[str, Any],
    *,
    namespace: str,
    key_map: dict[str, str],
    new_state: dict[str, Any],
) -> None:
    output = node.get("output")
    if isinstance(output, dict):
        node["output"] = {
            key_map.get(key, key): _rewrite_child_state_template(value, key_map)
            for key, value in output.items()
        }
    for field in ("state_key", "resume_key"):
        value = node.get(field)
        if isinstance(value, str):
            node[field] = key_map.get(
                value, _specialized_state_key(namespace, value)
            )
            new_state.setdefault(node[field], "dict")


def _managed_brief_content(message: dict[str, Any], matching_artifact: bool) -> str:
    content = (
        f"Task:\n{message['task']}\n\n"
        f"Exit criterion:\n{message['exit_criterion']}\n"
    )
    artifact = message.get("artifact_contract")
    if not matching_artifact or not isinstance(artifact, dict):
        return content
    markdown = artifact.get("markdown")
    sections = markdown.get("sections") if isinstance(markdown, dict) else None
    if not isinstance(artifact.get("path"), str) or not isinstance(sections, list):
        return content
    return (
        content
        + f"\nArtifact path: {artifact['path']}\n"
        + "Requested Markdown headings: "
        + ", ".join(str(section) for section in sections)
        + "\n"
    )


def _managed_brief_identity(
    original_name: str, namespace: str, managed_logical_id: str
) -> tuple[str, str]:
    state_key = "managed_brief_" + hashlib.sha256(
        b"lockstep.managed-brief/v1\0"
        + namespace.encode("utf-8")
        + b"\0"
        + managed_logical_id.encode("utf-8")
    ).hexdigest()
    match = re.fullmatch(r"step-(.+)-effect-[0-9a-f]{12}", original_name)
    if (
        match is not None
        and _stable_id(f"/flow/{match.group(1)}", "step", "effect") == original_name
    ):
        stable = _stable_id(f"/flow/{match.group(1)}", "step", "managed-brief")
    else:
        digest = hashlib.sha256(
            b"lockstep.managed-brief-node/v1\0" + original_name.encode("utf-8")
        ).hexdigest()[:12]
        stable = f"step-{original_name}-managed-brief-{digest}"
    return state_key, f"{namespace}.{stable}"


def _managed_brief(
    message: dict[str, Any],
    *,
    original_name: str,
    namespace: str,
    managed_logical_id: str | None,
    matching_artifact: bool,
) -> tuple[str, str, str] | None:
    if managed_logical_id is None:
        return None
    if not isinstance(message.get("task"), str) or not isinstance(
        message.get("exit_criterion"), str
    ):
        return None
    state_key, node_name = _managed_brief_identity(
        original_name, namespace, managed_logical_id
    )
    return node_name, state_key, _managed_brief_content(message, matching_artifact)


def _specialize_child_node(
    raw_node: object,
    *,
    original_name: str,
    namespace: str,
    runner: str,
    scope_key: str,
    key_map: dict[str, str],
    new_state: dict[str, Any],
    artifact_bindings: tuple[tuple[str, str, str, str, str, str], ...],
    inside_parallel_branch: bool,
) -> tuple[dict[str, Any], tuple[str, str, str] | None]:
    if not isinstance(raw_node, dict):
        raise ValueError("resolved child node must be a mapping")  # noqa: TRY004
    node = plain(raw_node)
    _specialize_child_node_state(
        node, namespace=namespace, key_map=key_map, new_state=new_state
    )
    message = node.get("message")
    descriptor = message.get("lockstep_effect") if isinstance(message, dict) else None
    if isinstance(descriptor, dict):
        rewritten, matching_artifact, managed_logical_id = _specialize_child_descriptor(
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
        brief = _managed_brief(
            message,
            original_name=original_name,
            namespace=namespace,
            managed_logical_id=managed_logical_id,
            matching_artifact=matching_artifact,
        )
        if brief is not None:
            _brief_name, state_key, _content = brief
            new_state[state_key] = "str"
            rewritten["inputs"] = {
                "brief": {"state_key": state_key},
                "snapshot": {"runtime_key": "current_project_snapshot"},
            }
        parse_effect_descriptor(rewritten, known_state_keys=set(new_state))
        message["lockstep_effect"] = rewritten
    else:
        brief = None
    if isinstance(message, dict):
        for message_key, message_value in tuple(message.items()):
            if message_key != "lockstep_effect":
                message[message_key] = _rewrite_child_state_template(
                    message_value, key_map
                )
    return node, brief


def _specialize_child_edges(
    raw_edges: object,
    *,
    node_map: dict[str, str],
    key_map: dict[str, str],
    managed_briefs: dict[str, str],
) -> list[dict[str, Any]]:
    rewritten_edges: list[dict[str, Any]] = []
    for raw_edge in raw_edges:
        edge = plain(raw_edge)
        source = edge.get("from")
        if isinstance(source, list):
            edge["from"] = [node_map[item] for item in source]
        elif source not in {"START", "END"}:
            edge["from"] = node_map[source]
        targets = edge.get("to")
        if isinstance(targets, list):
            edge["to"] = [
                (
                    target
                    if target in {"START", "END"}
                    else managed_briefs.get(node_map[target], node_map[target])
                )
                for target in targets
            ]
        elif targets not in {"START", "END"}:
            mapped_target = node_map[targets]
            edge["to"] = managed_briefs.get(mapped_target, mapped_target)
        if "condition" in edge:
            edge["condition"] = _rewrite_condition_references(
                edge["condition"], key_map
            )
        rewritten_edges.append(edge)
    rewritten_edges.extend(
        {"from": brief, "to": effect}
        for effect, brief in managed_briefs.items()
    )
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
