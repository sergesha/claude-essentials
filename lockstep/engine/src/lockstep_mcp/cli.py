"""`main()` argparse-routes the console-script verbs. `serve` (the default
when no verb is given) runs the FastMCP app over stdio.

Task 7 fills the hook/policy/doctor handlers:

- `hook-stop` / `hook-session-start` / `hook-pretool` / `hook-posttool`
  read one JSON object from stdin (best-effort — malformed/absent stdin
  degrades to `{}`, never a crash) and dispatch to `hook_stop`/
  `hook_session_start`/`hook_pretool`/`hook_posttool`. Those functions are
  the unit of testing (`tests/test_hooks_cli.py` and
  `tests/test_session_binding.py` call them directly) — the CLI wrappers
  are thin stdin/stdout plumbing.
- `policy require|clear` writes/removes an owner-authored `policy.d/<slug>.yaml`
  file (decision 15) — the PreToolUse no-run gate reads these.
- `doctor` is a diagnostic report (dirs exist, installed version
  self-report — distribution is the plugin's own cloned files run via
  `uv run`, so there is no version pin to check against) plus the loud
  detector for the silent-lockout failure: an ACTIVE run with no binding
  sidecar means the PostToolUse hook never fired — matcher/tool-name
  mismatch — and the report names the exact matcher to fix.

Fail-open vs fail-closed (Global Constraints): PreToolUse is the only gate
that can actually stop an action, so `hook_pretool` is internally
fail-closed — any exception inside it still produces a `deny` JSON on exit
0 (never a bare crash, which non-0/2 exit codes turn into fail-OPEN per the
platform's hook contract). Stop/SessionStart can only delay/annotate, never
block a determined stop (README honesty line, Task 9) — they fail OPEN
(allow / no context) on internal error, matching the pre-Task-7 stub
contract ("never look like a failure to whatever invoked it").

Hooks are read-only on ENGINE-owned state (`runs.json`, `policy.d/`,
checkpoints) — they never mutate a run. Their one OWN write is the
session-binding sidecar tree (`bindings/`, `sessions.py`): the PreToolUse
gate refreshes the owner's liveness stamp, `hook_posttool` binds/adopts on
lockstep MCP tool touches. Hook death is silent — nothing here observes
it; the engine's evidence gate is the load-bearing layer and does not
depend on hooks firing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import yaml

from lockstep_mcp import __version__, sessions
from lockstep_mcp.runs import ACTIVE_STATUS, RunIndex

# ---------------------------------------------------------------------------
# shared paths / env
# ---------------------------------------------------------------------------


def _state_dir() -> Path:
    return Path(os.environ.get("LOCKSTEP_STATE_DIR", str(Path.home() / ".lockstep")))


def _recipes_dir() -> Path:
    return Path(os.environ.get("LOCKSTEP_RECIPES", str(Path.cwd() / ".lockstep" / "recipes")))


def _policy_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "policy.d"


def _runs_json_path(state_dir: Path) -> Path:
    return Path(state_dir) / "runs.json"


def _session_stale_minutes() -> float:
    """The ONE liveness window: a run's driving session counts as live
    while its binding's `last_seen` is within this many minutes. It ticks
    on every gated tool call AND every lockstep MCP call — unlike
    `RunRecord.updated`, which only moves on step transitions. Must exceed
    the longest silent gap of a genuinely working session (a single long
    Bash call; subcall waits are covered by status-poll refreshes)."""
    try:
        return float(os.environ.get("LOCKSTEP_SESSION_STALE_MINUTES", "30"))
    except ValueError:
        return 30.0


def _project_matches(run_project: str, cwd: str) -> bool:
    """decision 11 / review M8: resolved equality OR run_project is a
    parent of cwd (cwd may be a subdirectory of the run's project)."""
    try:
        rp = Path(run_project).resolve()
        cp = Path(cwd).resolve()
    except Exception:  # noqa: BLE001 - unresolvable path never matches
        return False
    return cp == rp or rp in cp.parents


# ---------------------------------------------------------------------------
# fast path (review-2 minor): both runs.json and policy.d/ empty/absent ->
# skip all further work.
# ---------------------------------------------------------------------------


def _fast_path_empty(state_dir: Path) -> bool:
    runs_path = _runs_json_path(state_dir)
    runs_empty = True
    if runs_path.exists():
        try:
            runs_empty = not json.loads(runs_path.read_text())
        except Exception:  # noqa: BLE001 - unreadable/corrupt runs.json: don't fast-path past it
            runs_empty = False
    policy_dir = _policy_dir(state_dir)
    policy_empty = not policy_dir.exists() or not any(policy_dir.glob("*.yaml"))
    return runs_empty and policy_empty


# ---------------------------------------------------------------------------
# Stop hook
# ---------------------------------------------------------------------------


def hook_stop(stdin_json: dict, state_dir: Path, cwd: str) -> tuple[int, str]:
    state_dir = Path(state_dir)
    if _fast_path_empty(state_dir):
        return 0, ""

    try:
        if stdin_json.get("stop_hook_active"):
            return 0, ""

        idx = RunIndex(state_dir)
        matches = [r for r in idx.list(active_only=True) if _project_matches(r.project, cwd)]
        if not matches:
            return 0, ""

        # Task 8 (m8.5): ONE line per run, not one joined sentence — a run
        # parked in a subcall must NOT be told to scenario_done (that call
        # is refused while the subcall is in flight).
        lines = []
        for r in matches:
            b = r.brief or {}
            if b.get("step") == "_subcall":
                lines.append(
                    f"lockstep: run {r.run_id} — subcall in progress: "
                    f"{b.get('node')} ({b.get('runner')}) — check scenario_status; "
                    "done/escalate/abort are refused until the subcall completes."
                )
            else:
                lines.append(
                    f"lockstep: active run(s) awaiting a report — {r.run_id} "
                    f"(step: {r.step}). Report the step via scenario_done with "
                    "evidence, scenario_escalate if blocked, or scenario_abort "
                    "to cancel the run."
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
        idx = RunIndex(state_dir)
        matches = [r for r in idx.list(active_only=True) if _project_matches(r.project, cwd)]
        if not matches:
            return ""

        # C3: a spawned child session inherits LOCKSTEP_CHILD_RUN — mark
        # that run as THIS session's own, so the child never has to guess
        # which listed run is its (it also gets the id in the engine
        # preamble; this is the survives-compaction copy).
        own_run = os.environ.get("LOCKSTEP_CHILD_RUN")
        lines = []
        for r in matches:
            # Liveness hint from the binding sidecar, not RunRecord.updated
            # (which does not tick during real work): a run whose driver is
            # silent/absent is adoptable — tell the new session its door.
            binding = sessions.read_binding(state_dir, r.run_id)
            suffix = ("" if sessions.is_live(binding, stale_minutes) else
                      " (no live driving session — a scenario_status call on it adopts it)")
            if r.run_id == own_run:
                suffix += (" — THIS SESSION'S OWN child run: your session holds its "
                           "credential; drive and report it here")
            b = r.brief or {}
            if b.get("step") == "_subcall":
                # Task 8 (I8.1): name the subcall, never the raw '_subcall'
                # marker — it is machinery, not a work step.
                lines.append(
                    f"lockstep: run {r.run_id} — subcall in progress: "
                    f"{b.get('node')} ({b.get('runner')}){suffix} "
                    "— check via scenario_status"
                )
            else:
                lines.append(
                    f"lockstep: run {r.run_id} awaiting step {r.step!r}{suffix} "
                    "— check via scenario_status"
                )
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - SessionStart cannot block; fail OPEN (no context) on error
        return ""


# ---------------------------------------------------------------------------
# PreToolUse hook (decision 15) — the only gate that can actually stop an
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


def _child_chain_unlocked(idx: RunIndex, child_run: str, recipe: str, cwd: str) -> bool:
    """Walk `parent_run` from the session's own run to the root. Unlocked
    iff every run on the chain is awaiting and the root is a run of the
    policy recipe in this project. Fail closed: unknown id, cycle, or any
    terminal ancestor (a cascade that has not reaped this descendant yet
    must not hold the gate open) all deny. No timestamp check: a parent's
    `updated` does not tick while its child legitimately works, so age
    measures nothing here — the credential (LOCKSTEP_CHILD_RUN, held only
    by the spawned process) plus chain aliveness is the whole predicate."""
    seen: set[str] = set()
    try:
        rec = idx.get(child_run)
    except KeyError:
        return False
    while True:
        if rec.run_id in seen or rec.status != ACTIVE_STATUS:
            return False
        seen.add(rec.run_id)
        if rec.parent_run is None:
            return rec.recipe == recipe and _project_matches(rec.project, cwd)
        try:
            rec = idx.get(rec.parent_run)
        except KeyError:
            return False


def hook_pretool(stdin_json: dict, state_dir: Path) -> tuple[int, str]:
    state_dir = Path(state_dir)
    try:
        cwd = stdin_json.get("cwd") or os.getcwd()
        policy_dir = _policy_dir(state_dir)
        if not policy_dir.exists():
            return 0, ""

        matching_policy: dict | None = None
        for f in sorted(policy_dir.glob("*.yaml")):
            doc = yaml.safe_load(f.read_text()) or {}
            project = doc.get("project")
            if project and _project_matches(project, cwd):
                matching_policy = doc
                break

        if matching_policy is None:
            return 0, ""

        recipe = matching_policy.get("recipe")
        idx = RunIndex(state_dir)
        child_run = os.environ.get("LOCKSTEP_CHILD_RUN")
        if child_run:
            # I2: a spawned child session (this hook inherits the session's
            # env, so LOCKSTEP_CHILD_RUN names ITS run) is unlocked only
            # through its own ancestry — every run on the chain still
            # awaiting, terminating in an awaiting run of the policy recipe
            # in this project. A worker-visible awaiting policy run
            # elsewhere in the project does NOT unlock a child whose own
            # chain is dead. Session bindings play no part here: the env
            # credential, held only by the spawned process, already binds
            # this session to its run more tightly than a sidecar could.
            if _child_chain_unlocked(idx, child_run, recipe, cwd):
                return 0, ""
            return _deny(
                f"lockstep policy: this session's child run {child_run} has no "
                f"awaiting ancestry chain to a run of recipe {recipe}"
            )
        candidates = [
            r for r in idx.list(active_only=True)
            if _project_matches(r.project, cwd) and r.recipe == recipe
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
        for r in candidates:
            if sessions.refresh_if_owner(state_dir, r.run_id, session_id):
                return 0, ""
        stale_minutes = _session_stale_minutes()
        r = candidates[0]
        if sessions.is_live(sessions.read_binding(state_dir, r.run_id), stale_minutes):
            return _deny(
                f"lockstep policy: run {r.run_id} of recipe {recipe} is being driven "
                "by another live session — writes here belong to that session. If it "
                f"is truly gone it falls silent, and after {stale_minutes:g}m a "
                f"scenario_status call on {r.run_id} adopts the run; or scenario_abort "
                "it and scenario_start a fresh run"
            )
        return _deny(
            f"lockstep policy: run {r.run_id} of recipe {recipe} has no live driving "
            f"session — call scenario_status on {r.run_id} to adopt it, or "
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
# scenario_status polls included, so a parent waiting out a long subcall
# stays visibly live. Adoption (sessions.touch) also lives here and ONLY
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
# `lockstep-mcp doctor` detects the missed-binding state and says exactly
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
    (a file-read surfacing runs.json, an unrelated tool's own run ids)
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


def _posttool_run_id(tool_input, tool_response) -> str | None:
    if isinstance(tool_input, dict):
        v = tool_input.get("run_id")
        if isinstance(v, str) and v:
            return v
    return _find_run_id(tool_response)


def hook_posttool(stdin_json: dict, state_dir: Path) -> None:
    try:
        state_dir = Path(state_dir)
        tool_name = str(stdin_json.get("tool_name") or "")
        if not tool_name.startswith("mcp__"):
            return
        if _LOCKSTEP_TOOL_RE.fullmatch(tool_name):
            # Known lockstep name shape: trusted by name, permissive
            # extraction (input run_id, else any run_id in the response).
            run_id = _posttool_run_id(stdin_json.get("tool_input"),
                                      stdin_json.get("tool_response"))
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
        # Only a real, still-awaiting run is worth a binding — a failed call
        # (run_id in the input but no such run) or a terminal run binds
        # nothing. runs.json stays read-only here.
        idx = RunIndex(state_dir)
        try:
            record = idx.get(run_id)
        except KeyError:
            return
        if record.status != ACTIVE_STATUS:
            return
        sessions.touch(state_dir, run_id, session_id, _session_stale_minutes())
    except Exception:  # noqa: BLE001 - observer hook: never look like a failure
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
    _policy_dir(state_dir).mkdir(parents=True, exist_ok=True)
    path = _policy_path(state_dir, project)
    path.write_text(yaml.safe_dump({"project": str(Path(project).resolve()), "recipe": recipe}))
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
# version-pin check: distribution is the plugin's own cloned files run via
# `uv run --project ${CLAUDE_PLUGIN_ROOT}/engine`, so there is nothing
# external to fall out of sync with. NOT implemented: effective-settings
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

    # Binding liveness: every ACTIVE run must have a session-binding
    # sidecar — it is written by the PostToolUse hook on the very
    # scenario_start that created the run, so its absence proves the hook
    # never fired (matcher/tool-name mismatch), the exact silent-lockout
    # failure this check exists to make loud.
    try:
        active = RunIndex(state_dir).list(active_only=True) if _runs_json_path(state_dir).exists() else []
    except Exception as exc:  # noqa: BLE001 - unreadable index is itself a finding
        active = []
        check("runs index readable", False, f"{_runs_json_path(state_dir)}: {exc}")
    for r in active:
        binding = sessions.read_binding(state_dir, r.run_id)
        if binding is None:
            check(
                f"run {r.run_id} has a session binding", False,
                f"active run (recipe {r.recipe}) with no bindings/{r.run_id}.json: the "
                "PostToolUse binding hook never fired, so the policy gate denies every "
                "session, including the one that started the run. The installed "
                "PostToolUse matcher must match this installation's lockstep tool "
                f"names (shipped matcher: {LOCKSTEP_TOOL_MATCHER}). Find the real "
                "name in the session's tool list (it ends in __scenario_start) and "
                "add its prefix followed by .* to the PostToolUse matcher in the "
                "plugin's hooks/hooks.json or your settings hooks — responses are "
                "marker-verified, no code change needed. Then a scenario_status "
                f"call on {r.run_id} binds it",
            )
        else:
            live = sessions.is_live(binding, _session_stale_minutes())
            check(
                f"run {r.run_id} has a session binding", True,
                f"session {binding['session_id']}"
                + ("" if live else " (silent past the stale window — adoptable)"),
            )

    lines.append(f"installed version: {__version__}")

    header = "lockstep doctor: " + ("all green" if ok else "issues found")
    return ok, header + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI verb wrappers
# ---------------------------------------------------------------------------


def _read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
        if not raw or not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - absent/unreadable/malformed stdin degrades to {}
        return {}


def _cmd_serve(args: argparse.Namespace) -> int:
    from lockstep_mcp.server import app

    app.run()
    return 0


def _cmd_hook_stop(args: argparse.Namespace) -> int:
    stdin_json = _read_stdin_json()
    cwd = stdin_json.get("cwd") or os.getcwd()
    _exit_code, out = hook_stop(stdin_json, _state_dir(), cwd)
    if out:
        sys.stdout.write(out)
    return 0


def _cmd_hook_session_start(args: argparse.Namespace) -> int:
    stdin_json = _read_stdin_json()
    cwd = stdin_json.get("cwd") or os.getcwd()
    text = hook_session_start(_state_dir(), cwd)
    if text:
        payload = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
        sys.stdout.write(json.dumps(payload))
    return 0


def _cmd_hook_pretool(args: argparse.Namespace) -> int:
    stdin_json = _read_stdin_json()
    _exit_code, out = hook_pretool(stdin_json, _state_dir())
    if out:
        sys.stdout.write(out)
    return 0


def _cmd_hook_posttool(args: argparse.Namespace) -> int:
    hook_posttool(_read_stdin_json(), _state_dir())
    return 0


def _cmd_policy(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None)
    if action == "require":
        policy_require(_state_dir(), args.project, args.recipe)
    elif action == "clear":
        policy_clear(_state_dir(), args.project)
    else:
        print("usage: lockstep-mcp policy require --project PATH --recipe NAME")
        print("       lockstep-mcp policy clear --project PATH")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    ok, report = doctor(_state_dir(), _recipes_dir())
    print(report)
    # m8: exit 1 on issues found — a CI/operator invocation must be able to
    # gate on this without scraping the report text. Distinct from the
    # hook verbs (which must never look like a crash): doctor is a
    # deliberate diagnostic command, run on purpose, not a hook the
    # platform's fail-open/closed contract applies to.
    return 0 if ok else 1


_HANDLERS = {
    "serve": _cmd_serve,
    "hook-stop": _cmd_hook_stop,
    "hook-session-start": _cmd_hook_session_start,
    "hook-pretool": _cmd_hook_pretool,
    "hook-posttool": _cmd_hook_posttool,
    "policy": _cmd_policy,
    "doctor": _cmd_doctor,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lockstep-mcp")
    parser.add_argument("--version", action="store_true", help="print the installed version and exit")
    sub = parser.add_subparsers(dest="verb")
    for verb in _HANDLERS:
        if verb == "policy":
            policy_parser = sub.add_parser("policy")
            policy_sub = policy_parser.add_subparsers(dest="action")
            require_parser = policy_sub.add_parser("require")
            require_parser.add_argument("--project", required=True)
            require_parser.add_argument("--recipe", required=True)
            clear_parser = policy_sub.add_parser("clear")
            clear_parser.add_argument("--project", required=True)
        elif verb == "doctor":
            sub.add_parser("doctor")
        else:
            sub.add_parser(verb)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args, _unknown = parser.parse_known_args(argv)
    if getattr(args, "version", False):
        print(__version__)
        return 0
    verb = args.verb or "serve"
    return _HANDLERS[verb](args)


if __name__ == "__main__":
    sys.exit(main())
