"""Deterministic check registry + run_checks.

Two distinct execution modes live behind one function (the
explicit-execution contract):

- `run_checks(state, execute=True)` — the ENGINE's direct call. Reads checks
  from `state["brief"]["checks"]` + evidence from `state["evidence"]`, runs
  every check exactly once, and returns a fresh verdict. Engine-supplied
  baseline context (`_project` / `_baseline_start` / `_baseline_prev` /
  `_baseline_globs`) rides inside `state` — never injected as graph state.
- `run_checks(state)` (execute defaults False) — the IN-GRAPH node's call
  (`validate_one` in the fixtures). It never executes checks itself: it
  only republishes the verdict the engine already embedded in the resume
  payload's evidence (`evidence["_verdict_status"]`/`_verdict_reasons`). No
  embedded verdict -> `error` (anti-forgery: combined with the reserved `_`
  evidence-key prefix rejected upstream in the engine, a forged verdict can
  never reach this path with a status the graph will trust).

Verdict shape is FLAT and unconditional:
`{"verdict_status": "pass"|"fail"|"error", "verdict_reasons": [str]}`.

`error` means a check RAISED, OR a baseline check's target is not covered
by `baseline_globs` (the coverage predicate — never a vacuous
pass on out-of-glob artifacts). `error` short-circuits the whole check pass:
no further checks run, and none of the accumulated `fail` reasons from
earlier checks in the same pass are reported (an `error`
consumes no retry budget, so which ordinary failures were also present is
moot — the run doesn't resume either way).

Fail-closed rules: no checks configured, an unknown check type, or a
`path_from` key missing from evidence all yield an ordinary `fail` with a
reason — never a raise, never a silent pass.

`unchanged` checks are deferred to the END of a check pass regardless of
their position in the recipe's `checks:` list, and re-hash AFTER every
`cmd_ok`/`junit_gate` in the same pass has run (TOCTOU guard) — a command
that mutates a "frozen" file earlier in the list must still be caught.

Every path a check consumes (`path`, `path_from` evidence, baseline check
targets) is re-resolved against `_project` and re-contained inside it here,
regardless of what the engine already did — defense in depth, not the
primary gate.
"""

from __future__ import annotations

import fnmatch
import glob as glob_mod
import hashlib
import json
import os
import re
import shlex
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

DEFAULT_TIMEOUT = 600

# default ignore set for baseline manifests.
_IGNORE_DIR_NAMES = {"__pycache__", ".git"}


# ---------------------------------------------------------------------------
# path handling
# ---------------------------------------------------------------------------


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
    raw, err = _get_path(check, evidence)
    if err:
        return [f"file_matches_hash: {err}"]
    hash_from = check.get("hash_from")
    if not isinstance(hash_from, str) or not hash_from:
        return ["file_matches_hash requires 'hash_from'"]
    present, expected = _resolve_state_path(hash_from, ctx.get("_state") or {})
    if not present:
        # RuntimeError -> run_checks' blanket except -> error verdict: no
        # resume, no retry-budget burn
        raise RuntimeError(f"hash pin '{hash_from}' not present in run state")
    if not isinstance(expected, str) or not expected:
        raise RuntimeError(f"hash pin '{hash_from}' present but not a hex digest: {expected!r}")
    resolved = _resolve_path(raw, ctx.get("_project"))
    if not resolved.exists():
        return [f"file_matches_hash: {raw} does not exist"]
    actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if actual != expected:
        return [f"file_matches_hash: hash mismatch for {raw} — "
                "artifact changed after the subcall pinned it"]
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


def _is_ignored(rel_path: str) -> bool:
    parts = rel_path.split("/")
    if any(part in _IGNORE_DIR_NAMES for part in parts):
        return True
    return rel_path.endswith(".pyc")


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(project: Path, globs: list[str]) -> dict[str, str]:
    """Hash every file under `project` matching any pattern in `globs`
    (the default ignore set applied; symlinks not followed). Returns
    {relative_posix_path: sha256_hex}."""
    project = Path(project).resolve()
    manifest: dict[str, str] = {}
    for pattern in globs:
        # py3.11+ glob() excludes dotfiles/dot-dirs by default; without
        # include_hidden a hidden file inside a declared glob is invisible
        # to the manifest, so editing it never trips `unchanged`/`fresh`/
        # `changed_in`/`diff_only`. The explicit ignore set below still
        # applies on top (__pycache__/, .git/, *.pyc).
        for match in glob_mod.glob(str(project / pattern), recursive=True, include_hidden=True):
            p = Path(match)
            if p.is_symlink() or not p.is_file():
                continue
            try:
                rel = p.resolve().relative_to(project).as_posix()
            except ValueError:
                # Reached through a symlinked PARENT directory (is_symlink()
                # only tests the leaf), so the real file lives outside the
                # project. Same rule as a symlinked leaf — not followed, not
                # hashed. Raising here would be an agent-triggerable wedge:
                # one `ln -s` inside a baseline glob turns every check into a
                # permanent `error` verdict, which neither resumes nor burns
                # retry budget.
                continue
            if _is_ignored(rel):
                continue
            manifest[rel] = _hash_file(p)
    return manifest


