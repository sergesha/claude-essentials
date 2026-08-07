"""Task 6: the FastMCP app — the 11-tool lockstep MCP surface. Delegates
almost everything to `Engine` (Task 5); the only logic that lives here is
`scenario_dryrun` (decision 17, SHAPE-ONLY — never touches Engine, never
executes commands) and thin introspection wrappers around the recipes dir
and `RunIndex`.

**mcp SDK note:** this repo pins `mcp>=1.0` (Task 0); the resolved version
is 2.0.0, which renamed `FastMCP` (the class the plan's sketch names) to
`mcp.server.mcpserver.MCPServer` — there is no `mcp.server.fastmcp` module
in this SDK version at all. Imported here `as FastMCP` so the rest of this
module reads exactly like the plan's sketch; the object is otherwise a
drop-in (`@app.tool()`, `app.run()`, `app._tool_manager.list_tools()` for
introspection — the SDK's real tool registry, functionally identical to
the plan's anticipated `_tool_manager` fallback).

Lazy singleton (`_eng()` / `_reset_engine()`): the `Engine` is built once,
from `LOCKSTEP_STATE_DIR`/`LOCKSTEP_RECIPES` env vars (Global Constraints
defaults: `~/.lockstep`, `<cwd>/.lockstep/recipes`), on first tool call —
never at import time, so tests can set the env vars and call
`_reset_engine()` before exercising any tool.

`run.project` provenance (decision 11): `scenario_start` captures the
server process cwd (`Path.cwd().resolve()`) as `project` — it is never a
tool argument. `scenario_dryrun` (which has no run/project of its own)
uses the same server-cwd convention for its containment check.

Two small helpers duplicate logic that already lives in `Engine`
(`_check_path_containment`) and `RunIndex`/`recipes_dir` access
(`list_runs`/`list_recipes` reach into `Engine._runs`/`Engine._recipes_dir`
directly — Task 5's public `Engine` surface is intentionally just
start/status/done/escalate/abort/recipe_path/route_log_path, per the
plan's frozen interface list; those two stores are the underlying
persistence Task 6 needs read access to, not new engine behavior).

`run_trace`/`render_flow` read `Engine.route_log_path(run_id)`: nothing in
Tasks 1-5 currently writes that file (the yamlgraph route-log env-var
mechanism is documented in `yamlgraph_api.py` but never wired into
`engine.py`'s `start`/`resume` calls) — an absent route log is the honest
v1 answer (`run_trace` -> `""`, `render_flow` renders with no overlay),
not a bug to work around here.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

import yaml
from mcp.server.mcpserver import MCPServer as FastMCP

from lockstep_mcp import evidence as evidence_mod
from lockstep_mcp import profile_check
from lockstep_mcp import validators
from lockstep_mcp import yamlgraph_api as yg
from lockstep_mcp.engine import Engine

app = FastMCP("lockstep")

_engine: Engine | None = None

# decision 17: scenario_dryrun runs ONLY these; command (cmd_ok, git_clean,
# junit_gate) and baseline (fresh, unchanged, changed_in, diff_only) checks
# are reported `skipped (dryrun)` instead of executed.
SHAPE_CHECK_TYPES = {"file_exists", "file_nonempty", "md_has_sections", "file_matches"}


def _eng() -> Engine:
    global _engine
    if _engine is None:
        state_dir = Path(os.environ.get("LOCKSTEP_STATE_DIR", str(Path.home() / ".lockstep")))
        recipes_dir = Path(
            os.environ.get("LOCKSTEP_RECIPES", str(Path.cwd() / ".lockstep" / "recipes"))
        )
        _engine = Engine(state_dir, recipes_dir)
    return _engine


def _reset_engine() -> None:
    """Test-only: drop the lazy singleton so the next `_eng()` call rebuilds
    it from the (possibly just-changed) environment."""
    global _engine
    _engine = None


def _containment_errors(schema: dict | None, evidence: dict, project: str) -> list[str]:
    """Same rule as `Engine._check_path_containment` (decision 12), for
    `scenario_dryrun` — which has no `RunRecord` to read `project` from, so
    the caller passes the server cwd instead."""
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties") or {}
    base = Path(project).resolve()
    errors: list[str] = []
    for key, prop in props.items():
        if not isinstance(prop, dict) or prop.get("format") != "project-path":
            continue
        if key not in evidence:
            continue
        raw = evidence[key]
        if not isinstance(raw, str):
            errors.append(f"{key}: project-path value must be a string")
            continue
        resolved = (base / raw).resolve()
        if resolved != base and base not in resolved.parents:
            errors.append(f"{key}: path escapes project root: {raw!r}")
    return errors


def _load_step_brief(recipe_path: Path, step: str) -> dict | None:
    """Pure-YAML lookup of a step's `message` brief by name — no yamlgraph
    compile, no run started. `scenario_dryrun` must never execute the graph
    (decision 17), so it never goes through `yamlgraph_api.compile_recipe`."""
    with open(recipe_path) as f:
        doc = yaml.safe_load(f) or {}
    for node in (doc.get("nodes") or {}).values():
        if not isinstance(node, dict) or node.get("type") != "interrupt":
            continue
        message = node.get("message") or {}
        if message.get("step") == step:
            return message
    return None


# ---------------------------------------------------------------------------
# scenario_* — delegate to Engine
# ---------------------------------------------------------------------------


@app.tool()
def scenario_start(recipe: str, vars: dict | None = None) -> dict:
    """Start a new run of `recipe`. `run.project` = the server process cwd
    (decision 11) — never an argument here."""
    project = str(Path.cwd().resolve())
    return _eng().start(recipe, vars or {}, project)


@app.tool()
def scenario_status(run_id: str) -> dict:
    return _eng().status(run_id)


@app.tool()
def scenario_done(run_id: str, step: str, evidence: dict) -> dict:
    return _eng().done(run_id, step, evidence)


@app.tool()
def scenario_escalate(run_id: str, reason: str) -> dict:
    return _eng().escalate(run_id, reason)


@app.tool()
def scenario_abort(run_id: str) -> dict:
    return _eng().abort(run_id)


@app.tool()
def scenario_dryrun(recipe: str, step: str, evidence: dict) -> dict:
    """SHAPE-ONLY dryrun (decision 17): applies the same `_`-prefix
    rejection, schema validation, and path resolve+containment `done()`
    applies (project root = server cwd, since there is no run). Runs only
    shape checks; command/baseline checks report `skipped (dryrun)` and
    never execute. No RunIndex entry, no snapshot, no baseline artifact —
    nothing durable, nothing besides shape checks actually runs."""
    eng = _eng()
    recipe_path = eng.recipe_path(recipe)
    if not recipe_path.exists():
        raise ValueError(f"recipe not found: {recipe}")
    brief = _load_step_brief(recipe_path, step)
    if brief is None:
        raise ValueError(f"step {step!r} not found in recipe {recipe!r}")

    raw_evidence = evidence or {}

    forged = [k for k in raw_evidence if k.startswith("_")]
    if forged:
        return {
            "accepted": False,
            "errors": [f"reserved evidence key(s) rejected: {sorted(forged)}"],
        }

    schema = brief.get("evidence_schema")
    schema_errors = evidence_mod.validate_evidence(schema, raw_evidence)
    if schema_errors:
        return {"accepted": False, "errors": schema_errors}

    project = str(Path.cwd().resolve())
    path_errors = _containment_errors(schema, raw_evidence, project)
    if path_errors:
        return {"accepted": False, "errors": path_errors}

    ctx = {"_project": project}
    results = []
    for check in brief.get("checks") or []:
        ctype = check.get("type")
        if ctype in SHAPE_CHECK_TYPES:
            fn = validators.CHECKS.get(ctype)
            reasons = fn(check, raw_evidence, ctx) if fn else [f"unknown check type: {ctype!r}"]
            results.append(
                {"type": ctype, "verdict": "pass" if not reasons else "fail", "reasons": reasons}
            )
        else:
            results.append({"type": ctype, "verdict": "skipped (dryrun)"})

    return {"accepted": True, "results": results}


# ---------------------------------------------------------------------------
# recipe / run introspection
# ---------------------------------------------------------------------------


@app.tool()
def list_recipes() -> list[str]:
    d = Path(_eng()._recipes_dir)  # noqa: SLF001 - same-package internal, no public accessor
    return sorted(p.stem for p in d.glob("*.yaml"))


@app.tool()
def validate_recipe(path: str) -> dict:
    p = Path(path)
    yg_ok, yg_msg = yg.cli_validate(p)
    errors, warnings = profile_check.check_recipe_full(p)
    return {
        "ok": yg_ok and not errors,
        "yamlgraph": {"ok": yg_ok, "message": yg_msg},
        "errors": errors,
        "warnings": warnings,
    }


@app.tool()
def render_flow(recipe: str, run_id: str | None = None) -> str:
    eng = _eng()
    recipe_path = eng.recipe_path(recipe)
    overlay = None
    if run_id:
        route_log = eng.route_log_path(run_id)
        if route_log.exists():
            overlay = route_log
    return yg.cli_mermaid(recipe_path, overlay)


@app.tool()
def list_runs(project: str | None = None, active_only: bool = False) -> list[dict]:
    records = _eng()._runs.list(project=project, active_only=active_only)  # noqa: SLF001
    return [asdict(r) for r in records]


@app.tool()
def run_trace(run_id: str) -> str:
    p = _eng().route_log_path(run_id)
    if not p.exists():
        return ""
    return p.read_text()
