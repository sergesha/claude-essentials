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
gate refreshes the owner's liveness stamp, and `hook_posttool` binds only a
newly started run. Status never refreshes or adopts ownership. Hook death is silent — nothing here observes
it; the engine's evidence gate is the load-bearing layer and does not
depend on hooks firing.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

from lockstep import __version__
from lockstep.recipe.loader import RecipeError, RecipeLoader
from lockstep.runtime import sessions
from lockstep.runtime._hook_posttool_decision import (
    _posttool_identity,
    _bindable_start,
)
from lockstep.runtime._hook_pretool_decision import decide_pretool
from lockstep.runtime._hook_stop_decision import (
    _matching_stop_runs,
    _render_stop_decision,
)
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
from lockstep.runtime.owner_state import (
    StorageLimitExceeded,
    take_bounded,
    verify_owner_directory,
)

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

        return _render_stop_decision(
            _matching_stop_runs(
                _active_native(state_dir),
                state_dir=state_dir,
                cwd=cwd,
                session_id=stdin_json.get("session_id"),
                stale_minutes=_session_stale_minutes(),
                project_matches=_project_matches,
                owned_by_another=_owned_by_another_live_session,
            )
        )
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
                else " (no live driving session — start a fresh run)"
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
        reason = decide_pretool(
            stdin_json,
            state_dir,
            policy_dir_for=_policy_dir,
            project_matches=_project_matches,
            active_native=_active_native,
            stale_minutes_for=_session_stale_minutes,
        )
        return (0, "") if reason is None else _deny(reason)
    except Exception:  # noqa: BLE001 - fail-closed: internal error must never fail-open
        return _deny("lockstep: internal error — failing closed")


# ---------------------------------------------------------------------------
# PostToolUse hook — the binding writer. Fires on lockstep MCP tools only
# (hooks.json matcher; re-checked here — by name for the known shapes via
# LOCKSTEP_TOOL_MATCHER, by the server-stamped response marker for any
# other mcp__ name a user-extended matcher lets through). This is where a
# run gets BOUND to the session driving it only at scenario_start (run_id read
# from the tool response). Later status/observer calls never refresh or adopt
# ownership. Pure observer:
# no output, fail-OPEN on any internal error.
# ---------------------------------------------------------------------------


# The lockstep MCP tools carry a different name prefix per install shape:
# a `.mcp.json` server entry named "lockstep" yields `mcp__lockstep__<tool>`
# (verified live 2026-08-07); a plugin-manifest install yields
# `mcp__plugin_<plugin>_<server>__<tool>`. The
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

    `scenario_start` accepts only the server-stamped response identity; its
    untrusted input can never select or redirect a session binding. Other
    callers retain their explicitly named run after marker lookup.
    """
    marked = _find_marked_run_id(tool_response)
    if marked:
        return marked
    if tool_name.endswith("__scenario_start"):
        return None
    if isinstance(tool_input, dict):
        value = tool_input.get("run_id")
        if isinstance(value, str) and value:
            return value
    return _find_run_id(tool_response)


def hook_posttool(stdin_json: dict, state_dir: Path) -> None:
    try:
        state_dir = Path(state_dir)
        identity = _posttool_identity(
            stdin_json,
            known_tool=lambda name: _LOCKSTEP_TOOL_RE.fullmatch(name) is not None,
            posttool_run_id=_posttool_run_id,
            find_marked_run_id=_find_marked_run_id,
        )
        if identity is None:
            return
        run_id, session_id = identity
        # Bind the newly started root before asynchronous engine work reaches
        # its first manual step. Native children have no public run identity.
        projected = {
            binding.public_run_id: status
            for binding, status in read_only_statuses(state_dir)
        }
        if not _bindable_start(projected, run_id):
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


_ORPHAN_RECOVERY = (
    "Use a pre-simplification Lockstep build against the original exact project "
    "directory identity and this state directory, complete recovery there, and retry. "
    "Do not delete transaction.json manually."
)


def _read_only_legacy_authoring_diagnostics(state_dir: Path) -> tuple[str, ...]:
    """Bounded presence-only discovery for doctor; never creates owner state."""
    from lockstep.authoring_publisher import _legacy_evidence_note

    if not state_dir.exists() and not state_dir.is_symlink():
        return ()
    verify_owner_directory(state_dir)
    authoring = state_dir / "authoring"
    if not authoring.exists() and not authoring.is_symlink():
        return ()
    verify_owner_directory(authoring)
    try:
        namespaces = sorted(
            take_bounded(authoring.iterdir(), 256, "authoring namespaces"),
            key=lambda path: path.name,
        )
    except StorageLimitExceeded:
        return (
            (
                "legacy authoring evidence audit is bounded to 256 namespaces; "
                f"transaction evidence may remain undiscovered. {_ORPHAN_RECOVERY}"
            ),
        )
    findings: list[str] = []
    for namespace in namespaces:
        if len(namespace.name) != 64 or any(c not in "0123456789abcdef" for c in namespace.name):
            raise ValueError("authoring namespace name is invalid")
        verify_owner_directory(namespace)
        note = _legacy_evidence_note(namespace)
        if note is not None:
            findings.append(
                "legacy authoring transaction evidence is present in owner-state "
                f"namespace {namespace.name}; it may be a v4 transaction. "
                f"{_ORPHAN_RECOVERY}{note}"
            )
    return tuple(findings)


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
    try:
        legacy_findings = _read_only_legacy_authoring_diagnostics(state_dir)
    except Exception:  # noqa: BLE001 - unreadable owner state is a doctor finding
        legacy_findings = (
            (
                "read-only legacy authoring evidence audit failed; transaction "
                "evidence may remain undiscovered. Use a pre-simplification Lockstep "
                "build against the original exact project directory identity and "
                "this state directory. Do not delete transaction.json manually."
            ),
        )
    if legacy_findings:
        for finding in legacy_findings:
            check("legacy authoring evidence absent", False, finding)
    else:
        check("legacy authoring evidence absent", True)

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
                "marker-verified, no code change needed. Then start a fresh run",
            )
        else:
            live = sessions.is_live(binding, _session_stale_minutes())
            check(
                f"run {run_id} has a session binding",
                live,
                "binding present and live"
                if live
                else "binding is stale; start a fresh run",
            )

    lines.append(f"installed version: {__version__}")

    header = "lockstep doctor: " + ("all green" if ok else "issues found")
    return ok, header + "\n" + "\n".join(lines)