def _load_manifest(path: Any) -> dict[str, str]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _seg_match(path_segs: list[str], glob_segs: list[str]) -> bool:
    if not glob_segs:
        return not path_segs
    if glob_segs[0] == "**":
        rest = glob_segs[1:]
        return any(_seg_match(path_segs[i:], rest) for i in range(len(path_segs) + 1))
    if not path_segs:
        return False
    if not fnmatch.fnmatchcase(path_segs[0], glob_segs[0]):
        return False
    return _seg_match(path_segs[1:], glob_segs[1:])


def _glob_match(rel_path: str, pattern: str) -> bool:
    """Match the way `glob.glob(recursive=True)` does — `*` never crosses a
    `/`, only `**` does. `fnmatch` alone treats the whole path as one string,
    so `src/*` there matches `src/a/b.py`, which `build_manifest` would never
    have hashed: selection from a manifest and the manifest itself would
    disagree, and untouched nested files get reported as changed."""
    return _seg_match(
        [s for s in rel_path.split("/") if s],
        [s for s in pattern.split("/") if s],
    )


def _covered_by_globs(rel_path: str, globs: list[str]) -> bool:
    return any(_glob_match(rel_path, g) for g in globs)


def _path_covered(declared_path: str, baseline_globs: list[str]) -> bool:
    """True when SOME path at or under `declared_path` can match a baseline
    glob. Segment-wise, so a leading-wildcard glob (`**/*.py`, `*.md`) is
    covering rather than skipped — a literal-prefix test reads those as
    "prefix is empty, cover nothing" and wedges every `changed_in`/
    `diff_only` in the recipe on a permanent error verdict."""
    dsegs = [s for s in declared_path.split("/") if s]
    for g in baseline_globs:
        gsegs = [s for s in g.split("/") if s]
        i = 0
        ok = True
        while i < len(dsegs) and i < len(gsegs):
            if gsegs[i] == "**":
                break
            if not fnmatch.fnmatchcase(dsegs[i], gsegs[i]):
                ok = False
                break
            i += 1
        if ok:
            return True
    return False


def _check_fresh(check: dict, evidence: dict, ctx: dict) -> list[str]:
    raw, err = _get_path(check, evidence)
    if err:
        return [f"fresh: {err}"]
    project = ctx.get("_project")
    resolved = _resolve_path(raw, project)
    rel = resolved.relative_to(Path(project).resolve()).as_posix()
    baseline_globs = ctx.get("_baseline_globs") or []
    if not _covered_by_globs(rel, baseline_globs):
        raise ValueError(f"fresh: path {rel!r} not covered by baseline_globs")
    if not resolved.exists():
        return [f"fresh: {raw} does not exist"]
    start_manifest = _load_manifest(ctx.get("_baseline_start"))
    old_hash = start_manifest.get(rel)
    if old_hash is not None and old_hash == _hash_file(resolved):
        return [f"fresh: {rel} unchanged since run start"]
    return []


def _check_unchanged(check: dict, evidence: dict, ctx: dict) -> list[str]:
    glob_pat = check.get("glob")
    if not glob_pat:
        return ["unchanged requires 'glob'"]
    since = check.get("since", "start")
    project = ctx.get("_project")
    baseline_globs = ctx.get("_baseline_globs") or []
    manifest_path = ctx.get("_baseline_start") if since == "start" else ctx.get("_baseline_prev")
    if not manifest_path:
        raise ValueError(f"unchanged: no baseline manifest available for since={since!r}")

    selected = _load_manifest(manifest_path)
    matched_entries = {p: h for p, h in selected.items() if _glob_match(p, glob_pat)}
    if not matched_entries and glob_pat not in baseline_globs:
        raise ValueError(f"unchanged: glob {glob_pat!r} not covered by baseline_globs")

    current = build_manifest(project, [glob_pat])
    reasons = []
    for rel in sorted(set(matched_entries) | set(current)):
        if matched_entries.get(rel) != current.get(rel):
            reasons.append(f"unchanged: {rel} changed")
    return reasons


def _under_prefix(rel: str, declared: str) -> bool:
    """A bare `rel.startswith(declared)` treats `declared` as a
    raw string prefix, so `paths: ["src"]` wrongly covers `src-evil/...`
    too (same characters, different directory). Normalize `declared` to
    end with `/` before the prefix check — an exact match on `declared`
    itself (a file path, not a directory) still counts. `rel` is always a
    posix-relative manifest key (`build_manifest` writes `.as_posix()`),
    so `/` — not `os.sep` — is the correct separator here regardless of
    platform."""
    if rel == declared:
        return True
    prefix = declared if declared.endswith("/") else declared + "/"
    return rel.startswith(prefix)


