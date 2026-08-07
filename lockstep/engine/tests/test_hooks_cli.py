"""Task 7: hook handlers in cli.py — Stop / SessionStart / PreToolUse, plus
the `policy` and `doctor` CLI verbs and the heartbeat mechanism.

All path matching (decision 11 / review M8) is `Path.resolve()`
equality-or-parent-prefix: a run whose `project` is an ancestor of (or equal
to) the hook's `cwd` matches. Stop blocks with a `decision: block` JSON on
stdout naming the run_id, step, and all three exits. SessionStart returns
plain text (no block capability). PreToolUse (decision 15) is opt-in via
policy files and fails closed on any internal exception.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import lockstep_mcp.cli as cli
from lockstep_mcp.runs import RunIndex


def _mk_run(state_dir: Path, project: str, step: str = "one", recipe: str = "feature-dev") -> str:
    idx = RunIndex(state_dir)
    record = idx.create(recipe, project)
    idx.update(record.run_id, step=step, brief={"step": step, "task": "t", "exit_criterion": "x"})
    return record.run_id


def _set_updated(state_dir: Path, run_id: str, iso: str) -> None:
    path = state_dir / "runs.json"
    data = json.loads(path.read_text())
    data[run_id]["updated"] = iso
    path.write_text(json.dumps(data))


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


def test_session_start_flags_stale_runs(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    run_id = _mk_run(state_dir, str(proj.resolve()))
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    _set_updated(state_dir, run_id, stale_ts)

    text = cli.hook_session_start(state_dir, str(proj))

    assert "stale" in text


def test_session_start_ignores_fresh_runs(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _mk_run(state_dir, str(proj.resolve()))

    text = cli.hook_session_start(state_dir, str(proj))

    assert "stale" not in text


# ---------------------------------------------------------------------------
# PreToolUse gate — branch matrix
# ---------------------------------------------------------------------------


def test_pretool_no_policy_allows(tmp_path):
    exit_code, out = cli.hook_pretool({"cwd": str(tmp_path)}, tmp_path / "state")

    assert exit_code == 0
    assert out == ""


def test_pretool_matching_policy_and_run_allows(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _write_policy(state_dir, str(proj), "feature-dev")
    _mk_run(state_dir, str(proj.resolve()), recipe="feature-dev")

    exit_code, out = cli.hook_pretool({"cwd": str(proj)}, state_dir)

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


def test_pretool_without_child_env_keeps_v1_predicate(tmp_path, monkeypatch):
    # The worker session (no LOCKSTEP_CHILD_RUN) keeps v1 behaviour: an
    # awaiting run of the policy recipe unlocks the project; once it is
    # terminal, a still-awaiting descendant of another recipe does not.
    monkeypatch.delenv("LOCKSTEP_CHILD_RUN", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    state_dir = tmp_path / "state"
    _write_policy(state_dir, str(proj), "feature-dev")
    idx = RunIndex(state_dir)
    parent = idx.create("feature-dev", str(proj.resolve()))
    idx.create("child-review", str(proj.resolve()), parent_run=parent.run_id, nonce="n")

    exit_code, out = cli.hook_pretool({"cwd": str(proj)}, state_dir)
    assert exit_code == 0 and out == ""

    idx.update(parent.run_id, status="escalated")
    exit_code, out = cli.hook_pretool({"cwd": str(proj)}, state_dir)
    assert exit_code == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


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
    heartbeat = state_dir / "heartbeat.jsonl"
    heartbeat.write_text(
        json.dumps({"event": "SessionStart", "ts": datetime.now(timezone.utc).isoformat()}) + "\n"
    )

    ok, report = cli.doctor(state_dir, recipes_dir)

    assert ok is True
    assert "all green" in report
    assert "installed version:" in report


def test_doctor_flags_missing_dirs(tmp_path):
    ok, report = cli.doctor(tmp_path / "nope", tmp_path / "also-nope")

    assert ok is False
    assert "issues found" in report


def test_doctor_flags_stale_heartbeat_past_default_threshold(tmp_path):
    state_dir = tmp_path / "state"
    recipes_dir = tmp_path / "recipes"
    state_dir.mkdir()
    recipes_dir.mkdir()
    heartbeat = state_dir / "heartbeat.jsonl"
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    heartbeat.write_text(json.dumps({"event": "Stop", "ts": old_ts}) + "\n")

    ok, report = cli.doctor(state_dir, recipes_dir)

    assert ok is False
    assert "issues found" in report
    assert "stale" in report.lower()


def test_doctor_stale_threshold_configurable_via_env(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    recipes_dir = tmp_path / "recipes"
    state_dir.mkdir()
    recipes_dir.mkdir()
    heartbeat = state_dir / "heartbeat.jsonl"
    ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    heartbeat.write_text(json.dumps({"event": "Stop", "ts": ts}) + "\n")

    monkeypatch.setenv("LOCKSTEP_STALE_HOURS", "1")
    ok, report = cli.doctor(state_dir, recipes_dir)
    assert ok is False

    monkeypatch.setenv("LOCKSTEP_STALE_HOURS", "24")
    ok, report = cli.doctor(state_dir, recipes_dir)
    assert ok is True


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_write(tmp_path):
    state_dir = tmp_path / "state"

    cli._heartbeat(state_dir, "Stop")

    path = state_dir / "heartbeat.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "Stop"
    assert "ts" in entry


def test_heartbeat_rotation(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    path = state_dir / "heartbeat.jsonl"
    with open(path, "w") as f:
        for i in range(1005):
            f.write(json.dumps({"event": "x", "ts": str(i)}) + "\n")

    cli._heartbeat(state_dir, "Stop")

    lines = path.read_text().splitlines()
    assert len(lines) == 200
    assert json.loads(lines[-1])["event"] == "Stop"


def test_heartbeat_never_raises(tmp_path, monkeypatch):
    # a file where a directory is expected -> mkdir fails; must be swallowed.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    cli._heartbeat(blocker / "state", "Stop")  # must not raise


def test_hook_stop_writes_heartbeat_on_fast_path(tmp_path):
    state_dir = tmp_path / "state"

    exit_code, out = cli.hook_stop({"stop_hook_active": False}, state_dir, str(tmp_path))

    assert exit_code == 0 and out == ""
    heartbeat = state_dir / "heartbeat.jsonl"
    assert heartbeat.exists()
    entry = json.loads(heartbeat.read_text().splitlines()[0])
    assert entry["event"] == "Stop"


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
