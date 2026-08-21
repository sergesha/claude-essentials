"""Strict recipe admission before yamlgraph receives any workflow bytes.

This module deliberately understands only Lockstep's closed yamlgraph profile.
It decodes untrusted YAML into a bounded JSON domain, canonicalizes every
supported source file, closes the recursive subgraph dependency DAG, and
separates workflow edit authority from executable authority.

It does not compile a graph.  yamlgraph remains the only graph compiler.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

import yaml
from yaml.events import (
    AliasEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)

from lockstep.runtime.owner_state import StorageLimitExceeded
from lockstep.runtime.recipe_bundles import (
    MaterializedRecipe,
    RecipeBundleRef,
    ValidatedDependencyDAG,
    open_recipe_source_root,
    read_recipe_source_file,
    safe_recipe_relative_path,
)

if TYPE_CHECKING:
    from lockstep.runtime.recipe_bundles import RecipeBundleStore


class RecipeAuthorityError(ValueError):
    """Recipe bytes cannot enter the executable workflow boundary."""


class AuthorityDenied(RecipeAuthorityError):
    """A recipe requests executable authority without an exact owner grant."""


@dataclass(frozen=True)
class RecipeLimits:
    max_source_bytes: int = 4 * 1024 * 1024
    max_file_bytes: int = 1024 * 1024
    max_files: int = 256
    max_depth: int = 64
    max_nodes: int = 50_000
    max_container_items: int = 10_000
    max_scalar_bytes: int = 2 * 1024 * 1024
    max_integer_abs: int = 2**63 - 1

    def __post_init__(self) -> None:
        if (
            min(
                self.max_source_bytes,
                self.max_file_bytes,
                self.max_files,
                self.max_depth,
                self.max_nodes,
                self.max_container_items,
                self.max_scalar_bytes,
                self.max_integer_abs,
            )
            <= 0
        ):
            raise ValueError("recipe limits must be positive")


@dataclass(frozen=True, order=True)
class CanonicalRecipeFile:
    path: str
    bytes: bytes
    sha256: str


@dataclass(frozen=True, order=True)
class AuthorityRequirement:
    """One exact compile/runtime executable surface found in canonical YAML."""

    sha256: str
    kind: Literal["python", "shell"]
    tool_name: str
    descriptor: tuple[tuple[str, object], ...]
    uses: tuple[str, ...]


@dataclass(frozen=True, order=True)
class OwnerReviewedGrant:
    """TCB configuration, never a value accepted from recipe YAML."""

    recipe_sha256: str
    requirement_sha256: str
    authority: Literal["os_user_execution"]

    def __post_init__(self) -> None:
        for value in (self.recipe_sha256, self.requirement_sha256):
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(
                    "owner-reviewed grants require lowercase SHA-256 digests"
                )
        if self.authority != "os_user_execution":
            raise ValueError("local executable grants must name full os_user_execution")


@dataclass(frozen=True, order=True)
class OwnerReviewedPythonTarget:
    """Exact installed Lockstep callable admitted by owner configuration.

    A package prefix is never sufficient: every module/function pair is
    explicit.  The local-MVP installation is part of the TCB; arbitrary
    project-local or third-party import roots are intentionally unsupported.
    """

    module: str
    function: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"lockstep(?:\.[A-Za-z_]\w*)+", self.module):
            raise ValueError(
                "reviewed Python targets must name an exact installed Lockstep module"
            )
        if not re.fullmatch(r"[A-Za-z_]\w*", self.function):
            raise ValueError("reviewed Python targets require an exact function name")


@dataclass(frozen=True)
class RecipeAuthorityPolicy:
    grants: tuple[OwnerReviewedGrant, ...] = ()
    python_targets: tuple[OwnerReviewedPythonTarget, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.grants, tuple) or not isinstance(
            self.python_targets, tuple
        ):
            raise TypeError("recipe authority policy entries must be tuples")

    def permits(self, recipe_sha256: str, requirement: AuthorityRequirement) -> bool:
        digest_granted = any(
            grant.recipe_sha256 == recipe_sha256
            and grant.requirement_sha256 == requirement.sha256
            and grant.authority == "os_user_execution"
            for grant in self.grants
        )
        if not digest_granted:
            return False
        if requirement.kind != "python":
            return True
        descriptor = dict(requirement.descriptor)
        try:
            target = OwnerReviewedPythonTarget(
                module=str(descriptor.get("module", "")),
                function=str(descriptor.get("function", "")),
            )
        except ValueError:
            return False
        return target in self.python_targets


@dataclass(frozen=True)
class AuthorizedRecipe:
    root: str
    files: tuple[CanonicalRecipeFile, ...]
    definition_sha256: str
    dependency_dag: ValidatedDependencyDAG
    authority_requirements: tuple[AuthorityRequirement, ...]
    source_bundle_sha256: str

    def capture(self, store: RecipeBundleStore) -> AdmittedRecipe:
        """Publish these exact canonical bytes through the DAG-only store seam."""
        with tempfile.TemporaryDirectory(prefix="lockstep-canonical-recipe-") as raw:
            staging = Path(raw)
            for item in self.files:
                destination = staging / item.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(item.bytes)
            bundle = store.capture(staging, self.dependency_dag)
        return AdmittedRecipe(
            bundle=bundle,
            root=self.root,
            files=self.files,
            definition_sha256=self.definition_sha256,
            dependency_dag=self.dependency_dag,
            authority_requirements=self.authority_requirements,
        )


@dataclass(frozen=True)
class AuthorizedMaterialization:
    bundle: RecipeBundleRef
    definition_sha256: str
    dependency_dag: ValidatedDependencyDAG
    source_path: Path
    directory: Path


@dataclass(frozen=True)
class AdmittedRecipe:
    """Durable identity for one authorized canonical recipe bundle."""

    bundle: RecipeBundleRef
    root: str
    files: tuple[CanonicalRecipeFile, ...]
    definition_sha256: str
    dependency_dag: ValidatedDependencyDAG
    authority_requirements: tuple[AuthorityRequirement, ...]

    def materialize(self, store: RecipeBundleStore) -> AuthorizedMaterialization:
        materialized: MaterializedRecipe = store.materialize_for_compile(self.bundle)
        manifest = store.read_manifest(self.bundle)
        expected = tuple(
            (item.path, item.sha256, len(item.bytes)) for item in self.files
        )
        observed = tuple((item.path, item.sha256, item.size) for item in manifest.files)
        if manifest.root != self.root or observed != expected:
            raise RecipeAuthorityError(
                "admitted recipe bundle no longer matches its canonical definition"
            )
        return AuthorizedMaterialization(
            bundle=self.bundle,
            definition_sha256=self.definition_sha256,
            dependency_dag=self.dependency_dag,
            source_path=materialized.source_path,
            directory=materialized.directory,
        )


@dataclass(frozen=True)
class RecipeCandidate:
    """Canonical, closed content with no executable authority yet."""

    root: str
    files: tuple[CanonicalRecipeFile, ...]
    definition_sha256: str
    dependency_dag: ValidatedDependencyDAG
    authority_requirements: tuple[AuthorityRequirement, ...]
    source_bundle_sha256: str

    def authorize(self, policy: RecipeAuthorityPolicy) -> AuthorizedRecipe:
        if not isinstance(policy, RecipeAuthorityPolicy):
            raise TypeError("recipe authorization requires a RecipeAuthorityPolicy")
        denied = tuple(
            requirement
            for requirement in self.authority_requirements
            if not policy.permits(self.definition_sha256, requirement)
        )
        if denied:
            labels = ", ".join(
                f"{item.kind} tool {item.tool_name!r} ({item.sha256})"
                for item in denied
            )
            raise AuthorityDenied(
                "recipe executable authority denied: "
                f"{labels}; an exact owner-reviewed os_user_execution grant "
                "bound to this definition digest is required"
            )
        return AuthorizedRecipe(
            root=self.root,
            files=self.files,
            definition_sha256=self.definition_sha256,
            dependency_dag=self.dependency_dag,
            authority_requirements=self.authority_requirements,
            source_bundle_sha256=self.source_bundle_sha256,
        )


class _StrictJSONLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _StrictJSONLoader, node: yaml.MappingNode, deep=False):
    loader.flatten_mapping(node)
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise RecipeAuthorityError("recipe mapping keys must be strings")
        if key in result:
            raise RecipeAuthorityError(f"duplicate mapping key {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictJSONLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)
_StrictJSONLoader.yaml_implicit_resolvers = {}
_StrictJSONLoader.add_implicit_resolver(
    "tag:yaml.org,2002:null", re.compile(r"^(?:null)$"), ["n"]
)
_StrictJSONLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), ["t", "f"]
)
_StrictJSONLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int", re.compile(r"^-?(?:0|[1-9][0-9]*)$"), list("-0123456789")
)
_StrictJSONLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^-?(?:0|[1-9][0-9]*)\.[0-9]+(?:[eE][+-]?[0-9]+)?$"),
    list("-0123456789"),
)

_AMBIGUOUS_PLAIN = re.compile(
    r"^(?:yes|no|on|off|true|false|null|~|\.nan|[-+]?\.inf)$", re.IGNORECASE
)
_TIMESTAMP_PLAIN = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}(?:[Tt ]|$)")
_NON_JSON_NUMBER = re.compile(
    r"^(?:[-+]?0[0-9_]+|[-+]?[0-9][0-9_]*:[0-9:]"
    r"|[-+]?0[xob][0-9a-fA-F_]+|[-+]?[0-9_]+\.)$"
)
_JSON_NUMBER = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")


def _scan_yaml_events(data: bytes, limits: RecipeLimits, logical: str) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecipeAuthorityError(f"recipe source is not UTF-8: {logical}") from exc
    depth = 0
    nodes = 0
    scalar_bytes = 0
    container_items: list[list[int | bool]] = []
    try:
        events = yaml.parse(text)
        for event in events:
            if isinstance(event, AliasEvent):
                raise RecipeAuthorityError(f"YAML aliases are forbidden: {logical}")
            if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                if event.anchor is not None:
                    raise RecipeAuthorityError(f"YAML anchors are forbidden: {logical}")
                if event.tag not in (
                    None,
                    "tag:yaml.org,2002:map",
                    "tag:yaml.org,2002:seq",
                ):
                    raise RecipeAuthorityError(
                        f"explicit YAML tags are forbidden: {logical}"
                    )
                nodes += 1
                depth += 1
                if depth > limits.max_depth:
                    raise RecipeAuthorityError(
                        f"recipe YAML depth exceeds {limits.max_depth}: {logical}"
                    )
                if container_items:
                    container_items[-1][1] = int(container_items[-1][1]) + 1
                container_items.append([isinstance(event, MappingStartEvent), 0])
            elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
                if container_items:
                    is_mapping, count = container_items[-1]
                    admitted = limits.max_container_items * (2 if is_mapping else 1)
                    if int(count) > admitted:
                        raise RecipeAuthorityError(
                            "recipe container items exceed "
                            f"{limits.max_container_items}: {logical}"
                        )
                    container_items.pop()
                depth -= 1
            elif isinstance(event, ScalarEvent):
                if event.anchor is not None:
                    raise RecipeAuthorityError(f"YAML anchors are forbidden: {logical}")
                if event.tag not in (
                    None,
                    "tag:yaml.org,2002:str",
                    "tag:yaml.org,2002:null",
                    "tag:yaml.org,2002:bool",
                    "tag:yaml.org,2002:int",
                    "tag:yaml.org,2002:float",
                ):
                    raise RecipeAuthorityError(
                        f"explicit YAML tags are forbidden: {logical}"
                    )
                nodes += 1
                scalar_bytes += len(event.value.encode("utf-8"))
                if container_items:
                    container_items[-1][1] = int(container_items[-1][1]) + 1
                if event.style is None:
                    value = event.value
                    lower = value.lower()
                    json_literal = lower in {"true", "false", "null"} and value == lower
                    if (
                        (_AMBIGUOUS_PLAIN.fullmatch(value) and not json_literal)
                        or _TIMESTAMP_PLAIN.match(value)
                        or (
                            _NON_JSON_NUMBER.fullmatch(value)
                            and not _JSON_NUMBER.fullmatch(value)
                        )
                    ):
                        raise RecipeAuthorityError(
                            f"ambiguous scalar {value!r} must be quoted: {logical}"
                        )
            if nodes > limits.max_nodes:
                raise RecipeAuthorityError(
                    f"recipe YAML nodes exceed {limits.max_nodes}: {logical}"
                )
            if scalar_bytes > limits.max_scalar_bytes:
                raise RecipeAuthorityError(
                    f"recipe scalar bytes exceed {limits.max_scalar_bytes}: {logical}"
                )
    except yaml.YAMLError as exc:
        raise RecipeAuthorityError(f"invalid recipe YAML: {logical}: {exc}") from exc


def _decode_document(data: bytes, limits: RecipeLimits, logical: str) -> dict[str, Any]:
    _scan_yaml_events(data, limits, logical)
    try:
        loaded = yaml.load(data.decode("utf-8"), Loader=_StrictJSONLoader)
    except yaml.YAMLError as exc:
        raise RecipeAuthorityError(f"invalid recipe YAML: {logical}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RecipeAuthorityError(f"recipe document must be a mapping: {logical}")
    pending: list[object] = [loaded]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif (
            isinstance(value, int)
            and not isinstance(value, bool)
            and abs(value) > limits.max_integer_abs
        ):
            raise RecipeAuthorityError(
                f"recipe integer range exceeds +/-{limits.max_integer_abs}: {logical}"
            )
    return loaded


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


def _profile_document(document: dict[str, Any], logical: str) -> _DocumentProfile:
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
    nodes = _require_mapping(document.get("nodes"), f"{logical} nodes")
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

    dependencies: list[str] = []
    uses: dict[str, list[str]] = {name: [] for name in tools}
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
    return _DocumentProfile(tuple(dependencies), tuple(requirements))


class StrictRecipeIngress:
    """Read and close one recipe definition without invoking yamlgraph."""

    def __init__(
        self,
        source_root: str | Path,
        *,
        limits: RecipeLimits | None = None,
    ) -> None:
        self._source_root = Path(source_root)
        self._limits = limits or RecipeLimits()

    def inspect(self, root: str) -> RecipeCandidate:
        root = safe_recipe_relative_path(root)
        root_fd = open_recipe_source_root(self._source_root)
        active: set[str] = set()
        documents: dict[str, bytes] = {}
        source_hashes: dict[str, str] = {}
        requirements: list[AuthorityRequirement] = []
        total_bytes = 0

        def visit(logical: str) -> None:
            nonlocal total_bytes
            if logical in documents:
                return
            if logical in active:
                raise RecipeAuthorityError(
                    f"recursive subgraph dependency is not a DAG: {logical}"
                )
            if len(documents) + len(active) >= self._limits.max_files:
                raise RecipeAuthorityError(
                    f"recipe files exceed {self._limits.max_files}"
                )
            remaining = self._limits.max_source_bytes - total_bytes
            if remaining <= 0:
                raise RecipeAuthorityError(
                    f"recipe source bytes exceed {self._limits.max_source_bytes}"
                )
            try:
                data = read_recipe_source_file(
                    root_fd,
                    PurePosixPath(logical),
                    max_bytes=min(remaining, self._limits.max_file_bytes),
                )
            except StorageLimitExceeded as exc:
                raise RecipeAuthorityError(
                    "recipe source bytes exceed configured admission limit"
                ) from exc
            total_bytes += len(data)
            source_hashes[logical] = hashlib.sha256(data).hexdigest()
            document = _decode_document(data, self._limits, logical)
            profile = _profile_document(document, logical)
            active.add(logical)
            try:
                for dependency in profile.dependencies:
                    visit(dependency)
            finally:
                active.remove(logical)
            documents[logical] = _canonical_bytes(document)
            requirements.extend(profile.requirements)

        try:
            visit(root)
        except FileNotFoundError as exc:
            raise RecipeAuthorityError(f"recipe dependency not found: {exc}") from exc
        finally:
            os.close(root_fd)

        files = tuple(
            CanonicalRecipeFile(
                path=path,
                bytes=data,
                sha256=hashlib.sha256(data).hexdigest(),
            )
            for path, data in sorted(documents.items())
        )
        definition_payload = {
            "schema": "lockstep.recipe-definition/v1",
            "root": root,
            "files": [
                {"path": item.path, "sha256": item.sha256, "size": len(item.bytes)}
                for item in files
            ],
        }
        definition_sha256 = hashlib.sha256(
            _canonical_bytes(definition_payload)
        ).hexdigest()
        dag = ValidatedDependencyDAG.from_validated(
            root,
            (item.path for item in files),
            max_files=self._limits.max_files,
            max_dependencies=self._limits.max_files - 1,
        )
        source_manifest = hashlib.sha256(b"lockstep.compiled-bundle/v1\0")
        source_manifest.update(root.encode("utf-8"))
        source_manifest.update(b"\0")
        for path, sha256 in sorted(source_hashes.items()):
            source_manifest.update(path.encode("utf-8"))
            source_manifest.update(b"\0")
            source_manifest.update(sha256.encode("ascii"))
            source_manifest.update(b"\0")
        return RecipeCandidate(
            root=root,
            files=files,
            definition_sha256=definition_sha256,
            dependency_dag=dag,
            authority_requirements=tuple(sorted(requirements)),
            source_bundle_sha256=source_manifest.hexdigest(),
        )