def _check_changed_in(check: dict, evidence: dict, ctx: dict) -> list[str]:
    paths = check.get("paths")
    if not paths:
        return ["changed_in requires 'paths'"]
    since = check.get("since", "start")
    project = ctx.get("_project")
    baseline_globs = ctx.get("_baseline_globs") or []
    for declared in paths:
        if not _path_covered(declared, baseline_globs):
            raise ValueError(f"changed_in: path {declared!r} not covered by baseline_globs")

    manifest_path = ctx.get("_baseline_start") if since == "start" else ctx.get("_baseline_prev")
    if not manifest_path:
        raise ValueError(f"changed_in: no baseline manifest available for since={since!r}")

    selected = _load_manifest(manifest_path)
    current = build_manifest(project, baseline_globs)
    changed = [
        rel
        for rel in set(selected) | set(current)
        if any(_under_prefix(rel, p) for p in paths) and selected.get(rel) != current.get(rel)
    ]
    if not changed:
        return [f"changed_in: no changes detected under {paths}"]
    return []


def _check_diff_only(check: dict, evidence: dict, ctx: dict) -> list[str]:
    paths = check.get("paths")
    if not paths:
        return ["diff_only requires 'paths'"]
    project = ctx.get("_project")
    baseline_globs = ctx.get("_baseline_globs") or []
    for declared in paths:
        if not _path_covered(declared, baseline_globs):
            raise ValueError(f"diff_only: path {declared!r} not covered by baseline_globs")

    prev_manifest_path = ctx.get("_baseline_prev")
    if not prev_manifest_path:
        raise ValueError("diff_only: no previous baseline available")

    selected = _load_manifest(prev_manifest_path)
    current = build_manifest(project, baseline_globs)
    reasons = []
    for rel in sorted(set(selected) | set(current)):
        if selected.get(rel) != current.get(rel) and not any(_under_prefix(rel, p) for p in paths):
            reasons.append(f"diff_only: unexpected change outside {paths}: {rel}")
    return reasons


# ---------------------------------------------------------------------------
# registry + run_checks
# ---------------------------------------------------------------------------

CHECKS: dict[str, Callable[[dict, dict, dict], list[str]]] = {
    "file_exists": _check_file_exists,
    "file_nonempty": _check_file_nonempty,
    "md_has_sections": _check_md_has_sections,
    "file_matches": _check_file_matches,
    "file_matches_hash": _check_file_matches_hash,
    "cmd_ok": _check_cmd_ok,
    "git_clean": _check_git_clean,
    "junit_gate": _check_junit_gate,
    "fresh": _check_fresh,
    "unchanged": _check_unchanged,
    "changed_in": _check_changed_in,
    "diff_only": _check_diff_only,
}


def run_checks(state: dict[str, Any], execute: bool = False) -> dict[str, Any]:
    if not execute:
        # In-graph republish path: never executes checks itself (decision
        # 16). No embedded verdict -> error (anti-forgery).
        evidence = state.get("evidence") or {}
        status = evidence.get("_verdict_status")
        if status is None:
            return {"verdict_status": "error", "verdict_reasons": ["no verdict embedded in evidence"]}
        reasons = evidence.get("_verdict_reasons") or []
        return {"verdict_status": status, "verdict_reasons": list(reasons)}

    brief = state.get("brief") or {}
    checks = brief.get("checks") or []
    evidence = state.get("evidence") or {}
    ctx = {
        "_project": state.get("_project"),
        "_baseline_start": state.get("_baseline_start"),
        "_baseline_prev": state.get("_baseline_prev"),
        "_baseline_globs": state.get("_baseline_globs") or [],
        "_state": state.get("_state") or {},
    }

    if not checks:
        return {"verdict_status": "fail", "verdict_reasons": ["no checks configured"]}

    reasons: list[str] = []
    deferred: list[dict] = []
    try:
        for check in checks:
            ctype = check.get("type")
            if ctype == "unchanged":
                # TOCTOU guard: unchanged re-hashes AFTER every command in
                # this pass, regardless of its position in the list.
                deferred.append(check)
                continue
            fn = CHECKS.get(ctype)
            if fn is None:
                reasons.append(f"unknown check type: {ctype!r}")
                continue
            reasons.extend(fn(check, evidence, ctx))
        for check in deferred:
            reasons.extend(_check_unchanged(check, evidence, ctx))
    except Exception as e:  # noqa: BLE001 - deliberate: any raise -> error verdict
        return {"verdict_status": "error", "verdict_reasons": [str(e)]}

    if reasons:
        return {"verdict_status": "fail", "verdict_reasons": reasons}
    return {"verdict_status": "pass", "verdict_reasons": []}
