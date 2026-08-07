import json
import os
import sys
import time
from pathlib import Path

import pytest

from lockstep_mcp import subcalls
from lockstep_mcp.runners import RunnerError, RunnerSpec

FAKE = Path(__file__).parent / "fixtures" / "fake_runner.py"


def _argv(*extra):
    return [sys.executable, str(FAKE), *extra]


def _wait_terminal(wd, tries=140):
    res = subcalls.probe(wd)
    for _ in range(tries):
        if res["status"] != "running":
            break
        time.sleep(0.05)
        res = subcalls.probe(wd)
    return res


def _ctx(tmp_path, wd, *extra):
    # The REAL graph-delivered shape: resume payloads land ONLY inside the
    # `evidence` channel — the hooks never read top-level ctx or `brief`.
    return {
        "evidence": {
            "_subcall_workdir": str(wd),
            "_subcall_argv": _argv(*extra),
            "_subcall_cwd": str(tmp_path),
            "_subcall_env": dict(os.environ),
            "_subcall_timeout_minutes": 5,
            "_subcall_node": "review",
            "_subcall_runner": "claude",
        },
    }


# --- process layer -----------------------------------------------------------

def test_start_and_probe_completion(tmp_path):
    wd = tmp_path / "wd"
    subcalls.start_process(_argv("--mode", "ok"), cwd=str(tmp_path), env=None, workdir=wd, timeout_minutes=5)
    res = _wait_terminal(wd)
    assert res["status"] == "done" and res["exit_code"] == 0
    assert "fake-session-1" in res["output"]


def test_probe_reports_running_then_reattaches_after_restart(tmp_path):
    wd = tmp_path / "wd"
    subcalls.start_process(_argv("--mode", "ok", "--sleep", "1.0"), cwd=str(tmp_path), env=None,
                           workdir=wd, timeout_minutes=5)
    assert subcalls.probe(wd)["status"] == "running"
    proc = json.loads((wd / "proc.json").read_text())
    assert proc["pid"] > 0 and proc["started"] > 0          # start-time recorded
    # genuine "restart": drop every in-process handle; the verdict must come
    # from the state-dir files alone (the brief's version skipped this drop
    # and so never tested a restart at all).
    subcalls._HANDLES.clear()
    res = _wait_terminal(wd)
    assert res["status"] == "done" and res["exit_code"] == 0
    assert "fake-session-1" in res["output"]


def test_verdict_survives_restart_after_child_finished(tmp_path):
    # the anti-"finished process reported running" case from requirement (a)
    wd = tmp_path / "wd"
    subcalls.start_process(_argv("--mode", "ok"), cwd=str(tmp_path), env=None, workdir=wd, timeout_minutes=5)
    res = _wait_terminal(wd)
    assert res["status"] == "done"
    subcalls._HANDLES.clear()
    again = subcalls.probe(wd)
    assert again["status"] == "done" and again["exit_code"] == 0


def test_nonzero_exit_is_error_with_stderr(tmp_path):
    wd = tmp_path / "wd"
    subcalls.start_process(_argv("--mode", "fail"), cwd=str(tmp_path), env=None, workdir=wd, timeout_minutes=5)
    res = _wait_terminal(wd)
    assert res["status"] == "error" and res["exit_code"] == 3 and "boom" in res["stderr"]


def test_timeout_terminates_and_reports(tmp_path):
    wd = tmp_path / "wd"
    subcalls.start_process(_argv("--mode", "ok", "--sleep", "30"), cwd=str(tmp_path), env=None,
                           workdir=wd, timeout_minutes=0)      # already past deadline on first probe
    res = subcalls.probe(wd)
    assert res["status"] == "timeout"
    assert subcalls.probe(wd)["status"] == "timeout"           # stable, verdict is terminal


def test_timeout_resolves_without_handle(tmp_path):
    # requirement (b): the timeout must fire even in a process that never
    # owned the handle (restarted server).
    wd = tmp_path / "wd"
    subcalls.start_process(_argv("--mode", "ok", "--sleep", "30"), cwd=str(tmp_path), env=None,
                           workdir=wd, timeout_minutes=0)
    subcalls._HANDLES.clear()
    assert subcalls.probe(wd)["status"] == "timeout"
    assert subcalls.probe(wd)["status"] == "timeout"


