"""The FastMCP app — thin native runtime and authoring surfaces.

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

Lazy handles (`_command_for()` / `_projection_for()` / `_reset_engine()`) are built once,
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

from lockstep.authoring import (
    check_recovered_recipe,
    diff_recovered_recipe,
    estimate_recipe,
    initialize_minimal,
    publish_project_compilation,
    render_recipe,
)
from lockstep.authoring_publisher import observe_authoring_project
from lockstep.mcp._scenario_dryrun import (
    evaluate_scenario_dryrun,
    prevalidate_scenario_evidence,
)
from lockstep.recipe import profile
from lockstep.recipe import yamlgraph_adapter as yg
from lockstep.recipe.authority import (
    RecipeAuthorityError,
    RecipeAuthorityPolicy,
    StrictRecipeIngress,
)
from lockstep.recipe.loader import RecipeLoader
from lockstep.runtime import sessions
from lockstep.runtime.engine import Engine
from lockstep.runtime.projection import RuntimeProjection
from lockstep.runtime.recipe_bundles import RecipeBundleStore
from lockstep.runtime.service import (
    LockstepCommandService,
    preflight_recipe,
    validate_evidence_payload,
    validate_reason_payload,
    validate_start_input,
)

app = FastMCP("lockstep")

_command: LockstepCommandService | None = None
_command_config: tuple[Path, Path] | None = None
_projection: RuntimeProjection | None = None
_projection_config: tuple[Path, Path] | None = None

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


def _command_for(project: Path | None = None) -> LockstepCommandService:
    global _command, _command_config
    project_root = (project or Path.cwd()).resolve()
    state_dir, recipes_dir = _configured_paths(project_root)
    config = (state_dir, recipes_dir)
    if _command is None or _command_config != config:
        # `or`, never a get() default — an unset variable the plugin
        # manifest forwards arrives present and EMPTY, and `Path("")` is
        # the cwd, which would put run state inside the project tree.
        if _command is not None:
            _command.close()
        _command = Engine.command(state_dir, recipes_dir)
        _command_config = config
    return _command


def _projection_for(project: Path | None = None) -> RuntimeProjection:
    global _projection, _projection_config
    project_root = (project or Path.cwd()).resolve()
    state_dir, recipes_dir = _configured_paths(project_root)
    config = (state_dir, recipes_dir)
    if _projection is None or _projection_config != config:
        if _projection is not None:
            _projection.close()
        _projection = Engine.observe(state_dir, recipes_dir)
        _projection_config = config
    return _projection


def _configured_paths(project_root: Path) -> tuple[Path, Path]:
    """Resolve configured paths without constructing persistent services."""
    state_dir = Path(os.environ.get("LOCKSTEP_STATE_DIR") or str(Path.home() / ".lockstep"))
    recipes_dir = Path(
        os.environ.get("LOCKSTEP_RECIPES") or str(project_root / ".lockstep" / "recipes")
    )
    return state_dir.absolute(), recipes_dir.resolve()


def _reset_engine() -> None:
    """Test-only: drop lazy handles so the next capability call rebuilds
    it from the (possibly just-changed) environment."""
    global _command, _command_config, _projection, _projection_config
    if _command is not None:
        _command.close()
    _command = None
    _command_config = None
    if _projection is not None:
        _projection.close()
    _projection = None
    _projection_config = None


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
    _command_for(project_root).require_session(run_id, session_id, str(project_root))


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
    return _mark(_command_for(project).start(recipe, values, str(project)))


@app.tool()
def scenario_status(run_id: str, ctx: Context | None = None) -> dict:
    project = _project_for_context(ctx)
    return _mark(_projection_for(project).status(run_id, str(project)))


@app.tool()
def scenario_done(run_id: str, step: str, evidence: dict, ctx: Context | None = None) -> dict:
    checked_evidence = validate_evidence_payload(evidence)
    project = _project_for_context(ctx)
    session_id = _session_for_context(ctx)
    _assert_origin(run_id, session_id, project)
    return _mark(_command_for(project).done(
        run_id, step, checked_evidence, session_id=session_id, project=str(project)
    ))


@app.tool()
def scenario_escalate(run_id: str, reason: str, ctx: Context | None = None) -> dict:
    checked_reason = validate_reason_payload(reason)
    project = _project_for_context(ctx)
    session_id = _session_for_context(ctx)
    _assert_origin(run_id, session_id, project)
    return _mark(_command_for(project).escalate(
        run_id, checked_reason, session_id=session_id, project=str(project)
    ))


@app.tool()
def scenario_abort(run_id: str, ctx: Context | None = None) -> dict:
    project = _project_for_context(ctx)
    session_id = _session_for_context(ctx)
    _assert_origin(run_id, session_id, project)
    return _mark(_command_for(project).abort(
        run_id, session_id=session_id, project=str(project)
    ))


@app.tool()
def scenario_accept_artifact(
    token: str,
    ctx: Context | None = None,
) -> dict:
    project = _project_for_context(ctx)
    return _mark(
        _command_for(project).scenario_accept_artifact(token, project=str(project))
    )


@app.tool()
def scenario_wait(
    run_id: str, timeout_seconds: int = 30, ctx: Context | None = None
) -> dict:
    """Wait boundedly for a read-only native status revision."""

    project = _project_for_context(ctx)
    return _mark(
        _projection_for(project).wait(run_id, timeout_seconds, str(project))
    )


@app.tool()
def scenario_history(run_id: str, ctx: Context | None = None) -> list[dict]:
    """Return the bounded redacted native checkpoint history."""

    project = _project_for_context(ctx)
    return _projection_for(project).history(run_id, str(project))


@app.tool()
def scenario_events(run_id: str, ctx: Context | None = None) -> list[dict]:
    """Return bounded native/effect observations without advancing the run."""

    project = _project_for_context(ctx)
    return _projection_for(project).events(run_id, str(project))


@app.tool()
def scenario_recover(limit: int = 128, ctx: Context | None = None) -> dict:
    """Explicitly perform one bounded durable-recovery sweep."""

    project = _project_for_context(ctx)
    return _command_for(project).scenario_recover(str(project), limit=limit)


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
    raw_evidence, reserved_error = prevalidate_scenario_evidence(evidence)
    if reserved_error is not None:
        return reserved_error
    project_root = _project_for_context(ctx)
    _state_dir, recipes_dir = _configured_paths(project_root)
    return evaluate_scenario_dryrun(
        recipes_dir,
        recipe,
        step,
        raw_evidence,
        project_root=project_root,
        containment_errors=_containment_errors,
        load_step_brief=_load_step_brief,
        preflight=preflight_recipe,
    )


# ---------------------------------------------------------------------------
# recipe / run introspection
# ---------------------------------------------------------------------------


@app.tool()
def recipe_init(name: str, ctx: Context | None = None) -> dict:
    project = _project_for_context(ctx)
    owner_state, _recipes = _configured_paths(project)
    recipe = initialize_minimal(project, name, state_dir=owner_state)
    return {
        "name": name,
        "workflow": str(recipe.workflow_path.relative_to(project)),
        "recipe": str(recipe.recipe_path.relative_to(project)),
    }


@app.tool()
def recipe_compile(name: str, ctx: Context | None = None) -> dict:
    project = _project_for_context(ctx)
    owner_state, _recipes = _configured_paths(project)
    result = publish_project_compilation(project, name, state_dir=owner_state)
    return {
        "name": name,
        "digest": result.digest,
        "source_bundle_sha256": result.bundle_sha256,
    }


@app.tool()
def recipe_check(name: str, ctx: Context | None = None) -> dict:
    project = _project_for_context(ctx)
    owner_state, _recipes = _configured_paths(project)
    return check_recovered_recipe(project, name, state_dir=owner_state)


@app.tool()
def recipe_diff(name: str, ctx: Context | None = None) -> str:
    project = _project_for_context(ctx)
    owner_state, _recipes = _configured_paths(project)
    return diff_recovered_recipe(project, name, state_dir=owner_state)


@app.tool()
def recipe_render(
    name: str, view: str = "workflow", ctx: Context | None = None
) -> str:
    project = _project_for_context(ctx)
    owner_state, _recipes = _configured_paths(project)
    return observe_authoring_project(
        owner_state,
        project,
        lambda: render_recipe(project, name, view),
    )


@app.tool()
def recipe_estimate(name: str, ctx: Context | None = None) -> dict:
    project = _project_for_context(ctx)
    owner_state, _recipes = _configured_paths(project)
    return observe_authoring_project(
        owner_state,
        project,
        lambda: estimate_recipe(project, name),
    )


@app.tool()
def template_list() -> list[str]:
    from lockstep.templates import list_templates

    return list(list_templates())


@app.tool()
def template_show(
    template: str, name: str, ctx: Context | None = None
) -> dict:
    from lockstep.templates import show_template

    del ctx
    return show_template(template, name).to_dict()


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
    records = _projection_for(project_root).list_runs(str(project_root))
    if active_only:
        records = [item for item in records if item["status"] in {"starting", "awaiting", "running"}]
    return records


@app.tool()
def run_trace(run_id: str, ctx: Context | None = None) -> str:
    project = _project_for_context(ctx)
    return _projection_for(project).run_trace(run_id, str(project))
