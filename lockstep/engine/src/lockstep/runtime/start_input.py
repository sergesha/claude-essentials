"""Canonical admission boundary for native scenario start input."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from lockstep.runtime.errors import LockstepError
from lockstep.runtime.payload_limits import PayloadLimitExceeded, bounded_json

_RESERVED_START_KEYS = frozenset({"namespace"})


def validate_start_input(input: Mapping[object, object] | None) -> dict[str, Any]:
    try:
        values = bounded_json({} if input is None else input, label="scenario input")
    except PayloadLimitExceeded as exc:
        raise LockstepError(str(exc)) from exc
    if not isinstance(values, dict):
        raise LockstepError("scenario input must be a JSON object")
    forbidden = sorted(
        str(key)
        for key in values
        if not isinstance(key, str)
        or key.startswith(("_", "lockstep_"))
        or key in _RESERVED_START_KEYS
    )
    if forbidden:
        raise LockstepError(f"reserved scenario input keys are forbidden: {forbidden}")
    return values


def canonical_start_input(values: Mapping[str, Any]) -> bytes:
    try:
        admitted = validate_start_input(values)
        return json.dumps(
            admitted,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LockstepError("scenario input is not canonically encodable") from exc


def decode_canonical_start_input(encoded: bytes) -> dict[str, Any]:
    values = validate_start_input(json.loads(encoded))
    if canonical_start_input(values) != encoded:
        raise LockstepError("start admission input is not canonical")
    return values
