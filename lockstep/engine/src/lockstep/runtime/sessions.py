"""Session-binding sidecars — WHICH session drives WHICH run.

Native checkpoint timestamps cannot answer "is anyone actually driving this
run?": they do not tick on Write/Edit or `scenario_status` polls. The hooks
can answer it: the
platform delivers `session_id` in every hook input, the PreToolUse gate
fires on every gated tool call, and every lockstep MCP call fires
PostToolUse. This module persists that signal as one sidecar per run,
`<state_dir>/bindings/<run_id>.json` — hook-owned state, separate from the
read-only native workflow projection.

Binding rules (every mutation is a read-modify-write under the shared
crash-released advisory lock on the sidecar, published via tmp + `os.replace`):

- The OWNER (the binding names the calling session) refreshes
  `last_seen` on every touch. Ownership never lapses by idleness alone —
  staleness makes a run adoptABLE by someone else; it never evicts an
  owner nobody is competing with.
- `touch` is the explicit bind/adopt primitive. Production hooks invoke it only
  for `scenario_start`; status/observer calls never refresh or adopt a run.
  Adoption records `adopted_from` so a future explicit recovery surface can
  preserve provenance. A run whose driver is live is never rebound (`"foreign"`).
- The PreToolUse gate only ever calls `refresh_if_owner` — it NEVER binds or
  adopts. A stray Write or status call cannot silently take over an abandoned
  run.

A corrupt/unreadable sidecar reads as ABSENT: it cannot be refreshed by
anyone (so treating it as live would deadlock the run forever) and it
grants nothing (`refresh_if_owner` -> False keeps the gate closed until
a deliberate touch rebinds it).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lockstep.runtime.advisory_lock import advisory_file_lock
from lockstep.runtime.owner_state import ensure_owner_directory, seal_owner_file

# The response marker: the MCP server stamps this key into every tool
# result that names a `run_id` (server._mark), and the PostToolUse hook
# accepts a run_id from an UNRECOGNIZED mcp__ tool name only when this
# key sits beside it in the same JSON object (cli._find_marked_run_id).
# It makes binding independent of the tool-name spelling — an install
# under any server/plugin name only needs its shape added to the
# platform's PostToolUse matcher; the hook code recognizes the response
# itself. A bare `run_id` in a foreign tool's response (e.g. a file-read
# surfacing catalog data) carries no marker and binds nothing. Boundary,
# stated honestly: the marker authenticates the response SHAPE, not the
# server — a tool that deliberately replays a marked lockstep response
# (and whose name the installed matcher lets through) reads as lockstep;
# the damage ceiling is a binding touch, which never robs a live owner.
BINDING_MARKER_KEY = "lockstep_protocol"
BINDING_MARKER_VALUE = 1
MAX_SESSION_BINDING_BYTES = 64 * 1024
MAX_SESSION_ID_BYTES = 16 * 1024


def binding_path(state_dir: Path, run_id: str) -> Path:
    return Path(state_dir) / "bindings" / f"{run_id}.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _validate_session_identity(session_id: str | None) -> str:
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session identity must be a non-empty string")
    try:
        size = len(session_id.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("session identity contains invalid Unicode") from exc
    if size > MAX_SESSION_ID_BYTES:
        raise ValueError("session identity exceeds byte limit")
    return session_id


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
    be read, so "live forever" would wedge the run."""
    if not binding:
        return False
    try:
        seen = datetime.fromisoformat(binding.get("last_seen", ""))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return False
    return datetime.now(UTC) - seen <= timedelta(minutes=stale_minutes)


_REFRESH_LOCK_WAIT = 2.0


@contextmanager
def _binding_lock(path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """Apply the shared kernel mutex to the hook-owned binding namespace."""
    ensure_owner_directory(path.parent.parent, "bindings")
    with advisory_file_lock(Path(f"{path}.lock"), timeout=timeout):
        yield


def _write(path: Path, data: dict) -> None:
    encoded = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_SESSION_BINDING_BYTES:
        raise ValueError("session binding exceeds byte limit")
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_bytes(encoded)
    seal_owner_file(tmp, writable=True)
    os.replace(tmp, path)


@contextmanager
def locked_owner(
    state_dir: Path,
    run_id: str,
    session_id: str | None,
    stale_minutes: float,
) -> Iterator[None]:
    """Hold a live exact-owner binding from verification through commit."""
    path = binding_path(state_dir, run_id)
    with _binding_lock(path):
        binding = read_binding(state_dir, run_id)
        if (
            binding is None
            or not isinstance(session_id, str)
            or not session_id
            or binding["session_id"] != session_id
            or not is_live(binding, stale_minutes)
        ):
            raise PermissionError("worker session binding missing, stale, or mismatched")
        yield


def refresh_if_owner(
    state_dir: Path,
    run_id: str,
    session_id: str,
    stale_minutes: float,
) -> bool:
    """True iff the binding names a still-live `session_id`; refreshes `last_seen`.
    Never binds, never adopts — the gate's only verb."""
    _validate_session_identity(session_id)
    path = binding_path(state_dir, run_id)
    b = read_binding(state_dir, run_id)
    if b is None or b["session_id"] != session_id:
        return False                                   # cheap no-lock pre-check
    # Short, and far under the gate's hook budget: this refresh runs INSIDE
    # the PreToolUse deny path, and a hook killed at its budget emits no
    # deny at all — the gate fails open. A wedged sidecar lock must cost a
    # moment, never the whole budget.
    with _binding_lock(path, timeout=_REFRESH_LOCK_WAIT):
        b = read_binding(state_dir, run_id)
        if (
            b is None
            or b["session_id"] != session_id
            or not is_live(b, stale_minutes)
        ):
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
    session_id = _validate_session_identity(session_id)
    path = binding_path(state_dir, run_id)
    with _binding_lock(path):
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