def test_terminate_cancels_and_is_stable(tmp_path):
    wd = tmp_path / "wd"
    subcalls.start_process(_argv("--mode", "ok", "--sleep", "30"), cwd=str(tmp_path), env=None,
                           workdir=wd, timeout_minutes=5)
    subcalls.terminate(wd)
    res = subcalls.probe(wd)
    assert res["status"] == "error"
    assert "cancel" in " ".join(res["reasons"]).lower()
    assert subcalls.probe(wd)["status"] == "error"


def test_double_start_is_refused(tmp_path):
    wd = tmp_path / "wd"
    subcalls.start_process(_argv("--mode", "ok"), cwd=str(tmp_path), env=None, workdir=wd, timeout_minutes=5)
    with pytest.raises(RunnerError):
        subcalls.start_process(_argv("--mode", "ok"), cwd=str(tmp_path), env=None, workdir=wd, timeout_minutes=5)


def test_start_refuses_unverified_argv0(tmp_path):
    wd = tmp_path / "wd"
    with pytest.raises(RunnerError):        # relative: would require PATH resolution
        subcalls.start_process(["python3"], cwd=str(tmp_path), env=None, workdir=wd, timeout_minutes=5)
    with pytest.raises(RunnerError):        # absolute but absent
        subcalls.start_process([str(tmp_path / "no-such-bin")], cwd=str(tmp_path), env=None,
                               workdir=wd, timeout_minutes=5)
    assert not (wd / "proc.json").exists()  # nothing spawned, workdir reusable
    subcalls.start_process(_argv("--mode", "ok"), cwd=str(tmp_path), env=None, workdir=wd, timeout_minutes=5)
    assert _wait_terminal(wd)["status"] == "done"


def test_probe_without_record_is_error(tmp_path):
    res = subcalls.probe(tmp_path / "never-started")
    assert res["status"] == "error" and res["exit_code"] is None


# --- graph hooks -------------------------------------------------------------

def test_spawn_hook_writes_running_envelope(tmp_path):
    out = subcalls.spawn(_ctx(tmp_path, tmp_path / "wd"))
    assert out["_subcall_status"] == "running"
    assert out["_subcall_envelope"]["node"] == "review"


def test_poll_hook_transitions_to_done_with_envelope(tmp_path):
    state = _ctx(tmp_path, tmp_path / "wd")
    subcalls.spawn(state)
    for _ in range(140):
        out = subcalls.poll(state)
        if out["_subcall_status"] != "running":
            break
        time.sleep(0.05)
    env = out["_subcall_envelope"]
    assert out["_subcall_status"] == "done"
    assert env["exit_code"] == 0 and env["session_id"] == "fake-session-1"
    assert env["artifact_hashes"] == {}                 # stays {} for one-shot: the envelope itself
    #                                                     (output/exit_code/session_id) is the
    #                                                     validated artifact; hashes are fractal-only


def test_spawn_error_when_workdir_ctx_missing(tmp_path):
    out = subcalls.spawn({"evidence": {}})
    assert out["_subcall_status"] == "error"
    assert "ctx" in " ".join(out["_subcall_envelope"]["reasons"]).lower()


@pytest.mark.parametrize("missing", ["_subcall_workdir", "_subcall_argv", "_subcall_cwd",
                                     "_subcall_env", "_subcall_timeout_minutes"])
def test_spawn_requires_full_ctx_no_silent_fallbacks(tmp_path, missing):
    state = _ctx(tmp_path, tmp_path / "wd")
    del state["evidence"][missing]
    out = subcalls.spawn(state)
    assert out["_subcall_status"] == "error"
    assert "ctx" in " ".join(out["_subcall_envelope"]["reasons"]).lower()


