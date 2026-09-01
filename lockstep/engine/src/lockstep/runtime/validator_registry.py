"""Concrete non-baseline validator registry."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

DEFAULT_TIMEOUT = 600

def _resolve_path(raw: str, project: Any) -> Path:
    """Resolve `raw` against `project` (if given) and reject any resolved
    path that escapes the project root. `project is None` is the unit-test
    convenience case (no containment enforced, no project to resolve
    against)."""
    if project is None:
        return Path(raw)
    base = Path(project).resolve()
    resolved = (base / raw).resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(f"path escapes project root: {raw!r}")
    return resolved


def _get_path(check: dict, evidence: dict) -> tuple[str | None, str | None]:
    """Return (raw_path, error_reason) for a `path`/`path_from` check
    config. `path` is a literal pinned in the recipe; `path_from` pulls
    from evidence. Missing/absent -> (None, reason), never a raise."""
    if "path" in check:
        return check["path"], None
    key = check.get("path_from")
    if not key:
        return None, "missing 'path' or 'path_from'"
    if key not in evidence:
        return None, f"evidence key {key!r} not present"
    return evidence[key], None


def _default_cwd(check: dict, ctx: dict) -> Path:
    """cwd for command checks: explicit `cwd` (author-pinned, trusted, may
    be relative to project or absolute) else `_project`."""
    raw = check.get("cwd")
    project = ctx.get("_project")
    if raw:
        p = Path(raw)
        if p.is_absolute():
            return p
        return (Path(project).resolve() / raw) if project else p.resolve()
    if project:
        return Path(project).resolve()
    return Path.cwd()


# ---------------------------------------------------------------------------
# shape checks
# ---------------------------------------------------------------------------


def _check_file_exists(check: dict, evidence: dict, ctx: dict) -> list[str]:
    raw, err = _get_path(check, evidence)
    if err:
        return [f"file_exists: {err}"]
    resolved = _resolve_path(raw, ctx.get("_project"))
    # is_file(), never exists(): a DIRECTORY exists and has a non-zero
    # st_size, so `.` — the project root itself, which path containment
    # admits — would satisfy both file checks with no artifact at all.
    if not resolved.is_file():
        return [f"file_exists: {raw} is not a file"]
    return []


def _check_file_nonempty(check: dict, evidence: dict, ctx: dict) -> list[str]:
    raw, err = _get_path(check, evidence)
    if err:
        return [f"file_nonempty: {err}"]
    resolved = _resolve_path(raw, ctx.get("_project"))
    if not resolved.is_file():
        return [f"file_nonempty: {raw} is not a file"]
    if resolved.stat().st_size == 0:
        return [f"file_nonempty: {raw} is empty"]
    return []


def _check_md_has_sections(check: dict, evidence: dict, ctx: dict) -> list[str]:
    raw, err = _get_path(check, evidence)
    if err:
        return [f"md_has_sections: {err}"]
    resolved = _resolve_path(raw, ctx.get("_project"))
    if not resolved.exists():
        return [f"md_has_sections: {raw} does not exist"]
    text = resolved.read_text()
    reasons = []
    for section in check.get("sections") or []:
        pattern = re.compile(rf"^#{{1,6}}\s*{re.escape(section)}\b", re.MULTILINE | re.IGNORECASE)
        if not pattern.search(text):
            reasons.append(f"md_has_sections: missing section {section!r}")
    return reasons


def _check_file_matches(check: dict, evidence: dict, ctx: dict) -> list[str]:
    raw, err = _get_path(check, evidence)
    if err:
        return [f"file_matches: {err}"]
    regex = check.get("regex")
    if not regex:
        return ["file_matches requires 'regex'"]
    resolved = _resolve_path(raw, ctx.get("_project"))
    if not resolved.exists():
        return [f"file_matches: {raw} does not exist"]
    if not re.search(regex, resolved.read_text()):
        return [f"file_matches: content does not match {regex!r}"]
    return []


def _check_review_verdict(check: dict, evidence: dict, ctx: dict) -> list[str]:
    """Require one unambiguous PASS/FAIL verdict as the final content line."""
    raw, err = _get_path(check, evidence)
    if err:
        return [f"review_verdict: {err}"]
    expected = check.get("expected")
    if expected is not None and expected not in {"PASS", "FAIL"}:
        return ["review_verdict: expected must be PASS or FAIL"]
    resolved = _resolve_path(raw, ctx.get("_project"))
    if not resolved.is_file():
        return [f"review_verdict: {raw} is not a file"]
    text = resolved.read_text()
    verdicts = re.findall(
        r"(?m)^[ \t]*Verdict:[ \t]*(PASS|FAIL)[ \t]*$", text
    )
    if len(verdicts) != 1:
        return [f"review_verdict: expected exactly one verdict line, found {len(verdicts)}"]
    final_lines = text.rstrip().splitlines()
    final = final_lines[-1].strip() if final_lines else ""
    if final != f"Verdict: {verdicts[0]}":
        return ["review_verdict: verdict must be the final content line"]
    if expected is not None and verdicts[0] != expected:
        return [f"review_verdict: expected {expected}, found {verdicts[0]}"]
    return []


def _resolve_state_path(dotted: str, state_blob: dict) -> tuple[bool, Any]:
    """(present, value) — the flag distinguishes 'key absent' from 'value
    falsy'; a bare .get() chain conflates them into fail-open ambiguity."""
    cur: Any = state_blob
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _check_file_matches_hash(check: dict, evidence: dict, ctx: dict) -> list[str]:
    from lockstep.runtime.artifacts import ArtifactRef, ArtifactRegistry
    from lockstep.runtime.native_models import NativeCoordinate

    raw, err = _get_path(check, evidence)
    if err:
        return [f"file_matches_hash: {err}"]
    artifact_ref_from = check.get("artifact_ref_from")
    binding_name = check.get("artifact_binding")
    if (
        not isinstance(artifact_ref_from, str)
        or not artifact_ref_from
        or not isinstance(binding_name, str)
        or not binding_name
    ):
        return ["file_matches_hash requires an ArtifactRef and trusted binding"]
    state = ctx.get("_state") or {}
    present, selected_ref = _resolve_state_path(artifact_ref_from, state)
    if not present:
        raise RuntimeError(
            f"artifact ref selector '{artifact_ref_from}' not present in run state"
        )
    bindings = ctx.get("_artifact_provenance_bindings")
    expected = bindings.get(binding_name) if isinstance(bindings, dict) else None
    expected_fields = {
        "schema", "qualified_handle", "declared_name", "producer_effect_id",
        "producer_coordinate", "producer_descriptor_digest",
    }
    if (
        not isinstance(expected, dict)
        or set(expected) != expected_fields
        or expected.get("schema") != "lockstep.validator-artifact-binding/v1"
        or expected.get("qualified_handle") != binding_name
    ):
        raise RuntimeError("trusted artifact provenance binding is unavailable")
    registry = ctx.get("_artifact_registry")
    if not isinstance(registry, ArtifactRegistry):
        raise RuntimeError("trusted ArtifactRegistry is unavailable")
    try:
        coordinate_data = expected["producer_coordinate"]
        if not isinstance(coordinate_data, dict) or set(coordinate_data) != {
            "thread_id", "checkpoint_id", "checkpoint_ns", "task_id", "interrupt_id"
        }:
            raise ValueError
        coordinate = NativeCoordinate(**coordinate_data)
        record = registry.read(ArtifactRef.parse(selected_ref))
    except (KeyError, TypeError, ValueError):
        return ["file_matches_hash: artifact provenance is invalid"]
    if (
        record.producer_effect_id != expected["producer_effect_id"]
        or record.producer_coordinate != coordinate
        or record.descriptor_digest != expected["producer_descriptor_digest"]
        or record.declared_name != expected["declared_name"]
    ):
        return ["file_matches_hash: artifact provenance does not match producer"]
    resolved = _resolve_path(raw, ctx.get("_project"))
    if not resolved.is_file():
        return [f"file_matches_hash: {raw} does not exist"]
    content = resolved.read_bytes()
    if (
        hashlib.sha256(content).hexdigest() != record.blob.sha256
        or len(content) != record.blob.size
    ):
        return [f"file_matches_hash: content mismatch for {raw}"]
    return []


