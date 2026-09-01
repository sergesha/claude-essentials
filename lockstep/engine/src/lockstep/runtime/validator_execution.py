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
