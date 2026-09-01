"""Shape-only scenario dry-run evaluation without a runtime owner."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from lockstep.runtime import evidence as evidence_mod
from lockstep.runtime import validators
from lockstep.runtime._service_payloads import validate_evidence_shape
from lockstep.runtime.recipe_bundles import RecipeBundleStore

SHAPE_CHECK_TYPES = frozenset(
    ("file_exists", "file_nonempty", "md_has_sections", "file_matches")
)


def _reserved_evidence_error(evidence: dict) -> dict | None:
    forged = [key for key in evidence if key.startswith("_")]
    if not forged:
        return None
    return {
        "accepted": False,
        "errors": [f"reserved evidence key(s) rejected: {sorted(forged)}"],
    }


def prevalidate_scenario_evidence(evidence: dict) -> tuple[dict, dict | None]:
    """Bound untrusted evidence before any ambient workspace lookup."""
    raw_evidence = validate_evidence_shape(evidence)
    return raw_evidence, _reserved_evidence_error(raw_evidence)


def _materialize_brief(
    recipes_dir: Path,
    recipe: str,
    step: str,
    load_step_brief: Callable[[Path, str], dict | None],
    preflight: Callable[[Path, str], object],
) -> dict | None:
    authorized = preflight(recipes_dir, recipe)
    with tempfile.TemporaryDirectory(prefix="lockstep-dryrun-") as raw:
        store = RecipeBundleStore(Path(raw) / "owner-state")
        materialized = authorized.capture(store).materialize(store)
        return load_step_brief(materialized.source_path, step)


def _shape_check_result(check: dict, evidence: dict, context: dict) -> dict:
    check_type = check.get("type")
    if check_type not in SHAPE_CHECK_TYPES:
        return {"type": check_type, "verdict": "skipped (dryrun)"}
    implementation = validators.CHECKS.get(check_type)
    try:
        reasons = (
            implementation(check, evidence, context)
            if implementation
            else [f"unknown check type: {check_type!r}"]
        )
    except Exception as exc:  # noqa: BLE001 - dry-run reports pinned check errors
        return {
            "type": check_type,
            "verdict": "error",
            "reasons": [str(exc)],
        }
    return {
        "type": check_type,
        "verdict": "pass" if not reasons else "fail",
        "reasons": reasons,
    }


def _shape_results(brief: dict, evidence: dict, context: dict) -> list[dict]:
    return [
        _shape_check_result(check, evidence, context)
        for check in brief.get("checks") or []
    ]


def evaluate_scenario_dryrun(
    recipes_dir: Path,
    recipe: str,
    step: str,
    evidence: dict,
    *,
    project_root: Path,
    containment_errors: Callable[[dict | None, dict, str], list[str]],
    load_step_brief: Callable[[Path, str], dict | None],
    preflight: Callable[[Path, str], object],
) -> dict:
    raw_evidence, reserved_error = prevalidate_scenario_evidence(evidence)
    if reserved_error is not None:
        return reserved_error
    brief = _materialize_brief(
        recipes_dir,
        recipe,
        step,
        load_step_brief,
        preflight,
    )
    if brief is None:
        raise ValueError(f"step {step!r} not found in recipe {recipe!r}")
    schema = brief.get("evidence_schema")
    schema_errors = evidence_mod.validate_evidence(schema, raw_evidence)
    if schema_errors:
        return {"accepted": False, "errors": schema_errors}
    project = str(project_root)
    path_errors = containment_errors(schema, raw_evidence, project)
    if path_errors:
        return {"accepted": False, "errors": path_errors}
    return {
        "accepted": True,
        "results": _shape_results(brief, raw_evidence, {"_project": project}),
    }
