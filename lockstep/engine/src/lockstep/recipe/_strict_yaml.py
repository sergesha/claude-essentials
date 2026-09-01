"""Strict, bounded YAML scanning and decoding into the JSON value domain."""

from __future__ import annotations

import re
from typing import Any

import yaml
from yaml.events import (
    AliasEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)

from ._authority_models import RecipeAuthorityError, RecipeLimits


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
