"""end-to-end fake-agent cycle over the REAL `feature-dev` example
recipe (`lockstep/recipes/examples/feature-dev.yaml`) — the one thing no
unit test exercises: durability across a full engine restart, terminal
escalation, and abort, all through the same surfaces a real agent/hook
setup uses (`server.py` tools + `cli.hook_stop`).

Only the "plan" step is driven to completion here (shape + `fresh` checks).
Accepted gap: `junit_gate`/`changed_in`/`diff_only`/
`unchanged` have unit coverage in `test_validators.py` but no e2e pass in
this file — driving `implement`/`test`/`review` to completion would mean
actually running `pytest` on a nested fixture project, out of scope here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import lockstep_mcp.cli as cli
from lockstep_mcp import server
from lockstep_mcp.engine import LockstepError

EXAMPLES = Path(__file__).resolve().parents[2] / "recipes" / "examples"


def _configure(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(EXAMPLES))
    monkeypatch.chdir(project)
    server._reset_engine()
    return project


def test_full_cycle_restart_durability_terminal_escalation_abort(tmp_path, monkeypatch):
    project = _configure(monkeypatch, tmp_path)
    state_dir = tmp_path / "state"

    # --- start: parked on "plan" -------------------------------------
    res = server.scenario_start("feature-dev", {})
    run_id = res["run_id"]
    assert res["step"] == "plan"

    # --- reject empty evidence; state untouched -----------------------
    rejected = server.scenario_done(run_id, "plan", {})
    assert rejected["accepted"] is False

    # --- satisfy the plan step's checks: md_has_sections + fresh -------
    lockstep_dir = project / ".lockstep"
    lockstep_dir.mkdir()
    (lockstep_dir / "plan.md").write_text("# Approach\n...\n\n# Steps\n1. do it\n")

    result = server.scenario_done(run_id, "plan", {"plan_path": ".lockstep/plan.md"})
    assert result["accepted"] is True
    assert result["passed"] is True
    assert result["step"] == "implement"

    # --- route log carries the completed "plan" transition -------------
    trace = server.run_trace(run_id)
    assert trace != ""
    assert "plan" in trace

    # --- the route log is in yamlgraph's OWN `event: "route"` shape,
    # so render_flow's overlay actually picks it up (not just a private
    # "transition" event only our own code could ever read back) ---------
    overlay = server.render_flow("feature-dev", run_id)
    assert "#1" in overlay  # yamlgraph's ordinal decision marker

    # --- simulate a full server restart: fresh Engine, same state dir --
    server._reset_engine()

    status = server.scenario_status(run_id)
    assert status["step"] == "implement"

    exit_code, out = cli.hook_stop({"stop_hook_active": False}, state_dir, str(project))
    assert exit_code == 0
    assert "implement" in out

    # --- escalate: terminal, no further reports accepted ---------------
    esc = server.scenario_escalate(run_id, "blocked on missing dependency")
    assert esc["status"] == "escalated"

    with pytest.raises(LockstepError):
        server.scenario_done(run_id, "implement", {"summary": "x"})

    # --- second run: start then abort -----------------------------------
    res2 = server.scenario_start("feature-dev", {})
    run_id2 = res2["run_id"]

    aborted = server.scenario_abort(run_id2)
    assert aborted["status"] == "aborted"

    exit_code2, out2 = cli.hook_stop({"stop_hook_active": False}, state_dir, str(project))
    assert exit_code2 == 0
    assert out2 == ""

    assert server.list_runs(active_only=True) == []