# ---------------------------------------------------------------------------
# command checks
# ---------------------------------------------------------------------------


def _run_cmd(
    command: str, cwd: Path, timeout: int, extra_args: list[str] | None = None
) -> subprocess.CompletedProcess:
    args = shlex.split(command) + (extra_args or [])
    return subprocess.run(
        args, cwd=str(cwd), timeout=timeout, capture_output=True, text=True
    )


def _check_cmd_ok(check: dict, evidence: dict, ctx: dict) -> list[str]:
    command = check.get("command")
    if not isinstance(command, str) or not command.strip():
        return ["cmd_ok requires a literal 'command' pinned in the recipe"]
    cwd = _default_cwd(check, ctx)
    timeout = check.get("timeout", DEFAULT_TIMEOUT)
    result = _run_cmd(command, cwd, timeout)
    if result.returncode != 0:
        return [f"cmd_ok: {command!r} exited {result.returncode}"]
    return []


def _check_git_clean(check: dict, evidence: dict, ctx: dict) -> list[str]:
    cwd = _default_cwd(check, ctx)
    timeout = check.get("timeout", DEFAULT_TIMEOUT)
    result = _run_cmd("git status --porcelain", cwd, timeout)
    if result.returncode != 0:
        return [f"git_clean: git status failed: {result.stderr.strip()}"]
    if result.stdout.strip():
        return ["git_clean: working tree not clean"]
    return []


