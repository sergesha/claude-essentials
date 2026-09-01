"""Canonical serializers for pure workflow compilation artifacts."""

from __future__ import annotations

import json
from typing import Any, Mapping

import yaml


def plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain(item) for item in value]
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


def canonical_yaml(value: Mapping[str, Any]) -> bytes:
    text = yaml.safe_dump(
        plain(value), sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return text.replace("\r\n", "\n").encode("utf-8")


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            plain(value), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
