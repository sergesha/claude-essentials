"""Fail-open/fail-closed hook and policy behavior for the runtime.

The hook/policy/doctor functions:

- `hook-stop` / `hook-session-start` / `hook-pretool` / `hook-posttool`
  read one JSON object from stdin (best-effort — malformed/absent stdin
  degrades to `{}`, never a crash) and dispatch to `hook_stop`/
  `hook_session_start`/`hook_pretool`/`hook_posttool`. Those functions are
  the unit of testing (`tests/test_hooks_cli.py` and
  `tests/test_session_binding.py` call them directly) — the CLI wrappers
  are called by the CLI's thin stdin/stdout plumbing.
- `policy require|clear` writes/removes an owner-authored `policy.d/<slug>.yaml`
  file — the PreToolUse no-run gate reads these.
- `doctor` is a diagnostic report (dirs exist and Lockstep version
  self-report; dependency patch state is enforced earlier by bootstrap) plus the loud
  detector for the silent-lockout failure: an ACTIVE run with no binding
  sidecar means the PostToolUse hook never fired — matcher/tool-name
  mismatch — and the report names the exact matcher to fix.

Fail-open vs fail-closed (Global Constraints): PreToolUse is the only gate
that can actually stop an action, so `hook_pretool` is internally
fail-closed — any exception inside it still produces a `deny` JSON on exit
0 (never a bare crash, which non-0/2 exit codes turn into fail-OPEN per the
platform's hook contract). Stop/SessionStart can only delay/annotate, never
block a determined stop (the README says so plainly) — they fail OPEN
(allow / no context) on internal error: a hook must never look like a
failure to whatever invoked it.

Hooks are read-only on engine-owned catalog/checkpoint state and policy files;
they never mutate a run. Their one own write is the
session-binding sidecar tree (`bindings/`, `sessions.py`): the PreToolUse
gate refreshes the owner's liveness stamp, `hook_posttool` binds/adopts on
lockstep MCP tool touches. Hook death is silent — nothing here observes
it; the engine's evidence gate is the load-bearing layer and does not
depend on hooks firing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import yaml

from lockstep import __version__
from lockstep.recipe.loader import RecipeError, RecipeLoader
from lockstep.runtime import sessions
from lockstep.runtime.config import (
    policy_dir as _policy_dir,
)
from lockstep.runtime.config import (
    project_matches as _project_matches,
)
from lockstep.runtime.config import (
    recipes_dir as _recipes_dir,
)
from lockstep.runtime.config import (
    session_stale_minutes as _session_stale_minutes,
)
from lockstep.runtime.hook_projection import read_only_statuses

# ---------------------------------------------------------------------------
# fast path: both native catalog and policy.d/ empty/absent ->
# skip all further work.
# ---------------------------------------------------------------------------


def _fast_path_empty(state_dir: Path) -> bool:
    catalog_empty = not (state_dir / "runtime.sqlite").exists()
    policy_dir = _policy_dir(state_dir)
    policy_empty = not policy_dir.exists() or not any(policy_dir.glob("*.yaml"))
    return catalog_empty and policy_empty


def _active_native(state_dir: Path):
    return tuple(
        (binding, status)
        for binding, status in read_only_statuses(state_dir)
        if status.status in {"starting", "awaiting", "running"}
    )


# ---------------------------------------------------------------------------
# Stop hook
# ---------------------------------------------------------------------------


def _owned_by_another_live_session(state_dir: Path, run_id: str, session_id: str,
                                   stale_minutes: float) -> bool:
    binding = sessions.read_binding(state_dir, run_id)
    if binding is None or binding.get("session_id") == session_id:
        return False
    return sessions.is_live(binding, stale_minutes)


def hook_stop(stdin_json: dict, state_dir: Path, cwd: str) -> tuple[int, str]:
    state_dir = Path(state_dir)
    if _fast_path_empty(state_dir):
        return 0, ""

    try:
        if stdin_json.get("stop_hook_active"):
            return 0, ""

        matches = [
            (binding, status)
            for binding, status in _active_native(state_dir)
            if _project_matches(binding.project_identity, cwd)
        ]
        session_id = stdin_json.get("session_id")
        stale_minutes = _session_stale_minutes()
        if isinstance(session_id, str) and session_id:
            matches = [
                (binding, status)
                for binding, status in matches
                if not _owned_by_another_live_session(
                    state_dir, binding.public_run_id, session_id, stale_minutes
                )
            ]
        if not matches:
            return 0, ""

        lines = []
        for binding, status in matches:
            if status.status == "awaiting":
                lines.append(
                    f"lockstep: active run(s) awaiting a report — "
                    f"{binding.public_run_id} (step: {status.step}). Report the step "
                    "via scenario_done with evidence, scenario_escalate if blocked, "
                    "or scenario_abort to cancel the run."
                )
            else:
                lines.append(
                    f"lockstep: run {binding.public_run_id} is {status.status} under "
                    "engine ownership — check scenario_status before stopping."
                )
        return 0, json.dumps({"decision": "block", "reason": " ".join(lines)})
    except Exception:  # noqa: BLE001 - Stop can only delay a turn; fail OPEN on internal error
        return 0, ""


# ---------------------------------------------------------------------------
# SessionStart hook
# ---------------------------------------------------------------------------


def hook_session_start(state_dir: Path, cwd: str) -> str:
    state_dir = Path(state_dir)
    if _fast_path_empty(state_dir):
        return ""

    try:
        stale_minutes = _session_stale_minutes()
        matches = [
            (binding, status)
            for binding, status in _active_native(state_dir)
            if _project_matches(binding.project_identity, cwd)
        ]
        if not matches:
            return ""

        lines = []
        for run_binding, status in matches:
            session_binding = sessions.read_binding(
                state_dir, run_binding.public_run_id
            )
            suffix = (
                ""
                if sessions.is_live(session_binding, stale_minutes)
                else " (no live driving session — a scenario_status call on it adopts it)"
            )
            if status.status == "awaiting":
                lines.append(
                    f"lockstep: run {run_binding.public_run_id} awaiting step "
                    f"{status.step!r}{suffix} — check via scenario_status"
                )
            else:
                lines.append(
                    f"lockstep: run {run_binding.public_run_id} is {status.status} "
                    f"under {status.owner} ownership{suffix} — check via scenario_status"
                )
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - SessionStart cannot block; fail OPEN (no context) on error
        return ""


# ---------------------------------------------------------------------------
# PreToolUse hook — the only gate that can actually stop an
# action, so this is internally fail-closed: ANY exception -> deny.
# ---------------------------------------------------------------------------


def _deny(reason: str) -> tuple[int, str]:
    return 0, json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def hook_pretool(stdin_json: dict, state_dir: Path) -> tuple[int, str]:
    state_dir = Path(state_dir)
    try:
        cwd = stdin_json.get("cwd") or os.getcwd()
        policy_dir = _policy_dir(state_dir)
        if not policy_dir.exists():
            return 0, ""

        matching_policy: dict | None = None
        matching_depth = -1
        for f in sorted(policy_dir.glob("*.yaml")):
            doc = yaml.safe_load(f.read_text()) or {}
            project = doc.get("project")
            if project and _project_matches(project, cwd):
                depth = len(Path(project).resolve().parts)
                if depth > matching_depth:
                    matching_policy = doc
                    matching_depth = depth

        if matching_policy is None:
            return 0, ""

        recipe = matching_policy.get("recipe")
        recipe_digest = matching_policy.get("recipe_digest")
        if (
            not isinstance(recipe, str)
            or not recipe
            or not isinstance(recipe_digest, str)
            or len(recipe_digest) != 64
        ):
            return _deny("lockstep policy: configured recipe binding is invalid")
        candidates = [
            (binding, status)
            for binding, status in _active_native(state_dir)
            if status.status == "awaiting"
            and Path(binding.project_identity).resolve()
            == Path(matching_policy["project"]).resolve()
            and binding.recipe_digest == recipe_digest
        ]
        if not candidates:
            return _deny(f"lockstep policy: start recipe {recipe} via scenario_start first")
        # Session binding: the gate asks "is THIS session the one driving a
        # run of the policy recipe here?" — never "does some awaiting run
        # exist" (which let any session in on another session's run). The
        # platform delivers session_id in every hook input; a session owns
        # a run iff the run's binding sidecar names it (sessions.py — bound
        # at scenario_start by hook_posttool, adoptable via a lockstep tool
        # touch once the driver goes silent). The gate itself NEVER binds
        # or adopts; on an owned run it refreshes the liveness stamp, so
        # the owner's real work keeps its own claim alive.
        session_id = stdin_json.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return _deny(
                "lockstep policy: hook input carried no session_id — run "
                "ownership cannot be established; failing closed"
            )
        for binding, _status in candidates:
            if sessions.refresh_if_owner(
                state_dir, binding.public_run_id, session_id
            ):
                return 0, ""
        stale_minutes = _session_stale_minutes()
        binding, _status = candidates[0]
        run_id = binding.public_run_id
        if sessions.is_live(sessions.read_binding(state_dir, run_id), stale_minutes):
            return _deny(
                f"lockstep policy: run {run_id} of recipe {recipe} is being driven "
                "by another live session — writes here belong to that session. If it "
                f"is truly gone it falls silent, and after {stale_minutes:g}m a "
                f"scenario_status call on {run_id} adopts the run; or scenario_abort "
                "it and scenario_start a fresh run"
            )
        return _deny(
            f"lockstep policy: run {run_id} of recipe {recipe} has no live driving "
            f"session — call scenario_status on {run_id} to adopt it, or "
            "scenario_abort it and scenario_start a fresh run"
        )
    except Exception:  # noqa: BLE001 - fail-closed: internal error must never fail-open
        return _deny("lockstep: internal error — failing closed")


# ---------------------------------------------------------------------------
# PostToolUse hook — the binding writer. Fires on lockstep MCP tools only
# (hooks.json matcher; re-checked here — by name for the known shapes via
# LOCKSTEP_TOOL_MATCHER, by the server-stamped response marker for any
# other mcp__ name a user-extended matcher lets through). This is where a
# run gets BOUND to the session driving it: at scenario_start (run_id read
# from the tool response) and on every later touch naming the run —
# scenario_status polls included, so a long-running driver stays visibly
# live. Adoption (sessions.touch) also lives here and ONLY
# here: taking over an abandoned run requires deliberately touching it with
# a lockstep tool, never just writing a file in the project. Pure observer:
# no output, fail-OPEN on any internal error.
# ---------------------------------------------------------------------------


# The lockstep MCP tools carry a different name prefix per install shape:
# a `.mcp.json` server entry named "lockstep" yields `mcp__lockstep__<tool>`
# (verified live 2026-08-07); a plugin-manifest install yields
# `mcp__plugin_<plugin>_<server>__<tool>` — observed live on Claude Code
# 2.1.220: `mcp__plugin_lockstep_lockstep__scenario_start` (fixture:
# tests/fixtures/hooks/posttool_scenario_start_plugin_install.json). The
# PLUGIN segment is the user's install name — free text — but the SERVER
# segment is pinned to "lockstep" by the shipped plugin manifest's
# mcpServers key, so `mcp__plugin_.+_lockstep__` covers every plugin
# install regardless of what the user named the plugin. ONE home for the
# pattern, mirrored byte-for-byte into hooks/hooks.json's PostToolUse
# matcher (pinned by test_shipped_hook_matcher_covers_install_shapes).
# A tool name OUTSIDE these shapes (e.g. a hand-written .mcp.json server
# under another key) is still accepted by hook_posttool — but only via
# the server-stamped response marker (`sessions.BINDING_MARKER_KEY`), so
# extending the platform matcher is the ONLY step such an install needs;
# `lockstep doctor` detects the missed-binding state and says exactly
# that.
LOCKSTEP_TOOL_MATCHER = r"mcp__lockstep__.*|mcp__plugin_.+_lockstep__.*"
_LOCKSTEP_TOOL_RE = re.compile(LOCKSTEP_TOOL_MATCHER)


def _find_run_id(obj, depth: int = 0) -> str | None:
    """Best-effort `run_id` in a tool response: MCP responses may arrive as
    the structured dict or as content blocks whose text is the JSON."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        v = obj.get("run_id")
        if isinstance(v, str) and v:
            return v
        for val in obj.values():
            got = _find_run_id(val, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for val in obj:
            got = _find_run_id(val, depth + 1)
            if got:
                return got
    elif isinstance(obj, str):
        s = obj.strip()
        if s[:1] in "{[":
            try:
                return _find_run_id(json.loads(s), depth + 1)
            except ValueError:
                return None
    return None


def _find_marked_run_id(obj, depth: int = 0) -> str | None:
    """`run_id` accepted ONLY from a JSON object that also carries the
    server-stamped binding marker as a SIBLING key. This is the
    name-agnostic identity predicate for tools whose name is not a known
    lockstep shape: a bare `run_id` anywhere in a foreign tool's response
    (a file-read surfacing catalog data, an unrelated tool's own run ids)
    proves nothing, and must bind nothing."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        v = obj.get("run_id")
        if (isinstance(v, str) and v
                and obj.get(sessions.BINDING_MARKER_KEY) == sessions.BINDING_MARKER_VALUE):
            return v
        for val in obj.values():
            got = _find_marked_run_id(val, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for val in obj:
            got = _find_marked_run_id(val, depth + 1)
            if got:
                return got
    elif isinstance(obj, str):
        s = obj.strip()
        if s[:1] in "{[":
            try:
                return _find_marked_run_id(json.loads(s), depth + 1)
            except ValueError:
                return None
    return None


def _posttool_run_id(tool_input, tool_response, tool_name: str = "") -> str | None:
    """The run this call TOUCHED, never merely mentioned.

    Three sources, in order of how strongly each proves a touch: the run_id
    the caller itself named; the server's stamped marker; and — only for
    `scenario_start`, the one run-owning call whose input cannot carry a
    run_id — the id in its own response. Scraping any response would bind
    this session to the first stranger's run in a `list_runs` listing, and
    adopt it outright once that run's real driver is past the silence
    window.
    """
    if isinstance(tool_input, dict):
        v = tool_input.get("run_id")
        if isinstance(v, str) and v:
            return v
    marked = _find_marked_run_id(tool_response)
    if marked:
        return marked
    if tool_name.endswith("__scenario_start"):
        return _find_run_id(tool_response)
    return None


def hook_posttool(stdin_json: dict, state_dir: Path) -> None:
    try:
        state_dir = Path(state_dir)
        tool_name = str(stdin_json.get("tool_name") or "")
        if not tool_name.startswith("mcp__"):
            return
        if _LOCKSTEP_TOOL_RE.fullmatch(tool_name):
            # Known lockstep name shape. The run id comes from what the
            # CALLER named, or from the server's own stamped marker — never
            # from scraping the response, which would bind this session to
            # the first stranger's run in a `list_runs` listing (and adopt
            # it outright once that run's real driver is past the silence
            # window).
            run_id = _posttool_run_id(stdin_json.get("tool_input"),
                                      stdin_json.get("tool_response"),
                                      tool_name)
        else:
            # Unknown MCP tool name (a custom-named install whose user
            # extended the platform matcher — or any foreign tool that
            # slipped into it): only a marker-stamped response counts.
            run_id = _find_marked_run_id(stdin_json.get("tool_response"))
        if not run_id:
            return
        session_id = stdin_json.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        # Only a real, still worker-awaiting native run is bindable. Native
        # children have no public run or credential identity.
        projected = {
            binding.public_run_id: status
            for binding, status in read_only_statuses(state_dir)
        }
        status = projected.get(run_id)
        if status is None or status.status != "awaiting" or status.owner != "worker":
            return
        sessions.touch(state_dir, run_id, session_id, _session_stale_minutes())
    except Exception:  # noqa: BLE001, S110 - observer hook must fail open
        pass


# ---------------------------------------------------------------------------
# policy require|clear
# ---------------------------------------------------------------------------


def _policy_slug(project: str) -> str:
    resolved = str(Path(project).resolve())
    return hashlib.sha256(resolved.encode()).hexdigest()[:16]


def _policy_path(state_dir: Path, project: str) -> Path:
    return _policy_dir(state_dir) / f"{_policy_slug(project)}.yaml"


def policy_require(state_dir: Path, project: str, recipe: str) -> Path:
    state_dir = Path(state_dir)
    try:
        recipe_digest = RecipeLoader(_recipes_dir()).resolve(recipe).definition_sha256
    except (OSError, RecipeError, ValueError) as exc:
        raise ValueError(f"cannot bind policy recipe {recipe!r}: {exc}") from exc
    _policy_dir(state_dir).mkdir(parents=True, exist_ok=True)
    path = _policy_path(state_dir, project)
    path.write_text(
        yaml.safe_dump(
            {
                "project": str(Path(project).resolve()),
                "recipe": recipe,
                "recipe_digest": recipe_digest,
            }
        )
    )
    return path


def policy_clear(state_dir: Path, project: str) -> None:
    path = _policy_path(Path(state_dir), project)
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# doctor — dirs exist, installed version self-report, and the LOUD check
# for the one silent failure mode observed live: an active run with no
# binding sidecar means the PostToolUse binding hook never fired for it
# (the installed matcher does not match this installation's tool names),
# and the gate will deny even the session that started the run. No
# dependency patch check here: bootstrap already performs the pure read-only
# verification before importing this module. NOT implemented: effective-settings
# inspection, handler self-exec — those are v2.
# ---------------------------------------------------------------------------


def doctor(state_dir: Path, recipes_dir: Path) -> tuple[bool, str]:
    state_dir = Path(state_dir)
    recipes_dir = Path(recipes_dir)
    lines: list[str] = []
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        suffix = f" — {detail}" if detail else ""
        lines.append(f"[{'OK' if passed else 'FAIL'}] {label}{suffix}")

    check("state dir exists", state_dir.exists(), str(state_dir))
    check("recipes dir exists", recipes_dir.exists(), str(recipes_dir))

    # Every worker-awaiting native run must have the PostToolUse-owned
    # session binding that makes the write gate usable. Engine-owned running
    # work needs no worker session binding.
    try:
        active = [
            (binding, status)
            for binding, status in read_only_statuses(state_dir)
            if status.status == "awaiting" and status.owner == "worker"
        ]
    except Exception:  # noqa: BLE001 - unreadable projection is itself a finding
        active = []
        check(
            "native run projection readable",
            False,
            "trusted native state failed read-only verification",
        )
    for run_binding, _status in active:
        run_id = run_binding.public_run_id
        binding = sessions.read_binding(state_dir, run_id)
        if binding is None:
            check(
                f"run {run_id} has a session binding", False,
                f"worker-awaiting run with no bindings/{run_id}.json: the "
                "PostToolUse binding hook never fired, so the policy gate denies every "
                "session, including the one that started the run. The installed "
                "PostToolUse matcher must match this installation's lockstep tool "
                f"names (shipped matcher: {LOCKSTEP_TOOL_MATCHER}). Find the real "
                "name in the session's tool list (it ends in __scenario_start) and "
                "add its prefix followed by .* to the PostToolUse matcher in the "
                "plugin's hooks/hooks.json or your settings hooks — responses are "
                "marker-verified, no code change needed. Then a scenario_status "
                f"call on {run_id} binds it",
            )
        else:
            live = sessions.is_live(binding, _session_stale_minutes())
            check(
                f"run {run_id} has a session binding",
                live,
                "binding present and live"
                if live
                else "binding is stale and adoptable",
            )

    lines.append(f"installed version: {__version__}")

    header = "lockstep doctor: " + ("all green" if ok else "issues found")
    return ok, header + "\n" + "\n".join(lines)
