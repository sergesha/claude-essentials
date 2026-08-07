"""Task 7: hook handlers in cli.py — Stop / SessionStart / PreToolUse, plus
the `policy` and `doctor` CLI verbs.

All path matching (decision 11 / review M8) is `Path.resolve()`
equality-or-parent-prefix: a run whose `project` is an ancestor of (or equal
to) the hook's `cwd` matches. Stop blocks with a `decision: block` JSON on
stdout naming the run_id, step, and all three exits. SessionStart returns
plain text (no block capability). PreToolUse (decision 15) is opt-in via
policy files and fails closed on any internal exception; its worker
predicate is SESSION BINDING — the deep matrix for that (ownership,
adoption, theft-resistance, PostToolUse) lives in
`tests/test_session_binding.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

import lockstep_mcp.cli as cli
from lockstep_mcp import sessions
from lockstep_mcp.engine import Engine
from lockstep_mcp.runs import RunIndex

GOOD_RECIPES = Path(__file__).parent / "fixtures" / "recipes" / "good"
SESSION = "session-under-test"


def _mk_run(state_dir: Path, project: str, step: str = "one", recipe: str = "feature-dev") -> str:
    idx = RunIndex(state_dir)
    record = idx.create(recipe, project)
    idx.update(record.run_id, step=step, brief={"step": step, "task": "t", "exit_criterion": "x"})
    return record.run_id


def _write_policy(state_dir: Path, project: str, recipe: str) -> Path:
    policy_dir = state_dir / "policy.d"
    policy_dir.mkdir(parents=True, exist_ok=True)
    slug = cli._policy_slug(project)
    path = policy_dir / f"{slug}.yaml"
    path.write_text(yaml.safe_dump({"project": str(Path(project).resolve()), "recipe": recipe}))
    return path


# ---------------------------------------------------------------------------
# base four
# ---------------------------------------------------------------------------


def test_stop_blocks_on_active_run(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    state_dir = tmp_path / "state"
    run_id = _mk_run(state_dir, str(project.resolve()))

    exit_code, out = cli.hook_stop({"stop_hook_active": False}, state_dir, str(project))

    assert exit_code == 0
    data = json.loads(out)
    assert data["decision"] == "block"
    assert run_id in data["reason"]
    assert "one" in data["reason"]
    assert "scenario_done" in data["reason"]
    assert "scenario_escalate" in data["reason"]
    assert "scenario_abort" in data["reason"]


def test_stop_allows_when_hook_active(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    state_dir = tmp_path / "state"
    _mk_run(state_dir, str(project.resolve()))

    exit_code, out = cli.hook_stop({"stop_hook_active": True}, state_dir, str(project))

    assert exit_code == 0
    assert out == ""


def test_stop_allows_no_runs(tmp_path):
    exit_code, out = cli.hook_stop({"stop_hook_active": False}, tmp_path / "state", str(tmp_path))

    assert exit_code == 0
    assert out == ""


def test_session_start_lists_active_runs(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    state_dir = tmp_path / "state"
    run_id = _mk_run(state_dir, str(project.resolve()))

    text = cli.hook_session_start(state_dir, str(project))

    assert run_id in text
    assert "one" in text
    assert "scenario_status" in text


# ---------------------------------------------------------------------------
# path normalization + staleness
# ---------------------------------------------------------------------------


def test_stop_ignores_other_projects(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _mk_run(state_dir, str(other.resolve()))

    exit_code, out = cli.hook_stop({"stop_hook_active": False}, state_dir, str(proj))

    assert exit_code == 0
    assert out == ""


def test_stop_matches_subdirectory_cwd(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    sub = proj / "sub"
    sub.mkdir()
    state_dir = tmp_path / "state"
    run_id = _mk_run(state_dir, str(proj.resolve()))

    exit_code, out = cli.hook_stop({"stop_hook_active": False}, state_dir, str(sub))

    assert exit_code == 0
    data = json.loads(out)
    assert data["decision"] == "block"
    assert run_id in data["reason"]


def test_session_start_flags_runs_with_no_live_driver(tmp_path):
    # The liveness signal is the binding sidecar, never RunRecord.updated
    # (which does not tick during real work). An unbound run — typically
    # the aftermath of a crash — is flagged with its adoption door.
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _mk_run(state_dir, str(proj.resolve()))

    text = cli.hook_session_start(state_dir, str(proj))

    assert "no live driving session" in text
    assert "scenario_status" in text


def test_session_start_does_not_flag_a_driven_run(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    run_id = _mk_run(state_dir, str(proj.resolve()))
    sessions.touch(state_dir, run_id, SESSION, 30.0)

    text = cli.hook_session_start(state_dir, str(proj))

    assert "no live driving session" not in text


# ---------------------------------------------------------------------------
# PreToolUse gate — branch matrix
# ---------------------------------------------------------------------------


def test_pretool_no_policy_allows(tmp_path):
    exit_code, out = cli.hook_pretool({"cwd": str(tmp_path)}, tmp_path / "state")

    assert exit_code == 0
    assert out == ""


def test_pretool_matching_policy_and_run_allows(tmp_path):
    # The happy path: a run of the policy recipe, bound to the session
    # asking (normally by hook_posttool at scenario_start), unlocks it.
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _write_policy(state_dir, str(proj), "feature-dev")
    run_id = _mk_run(state_dir, str(proj.resolve()), recipe="feature-dev")
    sessions.touch(state_dir, run_id, SESSION, 30.0)

    exit_code, out = cli.hook_pretool({"cwd": str(proj), "session_id": SESSION}, state_dir)

    assert exit_code == 0
    assert out == ""


def test_pretool_policy_no_run_denies(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _write_policy(state_dir, str(proj), "feature-dev")

    exit_code, out = cli.hook_pretool({"cwd": str(proj)}, state_dir)

    assert exit_code == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "feature-dev" in data["hookSpecificOutput"]["permissionDecisionReason"]
    # M4: mirrors the SessionStart wrapper's convention of naming the
    # hook event inside hookSpecificOutput.
    assert data["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_pretool_recipe_mismatch_stays_denied(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _write_policy(state_dir, str(proj), "feature-dev")
    _mk_run(state_dir, str(proj.resolve()), recipe="other-recipe")

    exit_code, out = cli.hook_pretool({"cwd": str(proj)}, state_dir)

    assert exit_code == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_cross_project_run_stays_denied(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    state_dir = tmp_path / "state"
    _write_policy(state_dir, str(proj), "feature-dev")
    _mk_run(state_dir, str(other.resolve()), recipe="feature-dev")

    exit_code, out = cli.hook_pretool({"cwd": str(proj)}, state_dir)

    assert exit_code == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_child_session_unlock_narrowed_to_its_own_chain(tmp_path, monkeypatch):
    # I2: a spawned child session (LOCKSTEP_CHILD_RUN in its env) is
    # unlocked ONLY while its own ancestry chain terminates in an AWAITING
    # run of the policy recipe. The v1 predicate unlocked the whole project
    # for anyone whenever ANY awaiting policy run existed — so `other`
    # below would keep this child unlocked after its own ancestor died.
    # This test FAILS against the v1 hook code (which ignores the env).
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _write_policy(state_dir, str(proj), "feature-dev")
    idx = RunIndex(state_dir)
    parent = idx.create("feature-dev", str(proj.resolve()))
    child = idx.create("child-review", str(proj.resolve()), parent_run=parent.run_id, nonce="n")
    idx.create("feature-dev", str(proj.resolve()))         # unrelated awaiting policy run
    monkeypatch.setenv("LOCKSTEP_CHILD_RUN", child.run_id)

    exit_code, out = cli.hook_pretool({"cwd": str(proj)}, state_dir)
    assert exit_code == 0 and out == ""        # own chain awaiting: unlocked

    idx.update(parent.run_id, status="escalated")
    exit_code, out = cli.hook_pretool({"cwd": str(proj)}, state_dir)
    assert exit_code == 0
    data = json.loads(out)
    # denied although the unrelated awaiting policy run still exists —
    # the child's own dead chain decides, not the project.
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_child_env_with_unknown_run_fails_closed(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _write_policy(state_dir, str(proj), "feature-dev")
    idx = RunIndex(state_dir)
    idx.create("feature-dev", str(proj.resolve()))         # would unlock a plain worker
    monkeypatch.setenv("LOCKSTEP_CHILD_RUN", "no-such-run")

    exit_code, out = cli.hook_pretool({"cwd": str(proj)}, state_dir)
    assert exit_code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_worker_predicate_is_session_binding(tmp_path, monkeypatch):
    # The worker session (no LOCKSTEP_CHILD_RUN) is unlocked by owning a
    # run of the POLICY recipe: a bound run of another recipe (the child
    # below) never satisfies the gate, and once the policy run is terminal
    # the project locks again.
    monkeypatch.delenv("LOCKSTEP_CHILD_RUN", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _write_policy(state_dir, str(proj), "feature-dev")
    idx = RunIndex(state_dir)
    parent = idx.create("feature-dev", str(proj.resolve()))
    child = idx.create("child-review", str(proj.resolve()), parent_run=parent.run_id, nonce="n")
    sessions.touch(state_dir, parent.run_id, SESSION, 30.0)
    sessions.touch(state_dir, child.run_id, SESSION, 30.0)

    exit_code, out = cli.hook_pretool({"cwd": str(proj), "session_id": SESSION}, state_dir)
    assert exit_code == 0 and out == ""

    idx.update(parent.run_id, status="escalated")
    exit_code, out = cli.hook_pretool({"cwd": str(proj), "session_id": SESSION}, state_dir)
    assert exit_code == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_deny_advice_works_end_to_end(tmp_path, monkeypatch):
    # The deny message's own advice must actually open the gate. A session
    # facing a run with no live driver is told scenario_status (adoption) —
    # simulate the full round: MCP call + its PostToolUse fire — and also
    # the abort + fresh-start road. Driven through the real engine.
    monkeypatch.delenv("LOCKSTEP_CHILD_RUN", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _write_policy(state_dir, str(proj), "minimal")
    eng = Engine(state_dir, GOOD_RECIPES)
    run_id = eng.start("minimal", {}, str(proj.resolve()))["run_id"]  # crashed owner: no binding

    exit_code, out = cli.hook_pretool({"cwd": str(proj), "session_id": SESSION}, state_dir)
    assert exit_code == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = data["hookSpecificOutput"]["permissionDecisionReason"]
    assert "scenario_status" in reason and run_id in reason

    status = eng.status(run_id)                          # advice road 1: touch the run…
    cli.hook_posttool({"cwd": str(proj), "session_id": SESSION,   # …and the hook that fire brings
                       "tool_name": "mcp__lockstep__scenario_status",
                       "tool_input": {"run_id": run_id}, "tool_response": status},
                      state_dir)
    exit_code, out = cli.hook_pretool({"cwd": str(proj), "session_id": SESSION}, state_dir)
    assert exit_code == 0 and out == ""                  # adopted: gate open

    eng.abort(run_id)                                    # advice road 2: abort…
    out2 = eng.start("minimal", {}, str(proj.resolve()))  # …fresh start…
    cli.hook_posttool({"cwd": str(proj), "session_id": SESSION,
                       "tool_name": "mcp__lockstep__scenario_start",
                       "tool_input": {"recipe": "minimal"}, "tool_response": out2},
                      state_dir)                         # …bound at birth
    exit_code, out = cli.hook_pretool({"cwd": str(proj), "session_id": SESSION}, state_dir)
    assert exit_code == 0 and out == ""


def test_pretool_engine_calls_alone_never_open_the_gate(tmp_path, monkeypatch):
    # Anti-fix pin: the ENGINE never writes bindings — a bare status call
    # (e.g. _nudge_ancestors' internal poll, or an MCP status whose
    # PostToolUse hook never fired) must not bind the run to anyone or
    # open the gate for a non-owner. Only the hook-mediated touch does.
    monkeypatch.delenv("LOCKSTEP_CHILD_RUN", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _write_policy(state_dir, str(proj), "minimal")
    eng = Engine(state_dir, GOOD_RECIPES)
    run_id = eng.start("minimal", {}, str(proj.resolve()))["run_id"]

    eng.status(run_id)                                   # the tempting "recovery" step

    assert sessions.read_binding(state_dir, run_id) is None
    exit_code, out = cli.hook_pretool({"cwd": str(proj), "session_id": SESSION}, state_dir)
    assert exit_code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_exception_denies(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _write_policy(state_dir, str(proj), "feature-dev")

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(cli, "RunIndex", _Boom)

    exit_code, out = cli.hook_pretool({"cwd": str(proj)}, state_dir)

    assert exit_code == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# policy CLI round-trip
# ---------------------------------------------------------------------------


def test_policy_require_then_clear_roundtrip(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state_dir))

    assert cli.main(["policy", "require", "--project", str(proj), "--recipe", "feature-dev"]) == 0
    slug = cli._policy_slug(str(proj))
    policy_file = state_dir / "policy.d" / f"{slug}.yaml"
    assert policy_file.exists()
    doc = yaml.safe_load(policy_file.read_text())
    assert doc["recipe"] == "feature-dev"
    assert doc["project"] == str(proj.resolve())

    assert cli.main(["policy", "clear", "--project", str(proj)]) == 0
    assert not policy_file.exists()


def test_policy_bare_verb_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "state"))
    assert cli.main(["policy"]) == 0
    assert not (tmp_path / "state").exists()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_all_green(tmp_path):
    state_dir = tmp_path / "state"
    recipes_dir = tmp_path / "recipes"
    state_dir.mkdir()
    recipes_dir.mkdir()

    ok, report = cli.doctor(state_dir, recipes_dir)

    assert ok is True
    assert "all green" in report
    assert "installed version:" in report


def test_doctor_flags_missing_dirs(tmp_path):
    ok, report = cli.doctor(tmp_path / "nope", tmp_path / "also-nope")

    assert ok is False
    assert "issues found" in report


def test_doctor_screams_on_active_run_without_binding(tmp_path):
    # THE silent-lockout detector: an active run whose binding sidecar was
    # never written means the PostToolUse hook never fired for it — the
    # installed matcher does not match this installation's tool names.
    # Doctor must fail loudly and hand the user the exact matcher to fix.
    state_dir = tmp_path / "state"
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    run_id = _mk_run(state_dir, str(proj.resolve()))

    ok, report = cli.doctor(state_dir, recipes_dir)

    assert ok is False
    assert run_id in report
    assert "PostToolUse" in report
    assert cli.LOCKSTEP_TOOL_MATCHER in report         # the exact matcher, verbatim
    assert "scenario_status" in report                 # ...and the recovery touch


def test_doctor_green_when_active_run_is_bound(tmp_path):
    state_dir = tmp_path / "state"
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    run_id = _mk_run(state_dir, str(proj.resolve()))
    assert sessions.touch(state_dir, run_id, SESSION, 30.0) == "bound"

    ok, report = cli.doctor(state_dir, recipes_dir)

    assert ok is True
    assert run_id in report and SESSION in report


def test_doctor_ignores_terminal_runs_without_bindings(tmp_path):
    # Only ACTIVE runs prove the hook should have fired; terminal runs
    # (their sidecars GC'd or never made) are not a finding.
    state_dir = tmp_path / "state"
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    run_id = _mk_run(state_dir, str(proj.resolve()))
    RunIndex(state_dir).update(run_id, status="aborted")

    ok, report = cli.doctor(state_dir, recipes_dir)

    assert ok is True


def test_hooks_write_nothing_to_the_state_dir(tmp_path):
    # Hooks are read-only on ENGINE-owned state by contract; their one own
    # write is the bindings/ sidecar tree, and even that only for a live
    # policy-relevant run. A hook fire against an empty state dir — the
    # posttool observer included — leaves it absent.
    state_dir = tmp_path / "state"

    cli.hook_stop({"stop_hook_active": False}, state_dir, str(tmp_path))
    cli.hook_session_start(state_dir, str(tmp_path))
    cli.hook_pretool({"cwd": str(tmp_path)}, state_dir)
    cli.hook_posttool({"cwd": str(tmp_path), "session_id": SESSION,
                       "tool_name": "mcp__lockstep__scenario_status",
                       "tool_input": {"run_id": "no-such-run"}, "tool_response": {}},
                      state_dir)

    assert not state_dir.exists()


# ---------------------------------------------------------------------------
# Task 8: subcall-aware hook surfaces
# ---------------------------------------------------------------------------


def test_stop_text_is_subcall_aware_per_run(tmp_path):
    idx = RunIndex(tmp_path)
    parked = idx.create("rec", "/proj")
    idx.update(parked.run_id, step="_subcall",
               brief={"step": "_subcall", "node": "review", "runner": "claude"})
    working = idx.create("rec2", "/proj")
    idx.update(working.run_id, step="one", brief={"step": "one"})
    code, out = cli.hook_stop({"stop_hook_active": False, "cwd": "/proj"}, tmp_path, cwd="/proj")
    payload = json.loads(out)["reason"]
    # m8.5: the parked run's line must NOT say scenario_done; the working
    # run's line still must — per-run rendering, not one joined sentence.
    parked_line = next(l for l in payload.split("lockstep:") if parked.run_id in l)
    working_line = next(l for l in payload.split("lockstep:") if working.run_id in l)
    assert "subcall in progress" in parked_line and "scenario_done" not in parked_line
    assert "scenario_done" in working_line


def test_session_start_marks_the_sessions_own_child_run(tmp_path, monkeypatch):
    # C3: a spawned child session inherits LOCKSTEP_CHILD_RUN; the listing
    # must single out that run as the session's own, not leave the child to
    # guess between its parent's line and its own.
    idx = RunIndex(tmp_path)
    parent = idx.create("feature-dev", "/proj")
    idx.update(parent.run_id, step="_subcall",
               brief={"step": "_subcall", "node": "review", "runner": "claude"})
    child = idx.create("child-review", "/proj", parent_run=parent.run_id, nonce="n")
    idx.update(child.run_id, step="review", brief={"step": "review"})
    monkeypatch.setenv("LOCKSTEP_CHILD_RUN", child.run_id)
    ctx = cli.hook_session_start(tmp_path, cwd="/proj")
    child_line = next(l for l in ctx.splitlines() if child.run_id in l)
    parent_line = next(l for l in ctx.splitlines() if parent.run_id in l)
    assert "OWN child run" in child_line
    assert "OWN child run" not in parent_line


def test_session_start_names_the_subcall_not_the_raw_marker(tmp_path):
    idx = RunIndex(tmp_path)
    r = idx.create("rec", "/proj")
    idx.update(r.run_id, step="_subcall",
               brief={"step": "_subcall", "node": "review", "runner": "claude"})
    ctx = cli.hook_session_start(tmp_path, cwd="/proj")
    # v1 renders the step repr-quoted: awaiting step '_subcall' — that
    # exact token must be gone (I8.1: the old replace()-based assertion was
    # tautological and passed against unmodified v1 text).
    assert "subcall in progress" in ctx and "'_subcall'" not in ctx
