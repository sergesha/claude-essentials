"""Closed recipe document schema, dependency projection, and digests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from lockstep.runtime.recipe_bundles import safe_recipe_relative_path

from ._authority_models import AuthorityRequirement, RecipeAuthorityError, RecipeLimits
from ._strict_yaml import _decode_document


_TOP_LEVEL_FIELDS = {
    "version",
    "name",
    "description",
    "state",
    "nodes",
    "edges",
    "tools",
    "loop_limits",
    "loop_exits",
    "config",
    "variables",
    "baseline_globs",
    "x-lockstep-generated",
}
_NODE_FIELDS = {
    "interrupt": {"type", "message", "state_key", "resume_key", "idempotent"},
    "passthrough": {"type", "output"},
    "subgraph": {
        "type",
        "graph",
        "mode",
        "input_mapping",
        "output_mapping",
        "interrupt_output_mapping",
    },
    "python": {"type", "tool", "state_key", "on_error", "timeout", "variables"},
    "tool": {"type", "tool", "state_key", "on_error", "timeout", "variables"},
}
_CONFIG_FIELDS = {"recursion_limit", "max_map_items", "max_tokens", "timeout"}
_PYTHON_TOOL_FIELDS = {"type", "module", "function", "description"}
_SHELL_TOOL_FIELDS = {
    "type",
    "command",
    "description",
    "parse",
    "timeout",
    "working_dir",
    "env",
    "success_codes",
}
_STATE_TYPES = {
    "str",
    "string",
    "int",
    "integer",
    "float",
    "bool",
    "boolean",
    "list",
    "dict",
    "any",
}
_STATE_REDUCERS = {"add", "last_value", "sorted_add"}


def _closed_fields(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RecipeAuthorityError(
            f"{label} has unknown field(s): {', '.join(unknown)}"
        )


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecipeAuthorityError(f"{label} must be a mapping")
    return value


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RecipeAuthorityError(f"{label} must be a non-empty string")
    return value


def _require_optional_string(value: dict[str, Any], field: str, label: str) -> None:
    if field in value:
        _require_string(value[field], f"{label} {field}")


def _require_positive_number(
    value: object, label: str, *, integer: bool = False
) -> None:
    expected = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, expected) or value <= 0:
        kind = "positive integer" if integer else "positive number"
        raise RecipeAuthorityError(f"{label} must be a {kind}")


def _require_string_mapping(value: object, label: str) -> dict[str, str]:
    mapping = _require_mapping(value, label)
    if any(
        not isinstance(key, str) or not key or not isinstance(item, str)
        for key, item in mapping.items()
    ):
        raise RecipeAuthorityError(f"{label} must map non-empty strings to strings")
    return mapping  # type: ignore[return-value]


def _require_state_mapping(value: object, label: str) -> dict[str, Any]:
    mapping = _require_mapping(value, label)
    if any(not isinstance(key, str) or not key for key in mapping):
        raise RecipeAuthorityError(f"{label} keys must be non-empty strings")
    return mapping


def _validate_tool_definition(tool: dict[str, Any], tool_name: str) -> str:
    label = f"{tool.get('type')} tool {tool_name!r}"
    kind = tool.get("type")
    if kind == "python":
        _closed_fields(tool, _PYTHON_TOOL_FIELDS, label)
        _require_string(tool.get("module"), f"{label} module")
        _require_string(tool.get("function"), f"{label} function")
        _require_optional_string(tool, "description", label)
        return kind
    if kind == "shell":
        _closed_fields(tool, _SHELL_TOOL_FIELDS, label)
        _require_string(tool.get("command"), f"{label} command")
        _require_optional_string(tool, "description", label)
        _require_optional_string(tool, "working_dir", label)
        parse = tool.get("parse", "text")
        if parse not in {"text", "json", "none"}:
            raise RecipeAuthorityError(
                f"{label} parse must be one of: text, json, none"
            )
        if "timeout" in tool:
            _require_positive_number(tool["timeout"], f"{label} timeout")
        if "env" in tool:
            _require_string_mapping(tool["env"], f"{label} env")
        if "success_codes" in tool:
            codes = tool["success_codes"]
            if (
                not isinstance(codes, list)
                or not codes
                or any(
                    not isinstance(code, int) or isinstance(code, bool)
                    for code in codes
                )
            ):
                raise RecipeAuthorityError(
                    f"{label} success_codes must be a non-empty integer list"
                )
        return kind
    raise RecipeAuthorityError(f"unsupported tool kind {kind!r} for {tool_name!r}")


def _validate_node_fields(node: dict[str, Any], node_name: str, logical: str) -> None:
    kind = node["type"]
    label = f"{logical} node {node_name!r}"
    if kind == "interrupt":
        if "message" in node and not isinstance(node["message"], (str, dict)):
            raise RecipeAuthorityError(f"{label} message must be a string or mapping")
        _require_optional_string(node, "state_key", label)
        _require_optional_string(node, "resume_key", label)
        if "idempotent" in node and not isinstance(node["idempotent"], bool):
            raise RecipeAuthorityError(f"{label} idempotent must be a boolean")
    elif kind == "passthrough":
        if "output" in node:
            _require_state_mapping(node["output"], f"{label} output")
    elif kind == "subgraph":
        mode = node.get("mode", "invoke")
        if mode not in {"direct", "invoke"}:
            raise RecipeAuthorityError(f"{label} mode must be direct or invoke")
        for field in (
            "input_mapping",
            "output_mapping",
            "interrupt_output_mapping",
        ):
            if field not in node:
                continue
            mapping = node[field]
            if isinstance(mapping, str):
                if mapping not in {"auto", "*"}:
                    raise RecipeAuthorityError(
                        f"{label} {field} must be auto, *, or a string mapping"
                    )
            else:
                _require_string_mapping(mapping, f"{label} {field}")
    elif kind in {"python", "tool"}:
        _require_optional_string(node, "state_key", label)
        if node.get("on_error", "fail") not in {"fail", "skip"}:
            raise RecipeAuthorityError(f"{label} on_error must be fail or skip")
        if "timeout" in node:
            _require_positive_number(node["timeout"], f"{label} timeout")
        if "variables" in node:
            _require_state_mapping(node["variables"], f"{label} variables")


def _canonical_bytes(document: dict[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise RecipeAuthorityError(
            "recipe must contain only finite JSON values"
        ) from exc


def recipe_definition_sha256(
    root: str, files: Iterable[tuple[str, str, int]]
) -> str:
    """Derive the admitted recipe identity from its canonical bundle facts."""
    payload = {
        "schema": "lockstep.recipe-definition/v1",
        "root": root,
        "files": [
            {"path": path, "sha256": sha256, "size": size}
            for path, sha256, size in files
        ],
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def canonical_execution_bytes(
    source_bytes: bytes,
    *,
    logical_path: str,
    limits: RecipeLimits | None = None,
) -> bytes:
    """Return the exact bytes handed from strict ingress to yamlgraph.

    This is the shared compiler/admission representation: emitted YAML stays
    independently bound by the source bundle digest, while compiler authority
    is granted only to this closed, finite canonical JSON document.
    """

    if not isinstance(source_bytes, bytes):
        raise TypeError("recipe source must be bytes")
    logical = safe_recipe_relative_path(logical_path)
    bounded = limits or RecipeLimits()
    if len(source_bytes) > bounded.max_file_bytes:
        raise RecipeAuthorityError("recipe source bytes exceed configured admission limit")
    document = _decode_document(source_bytes, bounded, logical)
    _profile_document(document, logical)
    return _canonical_bytes(document)


def _resolve_reference(parent: str, raw: object) -> str:
    reference = _require_string(raw, "subgraph path")
    if (
        "\\" in reference
        or "\x00" in reference
        or PurePosixPath(reference).is_absolute()
    ):
        raise RecipeAuthorityError(f"subgraph path is unsafe: {reference!r}")
    parts = list(PurePosixPath(parent).parent.parts)
    for part in PurePosixPath(reference).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise RecipeAuthorityError(
                    f"subgraph path escapes source root: {reference!r}"
                )
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise RecipeAuthorityError(f"subgraph path is unsafe: {reference!r}")
    return safe_recipe_relative_path(PurePosixPath(*parts).as_posix())


@dataclass(frozen=True)
class _DocumentProfile:
    dependencies: tuple[str, ...]
    requirements: tuple[AuthorityRequirement, ...]


def _validate_document_header(document: dict[str, Any], logical: str) -> None:
    unknown_top = sorted(set(document) - _TOP_LEVEL_FIELDS)
    if unknown_top:
        raise RecipeAuthorityError(
            f"unknown top-level field(s): {', '.join(unknown_top)}"
        )
    for forbidden in ("data_files", "prompts_dir", "checkpointer"):
        if forbidden in document:
            raise RecipeAuthorityError(
                f"unsupported yamlgraph loader directive: {forbidden}"
            )

    if "version" in document and not isinstance(document["version"], str):
        raise RecipeAuthorityError(f"{logical} version must be a string")
    _require_string(document.get("name"), f"{logical} name")
    if "description" in document and not isinstance(document["description"], str):
        raise RecipeAuthorityError(f"{logical} description must be a string")


def _validate_document_state(document: dict[str, Any], logical: str) -> None:
    state = _require_mapping(document.get("state", {}), f"{logical} state")
    for field_name, specification in state.items():
        _require_string(field_name, f"{logical} state field name")
        if isinstance(specification, str):
            if specification.lower() not in _STATE_TYPES:
                raise RecipeAuthorityError(
                    f"{logical} state field {field_name!r} has unknown type {specification!r}"
                )
            continue
        spec = _require_mapping(specification, f"{logical} state field {field_name!r}")
        _closed_fields(
            spec, {"type", "reducer"}, f"{logical} state field {field_name!r}"
        )
        state_type = spec.get("type", "any")
        if not isinstance(state_type, str) or state_type.lower() not in _STATE_TYPES:
            raise RecipeAuthorityError(
                f"{logical} state field {field_name!r} has unknown type {state_type!r}"
            )
        reducer = spec.get("reducer")
        if reducer is not None and reducer not in _STATE_REDUCERS:
            raise RecipeAuthorityError(
                f"{logical} state field {field_name!r} has unknown reducer {reducer!r}"
            )


def _validate_document_edges(document: dict[str, Any], logical: str) -> None:
    edges = document.get("edges")
    if not isinstance(edges, list):
        raise RecipeAuthorityError(f"{logical} edges must be a list")
    for index, raw_edge in enumerate(edges):
        edge = _require_mapping(raw_edge, f"{logical} edge {index}")
        _closed_fields(edge, {"from", "to", "condition"}, f"{logical} edge {index}")
        _require_string(edge.get("from"), f"{logical} edge {index} from")
        targets = edge.get("to")
        if isinstance(targets, str):
            _require_string(targets, f"{logical} edge {index} to")
        elif isinstance(targets, list) and targets:
            for target in targets:
                _require_string(target, f"{logical} edge {index} to")
        else:
            raise RecipeAuthorityError(
                f"{logical} edge {index} to must be a string or non-empty string list"
            )
        if "condition" in edge and not isinstance(edge["condition"], str):
            raise RecipeAuthorityError(
                f"{logical} edge {index} condition must be a string"
            )


def _validate_document_loops(document: dict[str, Any], logical: str) -> None:
    loop_limits = _require_mapping(
        document.get("loop_limits", {}), f"{logical} loop_limits"
    )
    for node_name, limit in loop_limits.items():
        if (
            not isinstance(node_name, str)
            or not node_name
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
        ):
            raise RecipeAuthorityError(f"{logical} loop_limits entries are invalid")
    loop_exits = _require_mapping(
        document.get("loop_exits", {}), f"{logical} loop_exits"
    )
    for node_name, target in loop_exits.items():
        _require_string(node_name, f"{logical} loop_exits node")
        _require_string(target, f"{logical} loop_exits target")


def _validate_document_options(
    document: dict[str, Any], logical: str
) -> Mapping[str, Any]:
    if "baseline_globs" in document:
        globs = document["baseline_globs"]
        if not isinstance(globs, list) or any(
            not isinstance(item, str) for item in globs
        ):
            raise RecipeAuthorityError(
                f"{logical} baseline_globs must be a string list"
            )
    tools = _require_mapping(document.get("tools", {}), f"{logical} tools")
    config = _require_mapping(document.get("config", {}), f"{logical} config")
    _closed_fields(config, _CONFIG_FIELDS, f"{logical} config")
    for field in ("recursion_limit", "max_map_items", "max_tokens"):
        if field in config:
            _require_positive_number(
                config[field], f"{logical} config {field}", integer=True
            )
    if "timeout" in config:
        _require_positive_number(config["timeout"], f"{logical} config timeout")
    if "variables" in document:
        _require_state_mapping(document["variables"], f"{logical} variables")
    if "x-lockstep-generated" in document:
        _require_mapping(
            document["x-lockstep-generated"], f"{logical} x-lockstep-generated"
        )
    return tools


def _profile_tools(
    tools: Mapping[str, Any], logical: str
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    tool_kinds: dict[str, str] = {}
    tool_descriptors: dict[str, dict[str, Any]] = {}
    for tool_name, raw_tool in tools.items():
        _require_string(tool_name, f"{logical} tool name")
        tool = _require_mapping(raw_tool, f"{logical} tool {tool_name!r}")
        if "manifest" in tool:
            raise RecipeAuthorityError("tool manifest paths are not supported")
        kind = tool.get("type")
        if kind == "graph":
            raise RecipeAuthorityError("graph tool paths are not supported")
        kind = _validate_tool_definition(tool, tool_name)
        tool_kinds[tool_name] = kind
        tool_descriptors[tool_name] = tool
    return tool_kinds, tool_descriptors


def _profile_nodes(
    nodes: Mapping[str, Any],
    logical: str,
    tool_kinds: Mapping[str, str],
    tool_names: Iterable[str],
) -> tuple[list[str], dict[str, list[str]]]:
    dependencies: list[str] = []
    uses: dict[str, list[str]] = {name: [] for name in tool_names}
    for node_name, raw_node in nodes.items():
        _require_string(node_name, f"{logical} node name")
        node = _require_mapping(raw_node, f"{logical} node {node_name!r}")
        kind = node.get("type")
        allowed = _NODE_FIELDS.get(kind)
        if allowed is None:
            raise RecipeAuthorityError(
                f"unsupported node kind {kind!r}: {logical}#/nodes/{node_name}"
            )
        _closed_fields(node, allowed, f"{logical} node {node_name!r}")
        _validate_node_fields(node, node_name, logical)
        if kind == "subgraph":
            if "checkpointer" in node:
                raise RecipeAuthorityError("subgraph checkpointer is engine-owned")
            dependencies.append(_resolve_reference(logical, node.get("graph")))
        elif kind in {"python", "tool"}:
            tool_name = _require_string(
                node.get("tool"), f"{logical} node {node_name!r} tool"
            )
            expected = "python" if kind == "python" else "shell"
            if tool_kinds.get(tool_name) != expected:
                raise RecipeAuthorityError(
                    f"{logical} node {node_name!r} requires a declared {expected} tool"
                )
            uses[tool_name].append(f"{logical}#/nodes/{node_name}")
    return dependencies, uses


def _authority_requirements(
    logical: str,
    tool_kinds: Mapping[str, str],
    tool_descriptors: Mapping[str, dict[str, Any]],
    uses: Mapping[str, list[str]],
) -> tuple[AuthorityRequirement, ...]:
    requirements: list[AuthorityRequirement] = []
    for tool_name in sorted(tool_descriptors):
        descriptor_dict = tool_descriptors[tool_name]
        descriptor_payload = {
            "schema": "lockstep.recipe-executable/v1",
            "source": logical,
            "tool": tool_name,
            "descriptor": descriptor_dict,
            "uses": sorted(uses[tool_name]),
        }
        digest = hashlib.sha256(_canonical_bytes(descriptor_payload)).hexdigest()
        requirements.append(
            AuthorityRequirement(
                sha256=digest,
                kind=tool_kinds[tool_name],  # type: ignore[arg-type]
                tool_name=tool_name,
                descriptor=tuple(sorted(descriptor_dict.items())),
                uses=tuple(sorted(uses[tool_name])),
            )
        )
    return tuple(requirements)


def _profile_document(document: dict[str, Any], logical: str) -> _DocumentProfile:
    _validate_document_header(document, logical)
    _validate_document_state(document, logical)
    nodes = _require_mapping(document.get("nodes"), f"{logical} nodes")
    _validate_document_edges(document, logical)
    _validate_document_loops(document, logical)
    tools = _validate_document_options(document, logical)
    tool_kinds, tool_descriptors = _profile_tools(tools, logical)
    dependencies, uses = _profile_nodes(
        nodes, logical, tool_kinds, tool_descriptors
    )
    requirements = _authority_requirements(
        logical, tool_kinds, tool_descriptors, uses
    )
    return _DocumentProfile(tuple(dependencies), requirements)
