"""Bounded JSON-domain admission for public workflow transition payloads."""

from __future__ import annotations

import json
import math
from typing import Any

MAX_DEPTH = 16
MAX_NODES = 4096
MAX_SCALAR_UTF8_BYTES = 64 * 1024
MAX_CANONICAL_BYTES = 1024 * 1024
MIN_INTEGER = -(2**63)
MAX_INTEGER = 2**63 - 1


class PayloadLimitExceeded(ValueError):
    pass


def _utf8_size(value: str, *, label: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise PayloadLimitExceeded(f"{label} contains invalid Unicode") from exc


def bounded_json(value: object, *, label: str) -> Any:
    """Validate, size, canonicalize, and detach one JSON-domain value."""
    stack = [(value, 0)]
    seen_containers: set[int] = set()
    nodes = 0
    scalar_bytes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise PayloadLimitExceeded(f"{label} exceeds structural limits")
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, int):
            if not MIN_INTEGER <= item <= MAX_INTEGER:
                raise PayloadLimitExceeded(f"{label} integer is out of range")
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise PayloadLimitExceeded(f"{label} contains a non-finite number")
            continue
        if isinstance(item, str):
            size = _utf8_size(item, label=label)
            if size > MAX_SCALAR_UTF8_BYTES:
                raise PayloadLimitExceeded(f"{label} scalar exceeds byte limit")
            scalar_bytes += size
        elif isinstance(item, list):
            if len(item) > MAX_NODES:
                raise PayloadLimitExceeded(f"{label} exceeds structural limits")
            identity = id(item)
            if identity in seen_containers:
                raise PayloadLimitExceeded(f"{label} contains a repeated container")
            seen_containers.add(identity)
            stack.extend((child, depth + 1) for child in reversed(item))
        elif isinstance(item, dict):
            if len(item) > MAX_NODES:
                raise PayloadLimitExceeded(f"{label} exceeds structural limits")
            identity = id(item)
            if identity in seen_containers:
                raise PayloadLimitExceeded(f"{label} contains a repeated container")
            seen_containers.add(identity)
            for key, child in item.items():
                if not isinstance(key, str):
                    raise PayloadLimitExceeded(f"{label} keys must be strings")
                size = _utf8_size(key, label=label)
                if size > MAX_SCALAR_UTF8_BYTES:
                    raise PayloadLimitExceeded(f"{label} key exceeds byte limit")
                scalar_bytes += size
                if scalar_bytes > MAX_CANONICAL_BYTES:
                    raise PayloadLimitExceeded(f"{label} exceeds byte limit")
                stack.append((child, depth + 1))
        else:
            raise PayloadLimitExceeded(f"{label} contains a non-JSON value")
        if scalar_bytes > MAX_CANONICAL_BYTES:
            raise PayloadLimitExceeded(f"{label} exceeds byte limit")

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_CANONICAL_BYTES:
        raise PayloadLimitExceeded(f"{label} exceeds byte limit")
    return json.loads(encoded)
