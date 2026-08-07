"""`main()` argparse-routes the console-script verbs. `serve` (the default
when no verb is given) runs the FastMCP app over stdio.

Task 7 fills the hook/policy/doctor handlers:

- `hook-stop` / `hook-session-start` / `hook-pretool` read one JSON object
  from stdin (best-effort — malformed/absent stdin degrades to `{}`, never
  a crash) and dispatch to `hook_stop`/`hook_session_start`/`hook_pretool`.
  Those three functions are the unit of testing (`tests/test_hooks_cli.py`
  calls them directly) — the CLI wrappers are thin stdin/stdout plumbing.
- `policy require|clear` writes/removes an owner-authored `policy.d/<slug>.yaml`
  file (decision 15) — the PreToolUse no-run gate reads these.
- `doctor` is a v1-trimmed diagnostic report (dirs exist, heartbeat age,
  installed version self-report — distribution is the plugin's own cloned
  files run via `uv run`, so there is no version pin to check against).

Fail-open vs fail-closed (Global Constraints): PreToolUse is the only gate
that can actually stop an action, so `hook_pretool` is internally
fail-closed — any exception inside it still produces a `deny` JSON on exit
0 (never a bare crash, which non-0/2 exit codes turn into fail-OPEN per the
platform's hook contract). Stop/SessionStart can only delay/annotate, never
block a determined stop (README honesty line, Task 9) — they fail OPEN
(allow / no context) on internal error, matching the pre-Task-7 stub
contract ("never look like a failure to whatever invoked it").

Every hook handler appends one best-effort JSONL heartbeat line, including
on the fast path — the heartbeat is the CI liveness signal that the hook
wiring itself fired, independent of whether any lockstep run is active, so
it happens BEFORE the "nothing configured, bail early" check skips
everything else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from lockstep_mcp import __version__
from lockstep_mcp.locking import file_lock
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
# heartbeat (best-effort, no locking — decision per Task 7 plan text)
# ---------------------------------------------------------------------------

_HEARTBEAT_ROTATE_ABOVE = 1000
_HEARTBEAT_KEEP = 200


def _heartbeat(state_dir: Path, event: str) -> None:
    try:
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / "heartbeat.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps({"event": event, "ts": _now_iso()}) + "\n")
        _rotate_heartbeat(path)
    except Exception:  # noqa: BLE001 - heartbeat is best-effort, failures ignored
        pass


def _rotate_heartbeat(path: Path) -> None:
    # Task 8: concurrent child servers are normal now — the read→trim→replace
    # runs under the sidecar lock so two rotators can't splice each other's
    # os.replace. A LockTimeout lands in the blanket except: rotation simply
    # skips that tick. Appends stay lock-free append-only, as in v1.
    try:
        with file_lock(path, timeout=1.0):
            lines = path.read_text().splitlines(keepends=True)
            if len(lines) > _HEARTBEAT_ROTATE_ABOVE:
                keep = lines[-_HEARTBEAT_KEEP:]
                tmp = path.parent / (path.name + ".tmp")
                tmp.write_text("".join(keep))
                os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - concurrent-append loss during rotation is accepted
        pass


# ---------------------------------------------------------------------------
# fast path (review-2 minor): both runs.json and policy.d/ empty/absent ->
# skip all further work. Heartbeat is written by the caller BEFORE this
# check, not after — it is the one thing that must fire unconditionally.
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
    _heartbeat(state_dir, "Stop")

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
    _heartbeat(state_dir, "SessionStart")

    if _fast_path_empty(state_dir):
        return ""

    try:
        stale_hours = float(os.environ.get("LOCKSTEP_STALE_HOURS", "24"))
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
            suffix = (" (expired — scenario_abort it and scenario_start a fresh run)"
                      if _is_expired(r.updated, stale_hours) else "")
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


def _is_expired(updated_iso: str, hours: float) -> bool:
    try:
        updated = datetime.fromisoformat(updated_iso)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - updated > timedelta(hours=hours)
    except Exception:  # noqa: BLE001 - unparsable timestamp: never flagged expired
        return False


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


def _child_chain_unlocked(idx: RunIndex, child_run: str, recipe: str, cwd: str,
                          stale_hours: float) -> bool:
    """Walk `parent_run` from the session's own run to the root. Unlocked
    iff every run on the chain is awaiting AND unexpired (updated within
    `stale_hours`) and the root is a run of the policy recipe in this
    project. Fail closed: unknown id, cycle, any terminal ancestor (a
    cascade that has not reaped this descendant yet must not hold the gate
    open), or any expired ancestor (an abandoned run is dead — abort and
    re-enter through the recipe) all deny."""
    seen: set[str] = set()
    try:
        rec = idx.get(child_run)
    except KeyError:
        return False
    while True:
        if rec.run_id in seen or rec.status != ACTIVE_STATUS or _is_expired(rec.updated, stale_hours):
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
    _heartbeat(state_dir, "PreToolUse")

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
        # Run expiry (fail-closed): an awaiting run satisfies the gate only
        # until `updated` is older than LOCKSTEP_STALE_HOURS. An abandoned
        # run (dead worker; nothing writes it again) stays `awaiting`
        # forever, and before this rule it held the gate open indefinitely.
        # An expired run is dead — the only exit is scenario_abort plus a
        # fresh scenario_start; scenario_status does NOT refresh `updated`
        # (RunIndex.update is the sole writer), so checking status never
        # reopens the gate. Same threshold hook_session_start flags with.
        # Known interaction: a finished child's _nudge_ancestors poll
        # advances a parked parent and stamps its `updated` fresh,
        # restarting that parent's expiry clock — session binding (future
        # work) is the real fix, not a tweak here.
        stale_hours = float(os.environ.get("LOCKSTEP_STALE_HOURS", "24"))
        idx = RunIndex(state_dir)
        child_run = os.environ.get("LOCKSTEP_CHILD_RUN")
        if child_run:
            # I2: a spawned child session (this hook inherits the session's
            # env, so LOCKSTEP_CHILD_RUN names ITS run) is unlocked only
            # through its own ancestry — every run on the chain still
            # awaiting and fresh, terminating in an awaiting run of the
            # policy recipe in this project. A worker-visible awaiting
            # policy run elsewhere in the project does NOT unlock a child
            # whose own chain is dead.
            if _child_chain_unlocked(idx, child_run, recipe, cwd, stale_hours):
                return 0, ""
            return _deny(
                f"lockstep policy: this session's child run {child_run} has no "
                f"awaiting, unexpired ancestry chain to a run of recipe {recipe}"
            )
        candidates = [
            r for r in idx.list(active_only=True)
            if _project_matches(r.project, cwd) and r.recipe == recipe
        ]
        if any(not _is_expired(r.updated, stale_hours) for r in candidates):
            return 0, ""
        if candidates:
            return _deny(
                f"lockstep policy: run {candidates[0].run_id} of recipe {recipe} has "
                f"expired (awaiting, but no update in {stale_hours:g}h) — an expired "
                "run is dead: scenario_abort it, then scenario_start a fresh run of "
                "the recipe"
            )
        return _deny(f"lockstep policy: start recipe {recipe} via scenario_start first")
    except Exception:  # noqa: BLE001 - fail-closed: internal error must never fail-open
        return _deny("lockstep: internal error — failing closed")


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
# doctor (v1 trimmed — dirs exist, heartbeat age, installed version
# self-report. No version-pin check: distribution is the plugin's own
# cloned files run via `uv run --project ${CLAUDE_PLUGIN_ROOT}/engine`, so
# there is nothing external to fall out of sync with. NOT implemented:
# effective-settings inspection, handler self-exec — those are v2.)
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

    installed = __version__

    heartbeat_path = state_dir / "heartbeat.jsonl"
    if not heartbeat_path.exists():
        lines.append("[SKIP] heartbeat — no heartbeat.jsonl yet")
    else:
        try:
            last = None
            for line in heartbeat_path.read_text().splitlines():
                if line.strip():
                    last = line
            if last is None:
                check("heartbeat present", False, "heartbeat.jsonl is empty")
            else:
                entry = json.loads(last)
                ts = datetime.fromisoformat(entry["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - ts
                stale_hours = float(os.environ.get("LOCKSTEP_STALE_HOURS", "24"))
                is_stale = age > timedelta(hours=stale_hours)
                detail = f"last event {age.total_seconds():.0f}s ago"
                if is_stale:
                    detail += f" — stale (over {stale_hours}h)"
                check("heartbeat recent", not is_stale, detail)
        except Exception as exc:  # noqa: BLE001 - report, don't crash doctor
            check("heartbeat readable", False, str(exc))

    lines.append(f"installed version: {installed}")

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
