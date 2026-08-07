"""Task 6: server.py — the MCP tool surface. Delegates to `Engine` (Task 5)
through a lazy `_eng()` singleton built from `LOCKSTEP_STATE_DIR`/
`LOCKSTEP_RECIPES` env vars; `_reset_engine()` lets each test rebuild it
against a fresh tmp state/recipes dir.

Uses the mcp SDK's real `MCPServer` (imported here as `FastMCP` — see
server.py docstring for why: the 2.0.0 `mcp` package this repo pins under
`mcp>=2.0,<3` renamed the class, there is no `mcp.server.fastmcp` module).
Tool registration is introspected via `app._tool_manager.list_tools()`
(the actual SDK's registry — the plan's `_tool_manager` fallback landed).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lockstep_mcp import server
from lockstep_mcp.engine import LockstepError

FIXTURES = Path(__file__).parent / "fixtures" / "recipes"
GOOD = FIXTURES / "good"
BAD = FIXTURES / "bad"

EXPECTED_TOOLS = {
    "scenario_start",
    "scenario_status",
    "scenario_done",
    "scenario_escalate",
    "scenario_abort",
    "scenario_dryrun",
    "list_recipes",
    "validate_recipe",
    "render_flow",
    "list_runs",
    "run_trace",
}


def _configure(monkeypatch, tmp_path, recipes_dir=GOOD):
    # state dir is a SIBLING of the project cwd, never inside it — a state
    # dir inside the project tree is refused (runners.assert_state_dir_sane).
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(recipes_dir))
    monkeypatch.chdir(project)
    server._reset_engine()
    return project


# ---------------------------------------------------------------------------
# tool registration
# ---------------------------------------------------------------------------


def test_tools_registered():
    names = {t.name for t in server.app._tool_manager.list_tools()}
    assert names == EXPECTED_TOOLS


# ---------------------------------------------------------------------------
# scenario_start / scenario_done roundtrip
# ---------------------------------------------------------------------------


def test_start_and_done_roundtrip(tmp_path, monkeypatch):
    project = _configure(monkeypatch, tmp_path)

    res = server.scenario_start("minimal", {})
    run_id = res["run_id"]
    assert res["step"] == "one"
    assert res["evidence_schema"] is not None

    status = server.scenario_status(run_id)
    assert status["status"] == "awaiting"
    assert status["step"] == "one"

    artifact_dir = project / ".lockstep"
    artifact_dir.mkdir()
    (artifact_dir / "a.md").write_text("hi")

    result = server.scenario_done(run_id, "one", {"path": ".lockstep/a.md"})
    assert result["accepted"] is True
    assert result["passed"] is True
    assert result["done"] is True

    final_status = server.scenario_status(run_id)
    assert final_status["status"] == "done"


# ---------------------------------------------------------------------------
# validate_recipe
# ---------------------------------------------------------------------------


def test_validate_recipe_reports_profile(tmp_path, monkeypatch):
    _configure(monkeypatch, tmp_path)

    good = server.validate_recipe(str(GOOD / "minimal.yaml"))
    assert good["ok"] is True
    assert good["errors"] == []
    assert good["yamlgraph"]["ok"] is True

    bad = server.validate_recipe(str(BAD / "llm-node.yaml"))
    assert bad["ok"] is False
    assert any("forbidden node type" in e for e in bad["errors"])


# ---------------------------------------------------------------------------
# scenario_dryrun — SHAPE-ONLY (decision 17)
# ---------------------------------------------------------------------------


def test_scenario_dryrun_is_shape_only_and_leaves_nothing_durable(tmp_path, monkeypatch):
    project = _configure(monkeypatch, tmp_path)

    sentinel = tmp_path / "DRYRUN-SENTINEL-SHOULD-NOT-EXIST"
    artifact_dir = project / ".lockstep"
    artifact_dir.mkdir()
    (artifact_dir / "a.md").write_text("hi")

    result = server.scenario_dryrun("mixed-checks", "one", {"path": ".lockstep/a.md"})

    assert result["accepted"] is True
    verdicts = {r["type"]: r["verdict"] for r in result["results"]}
    assert verdicts["file_exists"] == "pass"
    assert verdicts["cmd_ok"] == "skipped (dryrun)"

    # the sentinel command was never actually run
    assert not sentinel.exists()

    # nothing durable: no run was ever created
    assert server.list_runs() == []


def test_scenario_dryrun_reports_shape_failure(tmp_path, monkeypatch):
    _configure(monkeypatch, tmp_path)

    result = server.scenario_dryrun("mixed-checks", "one", {"path": ".lockstep/missing.md"})

    assert result["accepted"] is True
    verdicts = {r["type"]: r["verdict"] for r in result["results"]}
    assert verdicts["file_exists"] == "fail"
    assert verdicts["cmd_ok"] == "skipped (dryrun)"
    assert server.list_runs() == []


def test_scenario_dryrun_reports_clean_error_on_recipe_pinned_path_escape(tmp_path, monkeypatch):
    """item 13: a shape check's `path:` is recipe-PINNED (not evidence-
    sourced), so `_containment_errors` — which only resolves/contains
    evidence keys annotated `format: project-path` — never sees it. The
    literal `../outside.md` in `dryrun-path-escape.yaml` reaches
    `validators._resolve_path` raw and raises ValueError; `scenario_dryrun`
    must turn that into a clean per-check error result, not an uncaught
    crash."""
    _configure(monkeypatch, tmp_path)

    result = server.scenario_dryrun("dryrun-path-escape", "one", {"note": "x"})

    assert result["accepted"] is True
    entry = next(r for r in result["results"] if r["type"] == "file_exists")
    assert entry["verdict"] == "error"
    assert any("escape" in reason.lower() for reason in entry["reasons"])


def test_scenario_dryrun_rejects_forged_verdict(tmp_path, monkeypatch):
    _configure(monkeypatch, tmp_path)

    result = server.scenario_dryrun(
        "mixed-checks", "one", {"path": ".lockstep/a.md", "_verdict_status": "pass"}
    )
    assert result["accepted"] is False


# ---------------------------------------------------------------------------
# scenario_abort
# ---------------------------------------------------------------------------


def test_scenario_abort_excludes_run_from_active_list(tmp_path, monkeypatch):
    _configure(monkeypatch, tmp_path)

    res = server.scenario_start("minimal", {})
    run_id = res["run_id"]

    result = server.scenario_abort(run_id)
    assert result["status"] == "aborted"

    status = server.scenario_status(run_id)
    assert status["status"] == "aborted"

    active = server.list_runs(active_only=True)
    assert all(r["run_id"] != run_id for r in active)

    with pytest.raises(LockstepError):
        server.scenario_done(run_id, "one", {"path": "x"})


# ---------------------------------------------------------------------------
# list_recipes / render_flow / run_trace
# ---------------------------------------------------------------------------


def test_list_recipes_render_flow_and_run_trace(tmp_path, monkeypatch):
    _configure(monkeypatch, tmp_path)

    names = server.list_recipes()
    assert "minimal" in names
    assert "two-steps" in names

    mermaid = server.render_flow("minimal")
    assert "step_one" in mermaid

    res = server.scenario_start("minimal", {})
    run_id = res["run_id"]

    # nothing populates route_log_path yet in v1 — empty string is the
    # honest answer, not a bug (see yamlgraph_api.py route-log probe note).
    assert server.run_trace(run_id) == ""

    mermaid_with_run = server.render_flow("minimal", run_id)
    assert "step_one" in mermaid_with_run
