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
defaults: `~/.lockstep`, `<resolved host project>/.lockstep/recipes`), on first
tool call — never at import time, so tests can set the env vars and call
`_reset_engine()` before exercising any tool.

`run.project` provenance is never a tool argument: Claude supplies the server
process cwd; Codex supplies the active workspace in MCP request metadata.
`scenario_dryrun` uses the same resolved host project for containment checks.

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
from mcp.server.mcpserver import Context
from mcp.server.mcpserver import MCPServer as FastMCP

from lockstep_mcp import evidence as evidence_mod
from lockstep_mcp import profile_check
from lockstep_mcp import sessions
from lockstep_mcp import validators
from lockstep_mcp import yamlgraph_api as yg
from lockstep_mcp.engine import Engine, LockstepError

app = FastMCP("lockstep")

_engine: Engine | None = None
_engine_config: tuple[Path, Path] | None = None

# scenario_dryrun runs ONLY these; command (cmd_ok, git_clean,
# junit_gate) and baseline (fresh, unchanged, changed_in, diff_only) checks
# are reported `skipped (dryrun)` instead of executed.
SHAPE_CHECK_TYPES = {"file_exists", "file_nonempty", "md_has_sections", "file_matches"}


def _project_for_context(ctx: Context | None) -> Path:
    """Resolve the host project without exposing it as a tool argument.

    Claude starts the server in the project directory. Codex starts bundled
    plugin commands from the plugin root, but includes the active workspace in
    its per-call metadata. Unknown clients retain the cwd convention.
    """
    if ctx is not None:
        try:
            meta = ctx.request_context.meta
        except (AttributeError, ValueError):
            meta = None
        if isinstance(meta, dict):
            turn = meta.get("x-codex-turn-metadata")
            workspaces = turn.get("workspaces") if isinstance(turn, dict) else None
            if isinstance(workspaces, dict):
                for workspace in workspaces:
                    if isinstance(workspace, str) and workspace:
                        return Path(workspace).resolve()
    return Path.cwd().resolve()


def _eng(project: Path | None = None) -> Engine:
    global _engine, _engine_config
    project_root = (project or Path.cwd()).resolve()
    state_dir = Path(os.environ.get("LOCKSTEP_STATE_DIR") or str(Path.home() / ".lockstep"))
    recipes_dir = Path(
        os.environ.get("LOCKSTEP_RECIPES") or str(project_root / ".lockstep" / "recipes")
    )
    config = (state_dir.resolve(), recipes_dir.resolve())
    if _engine is None or _engine_config != config:
        # `or`, never a get() default — an unset variable the plugin
        # manifest forwards arrives present and EMPTY, and `Path("")` is
        # the cwd, which would put run state inside the project tree.
        _engine = Engine(state_dir, recipes_dir)
        _engine_config = config
    return _engine


def _reset_engine() -> None:
    """Test-only: drop the lazy singleton so the next `_eng()` call rebuilds
    it from the (possibly just-changed) environment."""
    global _engine, _engine_config
    _engine = None
    _engine_config = None


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


def _assert_origin(run_id: str, project: Path | None = None) -> None:
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
    record = _eng(project)._runs.get(run_id)  # noqa: SLF001
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
def scenario_start(recipe: str, vars: dict | None = None, ctx: Context | None = None) -> dict:
    """Start a new run of `recipe`. `run.project` = the server process cwd
 — never an argument here."""
    project = _project_for_context(ctx)
    return _mark(_eng(project).start(recipe, vars or {}, str(project)))


@app.tool()
def scenario_status(run_id: str, ctx: Context | None = None) -> dict:
    return _mark(_eng(_project_for_context(ctx)).status(run_id))


@app.tool()
def scenario_done(run_id: str, step: str, evidence: dict, ctx: Context | None = None) -> dict:
    project = _project_for_context(ctx)
    _assert_origin(run_id, project)
    return _mark(_eng(project).done(run_id, step, evidence))


@app.tool()
def scenario_escalate(run_id: str, reason: str, ctx: Context | None = None) -> dict:
    project = _project_for_context(ctx)
    _assert_origin(run_id, project)
    return _mark(_eng(project).escalate(run_id, reason))


@app.tool()
def scenario_abort(run_id: str, ctx: Context | None = None) -> dict:
    project = _project_for_context(ctx)
    _assert_origin(run_id, project)
    return _mark(_eng(project).abort(run_id))


@app.tool()
def scenario_dryrun(
    recipe: str, step: str, evidence: dict, ctx: Context | None = None
) -> dict:
    """SHAPE-ONLY dryrun: applies the same `_`-prefix
    rejection, schema validation, and path resolve+containment `done()`
    applies (project root = server cwd, since there is no run). Runs only
    shape checks; command/baseline checks report `skipped (dryrun)` and
    never execute. No RunIndex entry, no snapshot, no baseline artifact —
    nothing durable, nothing besides shape checks actually runs."""
    project_root = _project_for_context(ctx)
    eng = _eng(project_root)
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

    project = str(project_root)
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
def list_recipes(ctx: Context | None = None) -> list[str]:
    d = Path(
        _eng(_project_for_context(ctx))._recipes_dir  # noqa: SLF001
    )
    return sorted(p.stem for p in d.glob("*.yaml"))


@app.tool()
def validate_recipe(path: str, ctx: Context | None = None) -> dict:
    project = _project_for_context(ctx)
    p = Path(path)
    if not p.is_absolute():
        p = project / p
    yg_ok, yg_msg = yg.cli_validate(p)
    errors, warnings = profile_check.check_recipe_full(p)
    return {
        "ok": yg_ok and not errors,
        "yamlgraph": {"ok": yg_ok, "message": yg_msg},
        "errors": errors,
        "warnings": warnings,
    }


@app.tool()
def render_flow(
    recipe: str, run_id: str | None = None, ctx: Context | None = None
) -> str:
    eng = _eng(_project_for_context(ctx))
    recipe_path = eng.recipe_path(recipe)
    overlay = None
    if run_id:
        route_log = eng.route_log_path(run_id)
        if route_log.exists():
            overlay = route_log
    return yg.cli_mermaid(recipe_path, overlay)


@app.tool()
def list_runs(
    project: str | None = None,
    active_only: bool = False,
    ctx: Context | None = None,
) -> list[dict]:
    records = _eng(_project_for_context(ctx))._runs.list(  # noqa: SLF001
        project=project, active_only=active_only
    )
    out = []
    for r in records:
        d = asdict(r)
        d.pop("nonce", None)                               # the spawn credential never goes on the wire
        out.append(d)
    return out


@app.tool()
def run_trace(run_id: str, ctx: Context | None = None) -> str:
    p = _eng(_project_for_context(ctx)).route_log_path(run_id)
    if not p.exists():
        return ""
    return p.read_text()
