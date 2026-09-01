"""Baseline manifests, glob coverage, and baseline validators."""

from __future__ import annotations

import fnmatch
import glob as glob_mod
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from lockstep.runtime.validator_registry import _get_path, _resolve_path

_IGNORE_DIR_NAMES = {"__pycache__", ".git"}

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
        # A manifest the engine NAMED but that is not there is a broken
        # state dir, never an empty project: answering `{}` makes `fresh`
        # pass on any path (nothing to compare against) and turns every
        # baseline guarantee vacuous, silently and in the agent's favour.
        raise ValueError(f"baseline manifest missing: {p}")
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
    # A DIRECTORY, or a path the manifest ignore set drops, is never in the
    # baseline — `old_hash is None` would then short-circuit to a pass, and
    # the step closes with no artifact produced at all. Both are the
    # agent's path to fix, so they FAIL (budget burns, run resumes) rather
    # than error.
    if not resolved.is_file():
        return [f"fresh: {rel} is not a file"]
    if _is_ignored(rel):
        return [f"fresh: {rel} is excluded from baseline manifests"]
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
    if not matched_entries and not _path_covered(glob_pat, baseline_globs):
        # Semantic, not byte-equality: a well-formed `src/vendor/**` under
        # `baseline_globs: ["src/**"]` that happens to match nothing YET
        # would otherwise raise on every done() — a permanent error verdict,
        # which never resumes and never burns budget.
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




BASELINE_CHECKS: dict[str, Callable[[dict, dict, dict], list[str]]] = {
    "fresh": _check_fresh,
    "unchanged": _check_unchanged,
    "changed_in": _check_changed_in,
    "diff_only": _check_diff_only,
}
