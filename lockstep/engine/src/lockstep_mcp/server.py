"""The FastMCP app — the 11-tool lockstep MCP surface. Delegates
almost everything to `Engine`; the only logic that lives here is
`scenario_dryrun` (SHAPE-ONLY — never touches Engine, never
executes commands) and thin introspection wrappers around the recipes dir
and `RunIndex`.

**mcp SDK note:** this repo pins `mcp>=2.0,<3` (the code below imports
`mcp.server.mcpserver`, which only exists from 2.0 onward — `mcp>=1.0`
would silently resolve to a 1.x install with no such module). The resolved
version is 2.0.0, which renamed `FastMCP` to
`mcp.server.mcpserver.MCPServer` — there is no `mcp.server.fastmcp` module
in this SDK version at all. Imported here `as FastMCP`; the object is a
drop-in (`@app.tool()`, `app.run()`, `app._tool_manager.list_tools()` for
introspection — the SDK's real tool registry).

Lazy singleton (`_eng()` / `_reset_engine()`): the `Engine` is built once,
from `LOCKSTEP_STATE_DIR`/`LOCKSTEP_RECIPES` env vars (Global Constraints
defaults: `~/.lockstep`, `<cwd>/.lockstep/recipes`), on first tool call —
never at import time, so tests can set the env vars and call
`_reset_engine()` before exercising any tool.

`run.project` provenance: `scenario_start` captures the
server process cwd (`Path.cwd().resolve()`) as `project` — it is never a
tool argument. `scenario_dryrun` (which has no run/project of its own)
uses the same server-cwd convention for its containment check.

Two small helpers duplicate logic that already lives in `Engine`
(`_check_path_containment`) and `RunIndex`/`recipes_dir` access
(`list_runs`/`list_recipes` reach into `Engine._runs`/`Engine._recipes_dir`
directly — the public `Engine` surface is intentionally just
start/status/done/escalate/abort/recipe_path/route_log_path; those two
stores are the underlying persistence the server reads, not new engine
behavior).

`run_trace`/`render_flow` read `Engine.route_log_path(run_id)`. The
yamlgraph route-log env-var mechanism documented in `yamlgraph_api.py` is
still never wired into `engine.py`'s `start`/`resume` calls — instead
`Engine.done()` appends its own best-effort JSONL transition line to that
path on every completed transition. A run with no completed transition
yet still has no file:
`run_trace` -> `""`, `render_flow` renders with no overlay, honestly.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import asdict
from pathlib import Path

import yaml
from mcp.server.mcpserver import MCPServer as FastMCP

from lockstep_mcp import evidence as evidence_mod
from lockstep_mcp import profile_check
from lockstep_mcp import sessions
from lockstep_mcp import validators
from lockstep_mcp import yamlgraph_api as yg
from lockstep_mcp.engine import Engine, LockstepError

app = FastMCP("lockstep")

_engine: Engine | None = None

# scenario_dryrun runs ONLY these; command (cmd_ok, git_clean,
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
    """Same rule as `Engine._check_path_containment`, for
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
    compile, no run started. `scenario_dryrun` must never execute the
    graph, so it never goes through `yamlgraph_api.compile_recipe`."""
    with open(recipe_path) as f:
        doc = yaml.safe_load(f) or {}
    for node in (doc.get("nodes") or {}).values():
        if not isinstance(node, dict) or node.get("type") != "interrupt":
            continue
        message = node.get("message") or {}
        if message.get("step") == step:
            return message
    return None


def _assert_origin(run_id: str) -> None:
    """Origin binding: a run with `parent_run` set was minted for a
    spawned child session, and only the process carrying that session's
    credential (the LOCKSTEP_CHILD_RUN + LOCKSTEP_CHILD_NONCE pair
    `runners.child_env` injected at spawn) may drive it through the three
    mutating verbs. Read-only tools stay unbound; in-process `Engine` calls
    bypass this by construction.

    "Origin binding closes the SANCTIONED MCP surface. It does NOT close
    same-user OS access: on a multi-user OS a process environment may be
    reachable by other same-user processes through ordinary OS facilities,
    and a worker with shell can Bash-launch its own credentialed engine.
    That is the SAME same-user residual class v1 already carries."
    """
    record = _eng()._runs.get(run_id)  # noqa: SLF001
    if record.parent_run is None:
        return
    env_run = os.environ.get("LOCKSTEP_CHILD_RUN")
    env_nonce = os.environ.get("LOCKSTEP_CHILD_NONCE", "")
    # ORDER MATTERS: a parented record with a falsy nonce refuses BEFORE any
    # comparison — compare_digest("", "") matches, and the "" default for
    # env_nonce is safe ONLY because of this guard.
    if not record.nonce or env_run != run_id or not hmac.compare_digest(env_nonce, record.nonce):
        raise LockstepError(
            "this run belongs to a spawned subcall session; the caller lacks its credential")


# ---------------------------------------------------------------------------
# scenario_* — delegate to Engine
# ---------------------------------------------------------------------------


def _mark(res: dict) -> dict:
    """Stamp the binding marker into a response that names a run — the
    PostToolUse hook's name-agnostic recognition signal (see
    `sessions.BINDING_MARKER_KEY`). Responses without a `run_id` (done
    verdicts, terminal status shapes, listings) stay unstamped: nothing in
    them identifies a bindable touch."""
    if isinstance(res, dict) and isinstance(res.get("run_id"), str) and res["run_id"]:
        return {**res, sessions.BINDING_MARKER_KEY: sessions.BINDING_MARKER_VALUE}
    return res


@app.tool()
def scenario_start(recipe: str, vars: dict | None = None) -> dict:
    """Start a new run of `recipe`. `run.project` = the server process cwd
 — never an argument here."""
    project = str(Path.cwd().resolve())
    return _mark(_eng().start(recipe, vars or {}, project))


@app.tool()
def scenario_status(run_id: str) -> dict:
    return _mark(_eng().status(run_id))


@app.tool()
def scenario_done(run_id: str, step: str, evidence: dict) -> dict:
    _assert_origin(run_id)
    return _mark(_eng().done(run_id, step, evidence))


@app.tool()
def scenario_escalate(run_id: str, reason: str) -> dict:
    _assert_origin(run_id)
    return _mark(_eng().escalate(run_id, reason))


@app.tool()
def scenario_abort(run_id: str) -> dict:
    _assert_origin(run_id)
    return _mark(_eng().abort(run_id))


@app.tool()
def scenario_dryrun(recipe: str, step: str, evidence: dict) -> dict:
    """SHAPE-ONLY dryrun: applies the same `_`-prefix
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
            try:
                reasons = fn(check, raw_evidence, ctx) if fn else [f"unknown check type: {ctype!r}"]
            except Exception as e:  # noqa: BLE001 - a recipe-pinned
                # `path:` (never evidence-sourced, so `_containment_errors`
                # above never sees it) can still raise inside the check
                # itself (e.g. `_resolve_path`'s path-escape guard). dryrun
                # is a probe tool — it must report that cleanly, not crash
                # the whole tool call over one check's bad recipe-pinned path.
                results.append({"type": ctype, "verdict": "error", "reasons": [str(e)]})
                continue
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
    out = []
    for r in records:
        d = asdict(r)
        d.pop("nonce", None)                               # the spawn credential never goes on the wire
        out.append(d)
    return out


@app.tool()
def run_trace(run_id: str) -> str:
    p = _eng().route_log_path(run_id)
    if not p.exists():
        return ""
    return p.read_text()
