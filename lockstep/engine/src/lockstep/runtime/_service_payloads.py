"""Bounded public payload validation for command mutations."""

from __future__ import annotations

from typing import Any

from lockstep.runtime.errors import LockstepError
from lockstep.runtime.payload_limits import PayloadLimitExceeded, bounded_json


def validate_evidence_payload(evidence: object) -> dict[str, Any]:
    value = validate_evidence_shape(evidence)
    if any(key.startswith("_") for key in value):
        raise LockstepError("reserved evidence keys are forbidden")
    return value

def validate_evidence_shape(evidence: object) -> dict[str, Any]:
    try:
        value = bounded_json(evidence, label="scenario evidence")
    except PayloadLimitExceeded as exc:
        raise LockstepError(str(exc)) from exc
    if not isinstance(value, dict):
        raise LockstepError("scenario evidence must be a JSON object")
    return value

def validate_reason_payload(reason: object) -> str:
    try:
        value = bounded_json(reason, label="scenario reason")
    except PayloadLimitExceeded as exc:
        raise LockstepError(str(exc)) from exc
    if not isinstance(value, str):
        raise LockstepError("scenario reason must be a string")
    return value
