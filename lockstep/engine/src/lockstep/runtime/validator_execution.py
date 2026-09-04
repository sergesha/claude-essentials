"""Validator pass execution and effect-integrity composition."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lockstep.runtime.manifests import (
    ProjectSnapshot,
    ProjectWritePath,
    capture_project,
    compare_effect,
)
from lockstep.runtime.validator_baselines import BASELINE_CHECKS, _check_unchanged
from lockstep.runtime.validator_registry import NON_BASELINE_CHECKS

CHECKS = {**NON_BASELINE_CHECKS, **BASELINE_CHECKS}

_MANUAL_PROJECT_READ_CHECK_TYPES = frozenset(
    (
        "file_exists",
        "file_nonempty",
        "md_has_sections",
        "file_matches",
        "review_verdict",
    )
)
_MANUAL_PROCESS_CHECK_TYPES = frozenset(("cmd_ok", "git_clean", "junit_gate"))
_MANUAL_TRUSTED_CONTEXT_CHECK_TYPES = frozenset(BASELINE_CHECKS) | {
    "file_matches_hash"
}


def _embedded_verdict(state: dict[str, Any]) -> dict[str, Any]:
    evidence = state.get("evidence") or {}
    status = evidence.get("_verdict_status")
    if status is None:
        return {
            "verdict_status": "error",
            "verdict_reasons": ["no verdict embedded in evidence"],
        }
    reasons = evidence.get("_verdict_reasons") or []
    return {"verdict_status": status, "verdict_reasons": list(reasons)}


def _validation_context(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "_project": state.get("_project"),
        "_baseline_start": state.get("_baseline_start"),
        "_baseline_prev": state.get("_baseline_prev"),
        "_baseline_globs": state.get("_baseline_globs") or [],
        "_state": state.get("_state") or {},
        "_artifact_registry": state.get("_artifact_registry"),
        "_artifact_provenance_bindings": state.get(
            "_artifact_provenance_bindings"
        ),
    }


def _execute_configured_checks(
    checks: list[dict], evidence: dict, context: dict
) -> dict[str, Any]:
    if not checks:
        return {
            "verdict_status": "fail",
            "verdict_reasons": ["no checks configured"],
        }
    reasons: list[str] = []
    deferred: list[dict] = []
    try:
        for check in checks:
            check_type = check.get("type")
            if check_type == "unchanged":
                deferred.append(check)
                continue
            implementation = CHECKS.get(check_type)
            if implementation is None:
                reasons.append(f"unknown check type: {check_type!r}")
                continue
            reasons.extend(implementation(check, evidence, context))
        for check in deferred:
            reasons.extend(_check_unchanged(check, evidence, context))
    except Exception as exc:  # noqa: BLE001 - any raise is an error verdict
        return {"verdict_status": "error", "verdict_reasons": [str(exc)]}
    if reasons:
        return {"verdict_status": "fail", "verdict_reasons": reasons}
    return {"verdict_status": "pass", "verdict_reasons": []}


def validate_manual_checks(
    checks: object, evidence: dict, project: str
) -> list[str]:
    """Run eligible project reads without conferring broader authority."""
    if checks is None:
        return []
    if not isinstance(checks, list) or any(
        not isinstance(check, dict) for check in checks
    ):
        return ["declared checks must be a list of objects"]
    if not checks:
        return []
    classification_errors: list[str] = []
    for check in checks:
        check_type = check.get("type")
        if not isinstance(check_type, str):
            classification_errors.append(f"unknown check type: {check_type!r}")
        elif check_type in _MANUAL_PROCESS_CHECK_TYPES:
            classification_errors.append(
                "manual completion cannot run declared check without pinned "
                f"execution: {check_type}"
            )
        elif check_type in _MANUAL_TRUSTED_CONTEXT_CHECK_TYPES:
            classification_errors.append(
                "manual completion lacks trusted validation context for declared "
                f"check: {check_type}"
            )
        elif check_type not in _MANUAL_PROJECT_READ_CHECK_TYPES:
            classification_errors.append(f"unknown check type: {check_type!r}")
    if classification_errors:
        return classification_errors

    context = {"_project": project}
    reasons: list[str] = []
    try:
        for check in checks:
            reasons.extend(CHECKS[check["type"]](check, evidence, context))
    except Exception as exc:  # noqa: BLE001 - manual validation fails closed
        return [str(exc)]
    return reasons


def _apply_effect_contract(
    state: dict[str, Any], verdict: dict[str, Any]
) -> dict[str, Any]:
    before = state.get("_effect_before")
    if before is None:
        return verdict
    if not isinstance(before, ProjectSnapshot):
        return {
            "verdict_status": "error",
            "verdict_reasons": ["integrity: invalid effect baseline"],
        }
    try:
        allowed = state.get("_effect_allowed") or []
        if not all(isinstance(path, ProjectWritePath) for path in allowed):
            raise TypeError("effect contract contains an invalid write path")
        outcome = state.get("_effect_outcome", verdict["verdict_status"])
        result = compare_effect(
            before,
            capture_project(Path(state["_project"])),
            allowed,
            outcome,
        )
    except Exception as exc:  # noqa: BLE001 - integrity gate fails closed
        return {
            "verdict_status": "error",
            "verdict_reasons": [f"integrity: {exc}"],
        }
    if result.integrity_error:
        return {
            "verdict_status": "error",
            "verdict_reasons": list(result.reasons),
        }
    verdict["effect_baseline_eligible"] = result.baseline_eligible
    return verdict


def run_checks(state: dict[str, Any], execute: bool = False) -> dict[str, Any]:
    if not execute:
        return _embedded_verdict(state)
    brief = state.get("brief") or {}
    checks = brief.get("checks") or []
    evidence = state.get("evidence") or {}
    verdict = _execute_configured_checks(
        checks, evidence, _validation_context(state)
    )
    return _apply_effect_contract(state, verdict)