def test_spawn_hook_refuses_env_none(tmp_path):
    # env=None would make Popen inherit the engine's FULL environment,
    # bypassing the child_env allowlist — fail closed.
    state = _ctx(tmp_path, tmp_path / "wd")
    state["evidence"]["_subcall_env"] = None
    out = subcalls.spawn(state)
    assert out["_subcall_status"] == "error"


def test_spawn_hook_reattaches_instead_of_double_spawning(tmp_path):
    state = _ctx(tmp_path, tmp_path / "wd", "--sleep", "1.0")
    assert subcalls.spawn(state)["_subcall_status"] == "running"
    pid1 = json.loads((tmp_path / "wd" / "proc.json").read_text())["pid"]
    assert subcalls.spawn(state)["_subcall_status"] == "running"   # idempotent
    assert json.loads((tmp_path / "wd" / "proc.json").read_text())["pid"] == pid1


def test_spawn_hook_reports_error_on_unverified_binary(tmp_path):
    state = _ctx(tmp_path, tmp_path / "wd")
    state["evidence"]["_subcall_argv"] = [str(tmp_path / "planted-runner")]
    out = subcalls.spawn(state)   # RunnerError must become an error envelope, not escape
    assert out["_subcall_status"] == "error"
    assert out["_subcall_envelope"]["reasons"]


def test_poll_hook_maps_timeout_to_error_status(tmp_path):
    state = _ctx(tmp_path, tmp_path / "wd", "--sleep", "30")
    state["evidence"]["_subcall_timeout_minutes"] = 0
    subcalls.spawn(state)
    out = subcalls.poll(state)
    assert out["_subcall_status"] == "error"
    assert "timeout" in " ".join(out["_subcall_envelope"]["reasons"]).lower()


def test_poll_preserves_spawn_failure_reason_over_probe_miss(tmp_path):
    # A spawn that failed before writing proc.json leaves its real cause in
    # the threaded top-level `_subcall_envelope`; the first poll tick's
    # generic "no subcall process record" must not overwrite it.
    state = _ctx(tmp_path, tmp_path / "wd")
    state["evidence"]["_subcall_argv"] = [str(tmp_path / "planted-runner")]
    out = subcalls.spawn(state)
    assert out["_subcall_status"] == "error"
    state["_subcall_envelope"] = out["_subcall_envelope"]  # threads as a top-level channel
    res = subcalls.poll(state)
    assert res["_subcall_status"] == "error"
    assert any("spawn failed" in r for r in res["_subcall_envelope"]["reasons"])


# --- fractal poll units: hand-written child index + baseline, no live child
# --- ------------------------------------------------------------------

def _fractal_src(tmp_path, wd, child_run="child-review-aaaa1111"):
    state_dir = tmp_path / "state"
    (state_dir / "runs").mkdir(parents=True, exist_ok=True)
    return {"_subcall_workdir": str(wd), "_subcall_node": "review",
            "_subcall_runner": "claude", "_subcall_child_run": child_run,
            "_subcall_state_dir": str(state_dir),
            "_subcall_artifacts": {"review": ".lockstep/review.md"}}, state_dir


def _seed_child(state_dir, child_run, status):
    rec = {"run_id": child_run, "recipe": "child-review", "project": "/proj",
           "status": status, "step": None, "brief": None,
           "started": "2026-08-07T00:00:00+00:00", "updated": "2026-08-07T00:00:00+00:00",
           "parent_run": "parent-1", "nonce": "n"}
    (state_dir / "runs.json").write_text(json.dumps({child_run: rec}))


def _seed_child_baseline(state_dir, child_run, manifest):
    (state_dir / "runs" / f"{child_run}.baseline_index").write_text("1")
    (state_dir / "runs" / f"{child_run}.baseline.1.json").write_text(json.dumps(manifest))


def test_fractal_poll_running_while_child_awaiting_and_process_alive(tmp_path):
    src, state_dir = _fractal_src(tmp_path, tmp_path / "wd")
    _seed_child(state_dir, src["_subcall_child_run"], "awaiting")
    subcalls.start_process(_argv("--sleep", "30"), cwd=str(tmp_path),
                           env=dict(os.environ), workdir=tmp_path / "wd", timeout_minutes=5)
    out = subcalls.poll({"evidence": src})
    assert out["_subcall_status"] == "running"


