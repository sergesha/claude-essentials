"""Evidence adaptation for manual interrupt / built-in verdict-relay recipes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lockstep.runtime._service_payloads import validate_evidence_payload
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.evidence import project_path_errors, validate_evidence
from lockstep.runtime.validator_execution import manual_check_verdict


def validated_manual_evidence(
    brief: object, result: Mapping[str, Any], project: str
) -> dict[str, Any]:
    """Supply only engine-validated inner evidence to the graph's relay node."""
    if not isinstance(brief, dict):
        raise LockstepError("manual interrupt has no evidence contract")
    evidence = validate_evidence_payload(result.get("evidence"))
    schema = brief.get("evidence_schema")
    try:
        errors = validate_evidence(schema, evidence)
        errors.extend(project_path_errors(schema, evidence, project))
    except Exception as exc:
        raise LockstepError("invalid manual evidence contract") from exc
    if errors:
        raise LockstepError("manual evidence rejected: " + "; ".join(errors))
    verdict = manual_check_verdict(brief.get("checks"), evidence, project)
    if verdict["verdict_status"] == "error":
        raise LockstepError(
            "manual evidence rejected: " + "; ".join(verdict["verdict_reasons"])
        )
    return {
        **evidence,
        "_verdict_status": verdict["verdict_status"],
        "_verdict_reasons": verdict["verdict_reasons"],
    }
