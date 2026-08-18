"""Session binding: the PreToolUse write-gate asks "is the session asking
to write the one DRIVING this run?" — not "does some awaiting run exist".

The platform delivers `session_id` in every hook input; `sessions.py`
persists per-run binding sidecars (hook-owned; runs.json stays read-only
to hooks). Rules under test:

- the owner session (binding names it) writes freely; every gated call
  refreshes its liveness stamp.
- any other session is denied while the owner is live — regardless of how
  fresh or stale `RunRecord.updated` is (the 24h expiry rule is deleted:
  `updated` does not tick during real work, so it measured nothing).
- adoption (crash recovery) happens ONLY through a deliberate lockstep
  tool touch (PostToolUse), and only once the owner has been silent
  longer than LOCKSTEP_SESSION_STALE_MINUTES — the gate itself never
  adopts, and a live owner can never be robbed.
- the spawned-child path (LOCKSTEP_CHILD_RUN + ancestry chain) is
  untouched by bindings; the no-policy path is untouched by everything.

`sessions` is imported lazily inside the tests that need the new API, so
running this file against pre-binding code shows BEHAVIORAL failures for
the gate flips and ImportError only where the API itself is the missing
piece.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import lockstep_mcp.cli as cli
from lockstep_mcp import sessions
from lockstep_mcp.runs import RunIndex

S1 = "session-aaaa-1111"
S2 = "session-bbbb-2222"

# ---------------------------------------------------------------------------
# helpers (mirror test_hooks_cli.py's; kept local — tests dir is not a package)
# ---------------------------------------------------------------------------


def _mk_run(state_dir: Path, project: str, step: str = "one", recipe: str = "feature-dev") -> str:
    idx = RunIndex(state_dir)
    record = idx.create(recipe, project)
    idx.update(record.run_id, step=step, brief={"step": step, "task": "t", "exit_criterion": "x"})
    return record.run_id


def _write_policy(state_dir: Path, project: str, recipe: str) -> Path:
    policy_dir = state_dir / "policy.d"
    policy_dir.mkdir(parents=True, exist_ok=True)
    path = policy_dir / f"{cli._policy_slug(project)}.yaml"
    path.write_text(yaml.safe_dump({"project": str(Path(project).resolve()), "recipe": recipe}))
    return path


def _set_updated(state_dir: Path, run_id: str, iso: str) -> None:
    path = state_dir / "runs.json"
    data = json.loads(path.read_text())
    data[run_id]["updated"] = iso
    path.write_text(json.dumps(data))


def _setup(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    state = tmp_path / "state"
    _write_policy(state, str(proj), "feature-dev")
    return proj, state


def _pretool(state: Path, proj: Path, session_id: str | None):
    stdin = {"cwd": str(proj)}
    if session_id is not None:
        stdin["session_id"] = session_id
    return cli.hook_pretool(stdin, state)


def _posttool(state: Path, proj: Path, session_id: str,
              tool: str = "mcp__lockstep__scenario_status",
              tool_input: dict | None = None, tool_response=None):
    return cli.hook_posttool(
        {"cwd": str(proj), "session_id": session_id, "tool_name": tool,
         "tool_input": tool_input or {}, "tool_response": tool_response or {}},
        state,
    )


def _bind(state: Path, run_id: str, session_id: str) -> None:
    from lockstep_mcp import sessions

    assert sessions.touch(state, run_id, session_id, 30.0) in ("bound", "adopted")


def _age_binding(state: Path, run_id: str, minutes: float) -> None:
    from lockstep_mcp import sessions

    p = sessions.binding_path(state, run_id)
    data = json.loads(p.read_text())
    data["last_seen"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    p.write_text(json.dumps(data))


def _denied(out: str) -> str:
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    return data["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# the owner writes; anyone else does not
# ---------------------------------------------------------------------------


def test_owner_session_is_allowed_and_refreshed(tmp_path):
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))
    _bind(state, run_id, S1)
    before = sessions.read_binding(state, run_id)["last_seen"]
    time.sleep(0.01)

    code, out = _pretool(state, proj, S1)

    assert code == 0 and out == ""
    after = sessions.read_binding(state, run_id)["last_seen"]
    assert after > before          # the gate itself keeps the owner's liveness ticking


def test_second_session_denied_while_owner_live(tmp_path):
    # THE core flip: today any session in the project is unlocked by another
    # session's awaiting run. Bound + live owner => everyone else is denied.
    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))
    _bind(state, run_id, S1)

    code, out = _pretool(state, proj, S2)

    assert code == 0
    reason = _denied(out)
    assert run_id in reason


def test_unbound_run_denies_and_names_the_adoption_door(tmp_path):
    # No binding at all (pre-upgrade run, or the PostToolUse bind never
    # fired): fail closed, but tell the session exactly how to adopt —
    # the gate itself never adopts.
    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))

    code, out = _pretool(state, proj, S2)

    assert code == 0
    reason = _denied(out)
    assert run_id in reason and "scenario_status" in reason


def test_missing_session_id_fails_closed(tmp_path):
    # The platform contract delivers session_id in every hook input; its
    # absence is ambiguity, and ambiguity fails closed.
    proj, state = _setup(tmp_path)
    _mk_run(state, str(proj.resolve()))

    code, out = _pretool(state, proj, session_id=None)

    assert code == 0
    _denied(out)


# ---------------------------------------------------------------------------
# binding at birth + liveness via lockstep-tool touches (PostToolUse)
# ---------------------------------------------------------------------------


def test_scenario_start_response_binds_the_starting_session(tmp_path):
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))

    _posttool(state, proj, S1, tool="mcp__lockstep__scenario_start",
              tool_input={"recipe": "feature-dev"},
              tool_response={"run_id": run_id, "step": "one"})

    assert sessions.read_binding(state, run_id)["session_id"] == S1
    code, out = _pretool(state, proj, S1)
    assert code == 0 and out == ""


def test_bind_reads_run_id_from_text_wrapped_tool_response(tmp_path):
    # MCP tool responses may arrive as content blocks with the JSON as
    # text — the run_id must still be found.
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))

    _posttool(state, proj, S1, tool="mcp__lockstep__scenario_start",
              tool_input={"recipe": "feature-dev"},
              tool_response={"content": [
                  {"type": "text", "text": json.dumps({"run_id": run_id, "step": "one"})}]})

    assert sessions.read_binding(state, run_id)["session_id"] == S1


def test_status_poll_refreshes_the_owner_binding(tmp_path):
    # A parent waiting out a long subcall only polls scenario_status — that
    # touch must keep its binding live, or a poller could be robbed mid-wait.
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))
    _bind(state, run_id, S1)
    _age_binding(state, run_id, 10.0)

    _posttool(state, proj, S1, tool_input={"run_id": run_id})

    seen = datetime.fromisoformat(sessions.read_binding(state, run_id)["last_seen"])
    assert datetime.now(timezone.utc) - seen < timedelta(minutes=1)


def test_touch_ignores_terminal_and_unknown_runs(tmp_path):
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))
    RunIndex(state).update(run_id, status="aborted")

    _posttool(state, proj, S1, tool_input={"run_id": run_id})
    _posttool(state, proj, S1, tool_input={"run_id": "no-such-run"})

    assert sessions.read_binding(state, run_id) is None
    assert sessions.read_binding(state, "no-such-run") is None


# ---------------------------------------------------------------------------
# the REAL platform payload — recorded live (Claude Code 2.1.220, 2026-08-07)
# from a plugin-manifest install, NOT invented. Two facts it pins: plugin
# MCP tools are named `mcp__plugin_<plugin>_<server>__<tool>` (the original
# `mcp__lockstep__.*`-only matcher never fired — bindings were never written
# and the gate locked out the very session that started the run), and
# `tool_response` arrives as a bare LIST of content blocks whose text is the
# JSON — not a dict, not `{"content": [...]}`.
# ---------------------------------------------------------------------------


def _recorded_payload() -> dict:
    p = Path(__file__).parent / "fixtures" / "hooks" / "posttool_scenario_start_plugin_install.json"
    return json.loads(p.read_text())


def _recorded_codex_payload() -> dict:
    p = Path(__file__).parent / "fixtures" / "hooks" / "posttool_scenario_start_codex.json"
    return json.loads(p.read_text())


def _force_run_id(state: Path, old: str, new: str) -> None:
    path = state / "runs.json"
    data = json.loads(path.read_text())
    rec = data.pop(old)
    rec["run_id"] = new
    data[new] = rec
    path.write_text(json.dumps(data))


def test_bind_from_recorded_plugin_install_payload(tmp_path):
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    payload = _recorded_payload()
    run_id = payload["tool_response"][0]["text"]
    run_id = json.loads(run_id)["run_id"]              # the hook must dig it out itself
    _force_run_id(state, _mk_run(state, str(proj.resolve())), run_id)

    cli.hook_posttool(payload, state)                  # byte-for-byte as the platform sent it

    binding = sessions.read_binding(state, run_id)
    assert binding is not None, "recorded payload did not bind — matcher or parser regressed"
    assert binding["session_id"] == payload["session_id"]


def test_bind_from_recorded_codex_payload(tmp_path):
    proj, state = _setup(tmp_path)
    payload = _recorded_codex_payload()
    run_id = cli._posttool_run_id(
        payload.get("tool_input"), payload.get("tool_response"), payload.get("tool_name", "")
    )
    assert run_id
    _force_run_id(state, _mk_run(state, str(proj.resolve())), run_id)

    cli.hook_posttool(payload, state)

    binding = sessions.read_binding(state, run_id)
    assert binding is not None
    assert binding["session_id"] == payload["session_id"]


def test_shipped_hook_matcher_covers_install_shapes():
    # The shipped hooks.json matcher must match the OBSERVED plugin-install
    # tool name, the `.mcp.json`-install name, AND a plugin installed under
    # ANY name (the plugin segment is the user's install name; the server
    # segment `lockstep` is pinned by the shipped manifest's mcpServers
    # key) — asserted mechanically against the file that ships, and against
    # cli.py's single-home copy of the same pattern.
    import re

    hooks = json.loads((Path(__file__).parents[2] / "hooks" / "hooks.json").read_text())
    (entry,) = hooks["hooks"]["PostToolUse"]
    matcher = entry["matcher"]
    assert matcher == cli.LOCKSTEP_TOOL_MATCHER    # ONE pattern, two homes, byte-equal
    assert re.fullmatch(matcher, _recorded_payload()["tool_name"])
    assert re.fullmatch(matcher, _recorded_codex_payload()["tool_name"])
    assert re.fullmatch(matcher, "mcp__lockstep__scenario_start")
    # the live-smoke hole: our plugin installed under a different name
    assert re.fullmatch(matcher, "mcp__plugin_myfork_lockstep__scenario_start")
    assert re.fullmatch(matcher, "mcp__plugin_team-tools_lockstep__scenario_status")
    # foreign tools stay outside: another plugin's own server...
    assert not re.fullmatch(matcher, "mcp__plugin_other_gitlab__create_issue")
    # ...a plugin whose NAME merely contains "lockstep" but whose server differs...
    assert not re.fullmatch(matcher, "mcp__plugin_my_lockstep_other__scenario_start")
    # ...and a bare server under another key (doctor's territory, via the marker)
    assert not re.fullmatch(matcher, "mcp__mystep__scenario_start")


def test_bind_from_plugin_install_under_any_plugin_name(tmp_path):
    # The recorded payload with ONLY the plugin install name changed — the
    # third shape the smoke report warned about. Binding must be
    # independent of what the user named the plugin.
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    payload = _recorded_payload()
    payload["tool_name"] = "mcp__plugin_myfork_lockstep__scenario_start"
    run_id = json.loads(payload["tool_response"][0]["text"])["run_id"]
    _force_run_id(state, _mk_run(state, str(proj.resolve())), run_id)

    cli.hook_posttool(payload, state)

    binding = sessions.read_binding(state, run_id)
    assert binding is not None, "renamed-plugin payload did not bind"
    assert binding["session_id"] == payload["session_id"]


def test_recorded_codex_payload_preserves_live_owner_and_stale_adoption(tmp_path):
    proj, state = _setup(tmp_path)
    payload = _recorded_codex_payload()
    run_id = cli._posttool_run_id(
        payload.get("tool_input"), payload.get("tool_response"), payload.get("tool_name", "")
    )
    assert run_id
    _force_run_id(state, _mk_run(state, str(proj.resolve())), run_id)
    cli.hook_posttool(payload, state)

    session_b = "codex-session-b"
    assert _pretool(state, proj, session_b)[1]
    status_payload = {
        **payload,
        "session_id": session_b,
        "tool_name": "mcp__lockstep__scenario_status",
        "tool_input": {"run_id": run_id},
        "tool_response": {"run_id": run_id, "status": "awaiting"},
    }
    cli.hook_posttool(status_payload, state)
    assert sessions.read_binding(state, run_id)["session_id"] == payload["session_id"]
    assert _pretool(state, proj, session_b)[1]

    _age_binding(state, run_id, 31.0)
    cli.hook_posttool(status_payload, state)
    assert sessions.read_binding(state, run_id)["session_id"] == session_b
    assert _pretool(state, proj, session_b) == (0, "")


# ---------------------------------------------------------------------------
# the name-agnostic door: an install under a fully custom server name only
# needs its shape added to the PLATFORM matcher — the hook then recognizes
# the lockstep response by the server-stamped marker, never by name. And a
# foreign tool's response must never pass for one: a bare `run_id` (e.g. a
# file-read surfacing runs.json) is NOT the marker.
# ---------------------------------------------------------------------------


def _marked(payload: dict) -> dict:
    from lockstep_mcp import sessions

    return {**payload, sessions.BINDING_MARKER_KEY: sessions.BINDING_MARKER_VALUE}


def test_custom_server_name_binds_via_response_marker(tmp_path):
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))

    _posttool(state, proj, S1, tool="mcp__mystep__scenario_start",
              tool_input={"recipe": "feature-dev"},
              tool_response=[{"type": "text",
                              "text": json.dumps(_marked({"run_id": run_id, "step": "one"}))}])

    assert sessions.read_binding(state, run_id)["session_id"] == S1


def test_foreign_tool_bare_run_id_never_binds(tmp_path):
    # A foreign mcp tool whose response happens to carry a live run_id —
    # the runs.json-through-a-file-read shape — must bind nothing.
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))

    _posttool(state, proj, S2, tool="mcp__filesystem__read_file",
              tool_input={"path": "state/runs.json"},
              tool_response={"run_id": run_id, "status": "awaiting"})
    _posttool(state, proj, S2, tool="mcp__filesystem__read_file",
              tool_input={"run_id": run_id})          # input run_id is no better

    assert sessions.read_binding(state, run_id) is None


def test_marker_must_sit_beside_run_id_in_one_object(tmp_path):
    # Scattered coincidence is not identity: the marker key in one object
    # and a run_id in another must not combine into a binding.
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))

    _posttool(state, proj, S2, tool="mcp__other__tool",
              tool_response=[{sessions.BINDING_MARKER_KEY: sessions.BINDING_MARKER_VALUE},
                             {"run_id": run_id}])

    assert sessions.read_binding(state, run_id) is None


def test_non_mcp_tools_never_bind(tmp_path):
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))

    _posttool(state, proj, S2, tool="Bash",
              tool_response=_marked({"run_id": run_id}))

    assert sessions.read_binding(state, run_id) is None


# ---------------------------------------------------------------------------
# adoption: the crash-recovery door — and its abuse boundary
# ---------------------------------------------------------------------------


def test_adoption_after_owner_crash(tmp_path):
    # Simulated crash: the owner stops firing hooks, so its binding goes
    # silent past the window. The resumed conversation (NEW session id)
    # touches the run with scenario_status — the PostToolUse hook adopts —
    # and only then does the gate open for it.
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))
    _bind(state, run_id, S1)
    _age_binding(state, run_id, 31.0)              # default window is 30m

    code, out = _pretool(state, proj, S2)          # the gate itself never adopts
    _denied(out)

    _posttool(state, proj, S2, tool_input={"run_id": run_id})

    binding = sessions.read_binding(state, run_id)
    assert binding["session_id"] == S2
    assert binding.get("adopted_from") == S1       # loud provenance, not a silent swap
    code, out = _pretool(state, proj, S2)
    assert code == 0 and out == ""


def test_touch_cannot_steal_a_live_run(tmp_path):
    # The abuse case: a second session touches a run whose owner is live.
    # The binding must not move, and the gate must stay shut for the toucher.
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))
    _bind(state, run_id, S1)

    _posttool(state, proj, S2, tool_input={"run_id": run_id})

    assert sessions.read_binding(state, run_id)["session_id"] == S1
    code, out = _pretool(state, proj, S2)
    _denied(out)
    code, out = _pretool(state, proj, S1)          # and the owner is unharmed
    assert out == ""


def test_adoption_window_configurable_via_env(tmp_path, monkeypatch):
    from lockstep_mcp import sessions

    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))
    _bind(state, run_id, S1)
    _age_binding(state, run_id, 10.0)

    _posttool(state, proj, S2, tool_input={"run_id": run_id})
    assert sessions.read_binding(state, run_id)["session_id"] == S1   # 10m < default 30m

    monkeypatch.setenv("LOCKSTEP_SESSION_STALE_MINUTES", "5")
    _posttool(state, proj, S2, tool_input={"run_id": run_id})
    assert sessions.read_binding(state, run_id)["session_id"] == S2   # 10m > 5m: adoptable


def test_owner_idleness_alone_never_unbinds(tmp_path):
    # An owner silent past the window with NO competing claim keeps its
    # run: staleness makes a run adoptABLE, it does not evict the owner.
    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))
    _bind(state, run_id, S1)
    _age_binding(state, run_id, 300.0)

    code, out = _pretool(state, proj, S1)

    assert code == 0 and out == ""


# ---------------------------------------------------------------------------
# the 24h expiry rule is deleted: RunRecord.updated does not tick during
# real work, so it measured session death where there was none
# ---------------------------------------------------------------------------


def test_owner_allowed_even_with_ancient_run_updated(tmp_path):
    # updated does not tick on Write/Edit or status polls — a >24h honest
    # work session must not be locked out of its own live run.
    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))
    _bind(state, run_id, S1)
    _set_updated(state, run_id,
                 (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())

    code, out = _pretool(state, proj, S1)

    assert code == 0 and out == ""


def test_child_chain_with_ancient_updated_still_unlocks(tmp_path, monkeypatch):
    # Same deletion on the child-ancestry predicate: a parent parked >24h
    # on a long-running child must not strand that child.
    proj, state = _setup(tmp_path)
    idx = RunIndex(state)
    parent = idx.create("feature-dev", str(proj.resolve()))
    child = idx.create("child-review", str(proj.resolve()), parent_run=parent.run_id, nonce="n")
    _set_updated(state, parent.run_id,
                 (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())
    monkeypatch.setenv("LOCKSTEP_CHILD_RUN", child.run_id)
    monkeypatch.setenv("LOCKSTEP_CHILD_NONCE", "n")

    code, out = _pretool(state, proj, S2)

    assert code == 0 and out == ""


# ---------------------------------------------------------------------------
# unaffected paths
# ---------------------------------------------------------------------------


def test_child_env_path_ignores_bindings(tmp_path, monkeypatch):
    # A spawned child session is credentialed by LOCKSTEP_CHILD_RUN + its
    # ancestry chain — a binding on the parent (even a foreign one) must
    # not close that path.
    proj, state = _setup(tmp_path)
    idx = RunIndex(state)
    parent = idx.create("feature-dev", str(proj.resolve()))
    child = idx.create("child-review", str(proj.resolve()), parent_run=parent.run_id, nonce="n")
    _bind(state, parent.run_id, S1)
    monkeypatch.setenv("LOCKSTEP_CHILD_RUN", child.run_id)
    monkeypatch.setenv("LOCKSTEP_CHILD_NONCE", "n")

    code, out = _pretool(state, proj, S2)

    assert code == 0 and out == ""

    idx.update(parent.run_id, status="escalated")  # dead chain still denies
    code, out = _pretool(state, proj, S2)
    _denied(out)


def test_no_policy_path_untouched_and_writes_no_sidecar(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    state = tmp_path / "state"
    _mk_run(state, str(proj.resolve()))

    code, out = _pretool(state, proj, S1)

    assert code == 0 and out == ""
    assert not (state / "bindings").exists()


def test_listing_other_sessions_runs_binds_nothing(tmp_path):
    # `list_runs` is introspection, not a touch: its response names every
    # active run in the project. Binding to the first id in it would make a
    # reader the owner of a stranger's run — and, past the silence window,
    # adopt it out from under its live driver.
    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))

    _posttool(state, proj, S2, tool="mcp__lockstep__list_runs",
              tool_input={"project": str(proj)},
              tool_response=[{"run_id": run_id, "status": "awaiting"}])

    assert sessions.read_binding(state, run_id) is None


def test_stop_does_not_block_a_session_that_does_not_drive_the_run(tmp_path):
    # Two sessions in one repo is routine. The run belongs to the one
    # driving it — telling the other to report or `scenario_abort` it hands
    # a stranger the verb that kills live work.
    proj, state = _setup(tmp_path)
    run_id = _mk_run(state, str(proj.resolve()))
    _bind(state, run_id, S1)

    code, out = cli.hook_stop({"session_id": S2}, state, str(proj))
    assert (code, out) == (0, "")

    code, out = cli.hook_stop({"session_id": S1}, state, str(proj))
    assert json.loads(out)["decision"] == "block"
