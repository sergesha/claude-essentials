"""Session-binding sidecars — WHICH session drives WHICH run.

`RunRecord.updated` cannot answer "is anyone actually driving this run?":
it does not tick on Write/Edit, on `scenario_status` polls, or while a
subcall runs — only on step transitions. The hooks can answer it: the
platform delivers `session_id` in every hook input, the PreToolUse gate
fires on every gated tool call, and every lockstep MCP call fires
PostToolUse. This module persists that signal as one sidecar per run,
`<state_dir>/bindings/<run_id>.json` — hook-OWNED state (hooks stay
read-only on `runs.json`; the engine neither reads nor writes bindings).

Binding rules (every mutation is a read-modify-write under
`locking.file_lock` on the sidecar, published via tmp + `os.replace`):

- The OWNER (the binding names the calling session) refreshes
  `last_seen` on every touch. Ownership never lapses by idleness alone —
  staleness makes a run adoptABLE by someone else; it never evicts an
  owner nobody is competing with.
- `touch` binds an UNBOUND run to the toucher, and ADOPTS a run whose
  driver has been silent longer than the liveness window — the
  crash-recovery door (a resumed conversation carries a NEW session id).
  Adoption records `adopted_from` so a takeover is provenance, never a
  silent swap. A run whose driver is live is never rebound (`"foreign"`).
- The PreToolUse gate only ever calls `refresh_if_owner` — it NEVER
  binds or adopts. Adoption requires the deliberate lockstep-tool touch
  (PostToolUse), so a stray Write in the project cannot silently take
  over an abandoned run; the gate's deny message names that door.

A corrupt/unreadable sidecar reads as ABSENT: it cannot be refreshed by
anyone (so treating it as live would deadlock the run forever) and it
grants nothing (`refresh_if_owner` -> False keeps the gate closed until
a deliberate touch rebinds it).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lockstep_mcp.locking import file_lock

# The response marker: the MCP server stamps this key into every tool
# result that names a `run_id` (server._mark), and the PostToolUse hook
# accepts a run_id from an UNRECOGNIZED mcp__ tool name only when this
# key sits beside it in the same JSON object (cli._find_marked_run_id).
# It makes binding independent of the tool-name spelling — an install
# under any server/plugin name only needs its shape added to the
# platform's PostToolUse matcher; the hook code recognizes the response
# itself. A bare `run_id` in a foreign tool's response (e.g. a file-read
# surfacing runs.json) carries no marker and binds nothing. Boundary,
# stated honestly: the marker authenticates the response SHAPE, not the
# server — a tool that deliberately replays a marked lockstep response
# (and whose name the installed matcher lets through) reads as lockstep;
# the damage ceiling is a binding touch, which never robs a live owner.
BINDING_MARKER_KEY = "lockstep_protocol"
BINDING_MARKER_VALUE = 1


def binding_path(state_dir: Path, run_id: str) -> Path:
    return Path(state_dir) / "bindings" / f"{run_id}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_binding(state_dir: Path, run_id: str) -> dict | None:
    try:
        data = json.loads(binding_path(state_dir, run_id).read_text())
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("session_id"), str) and data["session_id"]:
        return data
    return None


def is_live(binding: dict | None, stale_minutes: float) -> bool:
    """A binding is live while its `last_seen` is within the window. An
    unparsable stamp is NOT live — the owner cannot refresh what cannot
    be read, so "live forever" would wedge the run; "adoptable" heals."""
    if not binding:
        return False
    try:
        seen = datetime.fromisoformat(binding.get("last_seen", ""))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) - seen <= timedelta(minutes=stale_minutes)


_REFRESH_LOCK_WAIT = 2.0


def _write(path: Path, data: dict) -> None:
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def refresh_if_owner(state_dir: Path, run_id: str, session_id: str) -> bool:
    """True iff the binding names `session_id`; refreshes `last_seen`.
    Never binds, never adopts — the gate's only verb."""
    path = binding_path(state_dir, run_id)
    b = read_binding(state_dir, run_id)
    if b is None or b["session_id"] != session_id:
        return False                                   # cheap no-lock pre-check
    # Short, and far under the gate's hook budget: this refresh runs INSIDE
    # the PreToolUse deny path, and a hook killed at its budget emits no
    # deny at all — the gate fails open. A wedged sidecar lock must cost a
    # moment, never the whole budget.
    with file_lock(path, timeout=_REFRESH_LOCK_WAIT):
        b = read_binding(state_dir, run_id)
        if b is None or b["session_id"] != session_id:
            return False                               # re-verified under the lock
        b["last_seen"] = _now_iso()
        _write(path, b)
    return True


def touch(state_dir: Path, run_id: str, session_id: str, stale_minutes: float) -> str:
    """The PostToolUse verb: owner -> refresh (`"owned"`); unbound/corrupt
    -> bind (`"bound"`); silent past the window -> adopt (`"adopted"`,
    with `adopted_from` provenance); live foreign owner -> no-op
    (`"foreign"`). Verdict formed UNDER the sidecar lock, so two racing
    adopters resolve to exactly one owner."""
    path = binding_path(state_dir, run_id)
    with file_lock(path):
        b = read_binding(state_dir, run_id)
        now = _now_iso()
        if b is not None and b["session_id"] == session_id:
            b["last_seen"] = now
            _write(path, b)
            return "owned"
        if b is not None and is_live(b, stale_minutes):
            return "foreign"
        new = {"session_id": session_id, "bound_at": now, "last_seen": now}
        if b is not None:
            new["adopted_from"] = b["session_id"]
        _write(path, new)
        return "adopted" if b is not None else "bound"