def test_fractal_poll_done_collects_child_baseline_hashes_and_is_stable(tmp_path):
    src, state_dir = _fractal_src(tmp_path, tmp_path / "wd")
    _seed_child(state_dir, src["_subcall_child_run"], "done")
    digest = "f" * 64
    _seed_child_baseline(state_dir, src["_subcall_child_run"], {".lockstep/review.md": digest})
    out = subcalls.poll({"evidence": src})
    env = out["_subcall_envelope"]
    assert out["_subcall_status"] == "done"
    assert env["child_status"] == "done" and env["artifact_hashes"] == {"review": digest}
    # stable: child terminal decides BEFORE any probe — terminate()'s
    # cancelled verdict must never flip a done envelope on the next poll
    again = subcalls.poll({"evidence": src})
    assert again["_subcall_status"] == "done"
    assert again["_subcall_envelope"]["artifact_hashes"] == {"review": digest}


def test_fractal_poll_errors_when_artifact_missing_from_child_baseline(tmp_path):
    src, state_dir = _fractal_src(tmp_path, tmp_path / "wd")
    _seed_child(state_dir, src["_subcall_child_run"], "done")
    _seed_child_baseline(state_dir, src["_subcall_child_run"], {})   # nothing pinned
    out = subcalls.poll({"evidence": src})
    assert out["_subcall_status"] == "error"
    assert any("review" in r for r in out["_subcall_envelope"]["reasons"])


def test_fractal_poll_errors_on_escalated_child(tmp_path):
    src, state_dir = _fractal_src(tmp_path, tmp_path / "wd")
    _seed_child(state_dir, src["_subcall_child_run"], "escalated")
    out = subcalls.poll({"evidence": src})
    assert out["_subcall_status"] == "error"
    assert out["_subcall_envelope"]["child_status"] == "escalated"


def test_fractal_poll_errors_when_process_died_but_child_awaiting(tmp_path):
    src, state_dir = _fractal_src(tmp_path, tmp_path / "wd")
    _seed_child(state_dir, src["_subcall_child_run"], "awaiting")
    subcalls.start_process(_argv("--mode", "ok"), cwd=str(tmp_path),
                           env=dict(os.environ), workdir=tmp_path / "wd", timeout_minutes=5)
    _wait_terminal(tmp_path / "wd")
    out = subcalls.poll({"evidence": src})
    assert out["_subcall_status"] == "error"
    assert any(src["_subcall_child_run"] in r for r in out["_subcall_envelope"]["reasons"])


# --- resume_session shape gate (obligation 2) --------------------------------

def _spec():
    return RunnerSpec(name="claude", path=sys.executable, models=["claude-sonnet-4-5"],
                      timeout_minutes=5, max_subcalls_per_run=8, max_fractal_depth=2)


def test_safe_argv_accepts_sane_resume_and_keeps_prompt_terminated():
    argv = subcalls.safe_argv(_spec(), "--hostile prompt", None, "sess.1-A_b")
    i = argv.index("--resume")
    assert argv[i + 1] == "sess.1-A_b"
    assert argv[-2:] == ["--", "--hostile prompt"]      # prompt stays behind the terminator


@pytest.mark.parametrize("hostile", [
    "--dangerously-skip-permissions",   # flag injection into the pre-terminator slot
    "-r",
    "",
    ".hidden",                          # must start alphanumeric
    "a b",
    "a" * 129,
    "x;rm -rf /",
])
def test_safe_argv_rejects_hostile_resume(hostile):
    with pytest.raises(RunnerError):
        subcalls.safe_argv(_spec(), "prompt", None, hostile)


def test_validate_resume_session_returns_value():
    assert subcalls.validate_resume_session("abc-123") == "abc-123"
    with pytest.raises(RunnerError):
        subcalls.validate_resume_session(None)