def _state_dir() -> Path:
    # `or`, never a get() default: the plugin manifest passes
    # LOCKSTEP_STATE_DIR through unconditionally, so an unset variable
    # arrives PRESENT AND EMPTY — and `Path("")` is the cwd, i.e. the
    # project tree.
    return Path(os.environ.get("LOCKSTEP_STATE_DIR") or str(Path.home() / ".lockstep"))


def _parse_junit(xml_path: Path) -> dict[str, int]:
    root = ET.parse(xml_path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, 0) or 0)
    return totals


def _check_junit_gate(check: dict, evidence: dict, ctx: dict) -> list[str]:
    command = check.get("command")
    if not isinstance(command, str) or not command.strip():
        return ["junit_gate requires a literal 'command' pinned in the recipe"]
    if "min_tests" not in check:
        return ["junit_gate requires 'min_tests'"]
    min_tests = check["min_tests"]
    max_skipped = check.get("max_skipped")
    cwd = _default_cwd(check, ctx)
    timeout = check.get("timeout", DEFAULT_TIMEOUT)

    tmp_dir = _state_dir() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    xml_path = tmp_dir / f"{uuid.uuid4().hex}.xml"
    try:
        _run_cmd(command, cwd, timeout, extra_args=[f"--junitxml={xml_path}"])
        if not xml_path.exists():
            return ["junit_gate: no junit xml produced"]
        totals = _parse_junit(xml_path)
    finally:
        xml_path.unlink(missing_ok=True)

    reasons = []
    if totals["tests"] < min_tests:
        reasons.append(f"junit_gate: {totals['tests']} tests ran, need >= {min_tests}")
    if totals["failures"] or totals["errors"]:
        reasons.append(
            f"junit_gate: {totals['failures']} failures, {totals['errors']} errors"
        )
    if max_skipped is not None and totals["skipped"] > max_skipped:
        reasons.append(f"junit_gate: {totals['skipped']} skipped, max {max_skipped}")
    return reasons


# ---------------------------------------------------------------------------
# baseline manifest + checks
# ---------------------------------------------------------------------------




NON_BASELINE_CHECKS: dict[str, Callable[[dict, dict, dict], list[str]]] = {
    "file_exists": _check_file_exists,
    "file_nonempty": _check_file_nonempty,
    "md_has_sections": _check_md_has_sections,
    "file_matches": _check_file_matches,
    "review_verdict": _check_review_verdict,
    "file_matches_hash": _check_file_matches_hash,
    "cmd_ok": _check_cmd_ok,
    "git_clean": _check_git_clean,
    "junit_gate": _check_junit_gate,
}
