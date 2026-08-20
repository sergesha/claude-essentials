"""The FastMCP app — the 11-tool lockstep MCP surface.

Scenario lifecycle operations delegate to the state-free ``Engine`` facade;
read-only tools project immutable catalog bindings and native checkpoints.
``scenario_dryrun`` remains shape-only and never executes commands.

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

The server never owns workflow transitions or a second status vocabulary.
``run_trace`` reads native checkpoint history and ``render_flow`` compiles
only an authority-checked immutable recipe materialization.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml
from mcp.server.mcpserver import Context
from mcp.server.mcpserver import MCPServer as FastMCP

from lockstep.recipe import profile
from lockstep.recipe import yamlgraph_adapter as yg
from lockstep.recipe.authority import (
    RecipeAuthorityError,
    RecipeAuthorityPolicy,
    StrictRecipeIngress,
)
from lockstep.recipe.loader import RecipeLoader
from lockstep.runtime import evidence as evidence_mod
from lockstep.runtime import sessions, validators
from lockstep.runtime.engine import Engine
from lockstep.runtime.recipe_bundles import RecipeBundleStore
from lockstep.runtime.service import (
    preflight_recipe,
    validate_evidence_payload,
    validate_evidence_shape,
    validate_reason_payload,
    validate_start_input,
)

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
    state_dir, recipes_dir = _configured_paths(project_root)
    config = (state_dir, recipes_dir)
    if _engine is None or _engine_config != config:
        # `or`, never a get() default — an unset variable the plugin
        # manifest forwards arrives present and EMPTY, and `Path("")` is
        # the cwd, which would put run state inside the project tree.
        if _engine is not None:
            _engine.close()
        _engine = Engine(state_dir, recipes_dir)
        _engine_config = config
    return _engine


def _configured_paths(project_root: Path) -> tuple[Path, Path]:
    """Resolve configured paths without constructing persistent services."""
    state_dir = Path(os.environ.get("LOCKSTEP_STATE_DIR") or str(Path.home() / ".lockstep"))
    recipes_dir = Path(
        os.environ.get("LOCKSTEP_RECIPES") or str(project_root / ".lockstep" / "recipes")
    )
    return state_dir.resolve(), recipes_dir.resolve()


def _reset_engine() -> None:
    """Test-only: drop the lazy singleton so the next `_eng()` call rebuilds
    it from the (possibly just-changed) environment."""
    global _engine, _engine_config
    if _engine is not None:
        _engine.close()
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


def _assert_origin(
    run_id: str, session_id: str | None, project: Path | None = None
) -> None:
    """Require the current public run's native worker-session binding.

    Native subgraphs have no public child identity or environment credential.
    The service verifies the same binding again and holds its mutation lock
    through resume commit, so this MCP-edge check is an early fail-closed guard
    rather than authority of its own.
    """
    project_root = (project or Path.cwd()).resolve()
    _eng(project_root).require_session(run_id, session_id, str(project_root))


def _session_for_context(ctx: Context | None) -> str | None:
    """Read the authenticated session correlation supplied by the MCP edge."""
    if ctx is None:
        return None
    try:
        meta = ctx.request_context.meta
    except (AttributeError, ValueError):
        return None
    if isinstance(meta, dict):
        value = meta.get("session_id") or meta.get("x-lockstep-session-id")
        if isinstance(value, str) and value:
            return value
    return None


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
    values = validate_start_input(vars)
    _state_dir, recipes_dir = _configured_paths(project)
    authorized = preflight_recipe(recipes_dir, recipe)
    return _mark(
        _eng(project).start_authorized(recipe, authorized, values, str(project))
    )


@app.tool()
def scenario_status(run_id: str, ctx: Context | None = None) -> dict:
    project = _project_for_context(ctx)
    return _mark(_eng(project).status(run_id, str(project)))


@app.tool()
def scenario_done(run_id: str, step: str, evidence: dict, ctx: Context | None = None) -> dict:
    checked_evidence = validate_evidence_payload(evidence)
    project = _project_for_context(ctx)
    session_id = _session_for_context(ctx)
    _assert_origin(run_id, session_id, project)
    return _mark(_eng(project).done(
        run_id, step, checked_evidence, session_id=session_id, project=str(project)
    ))


@app.tool()
def scenario_escalate(run_id: str, reason: str, ctx: Context | None = None) -> dict:
    checked_reason = validate_reason_payload(reason)
    project = _project_for_context(ctx)
    session_id = _session_for_context(ctx)
    _assert_origin(run_id, session_id, project)
    return _mark(_eng(project).escalate(
        run_id, checked_reason, session_id=session_id, project=str(project)
    ))


@app.tool()
def scenario_abort(run_id: str, ctx: Context | None = None) -> dict:
    project = _project_for_context(ctx)
    session_id = _session_for_context(ctx)
    _assert_origin(run_id, session_id, project)
    return _mark(_eng(project).abort(
        run_id, session_id=session_id, project=str(project)
    ))


@app.tool()
def scenario_dryrun(
    recipe: str, step: str, evidence: dict, ctx: Context | None = None
) -> dict:
    """SHAPE-ONLY dryrun: applies the same `_`-prefix
    rejection, schema validation, and path resolve+containment `done()`
    applies (project root = server cwd, since there is no run). Runs only
    shape checks; command/baseline checks report `skipped (dryrun)` and
    never execute. No catalog entry, checkpoint, or baseline artifact —
    nothing durable, nothing besides shape checks actually runs."""
    raw_evidence = validate_evidence_shape(evidence)
    forged = [key for key in raw_evidence if key.startswith("_")]
    if forged:
        return {
            "accepted": False,
            "errors": [f"reserved evidence key(s) rejected: {sorted(forged)}"],
        }
    project_root = _project_for_context(ctx)
    _state_dir, recipes_dir = _configured_paths(project_root)
    authorized = preflight_recipe(recipes_dir, recipe)
    with tempfile.TemporaryDirectory(prefix="lockstep-dryrun-") as raw:
        store = RecipeBundleStore(Path(raw) / "owner-state")
        materialized = authorized.capture(store).materialize(store)
        brief = _load_step_brief(materialized.source_path, step)
    if brief is None:
        raise ValueError(f"step {step!r} not found in recipe {recipe!r}")

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
    _state_dir, recipes_dir = _configured_paths(_project_for_context(ctx))
    return sorted(RecipeLoader(recipes_dir).discover())


@app.tool()
def validate_recipe(path: str, ctx: Context | None = None) -> dict:
    project = _project_for_context(ctx)
    p = Path(path)
    if not p.is_absolute():
        p = project / p
    p = p.absolute()
    try:
        candidate = StrictRecipeIngress(p.parent).inspect(p.name)
        # Until native typed effects can carry a coordinate-bound executable
        # grant, validation is intentionally declarative-only.  In particular,
        # it must not import a recipe-selected Python module merely to report
        # diagnostics.
        authorized = candidate.authorize(RecipeAuthorityPolicy())
    except (OSError, RecipeAuthorityError, ValueError) as exc:
        message = str(exc)
        return {
            "ok": False,
            "yamlgraph": {"ok": False, "message": f"not compiled: {message}"},
            "errors": [message],
            "warnings": [],
        }

    with tempfile.TemporaryDirectory(prefix="lockstep-recipe-validation-") as raw:
        store = RecipeBundleStore(Path(raw) / "owner-state")
        materialized = authorized.capture(store).materialize(store)
        errors, warnings = profile.check_recipe_full(materialized.source_path)
        if errors:
            return {
                "ok": False,
                "yamlgraph": {
                    "ok": False,
                    "message": "not compiled: Lockstep profile rejected recipe",
                },
                "errors": errors,
                "warnings": warnings,
            }
        yg_ok, yg_msg = yg.validate_native(materialized)
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
    _state_dir, recipes_dir = _configured_paths(_project_for_context(ctx))
    authorized = preflight_recipe(recipes_dir, recipe)
    with tempfile.TemporaryDirectory(prefix="lockstep-render-") as raw:
        store = RecipeBundleStore(Path(raw) / "owner-state")
        materialized = authorized.capture(store).materialize(store)
        # Native history no longer fabricates a yamlgraph route-log overlay.
        # ``run_id`` is retained in the public signature for compatibility.
        del run_id
        return yg.render_native(materialized)


@app.tool()
def list_runs(
    active_only: bool = False,
    ctx: Context | None = None,
) -> list[dict]:
    project_root = _project_for_context(ctx)
    records = _eng(project_root).list_runs(str(project_root))
    if active_only:
        records = [item for item in records if item["status"] in {"starting", "awaiting", "running"}]
    return records


@app.tool()
def run_trace(run_id: str, ctx: Context | None = None) -> str:
    project = _project_for_context(ctx)
    return "\n".join(
        str(item) for item in _eng(project).history(run_id, str(project))
    )
