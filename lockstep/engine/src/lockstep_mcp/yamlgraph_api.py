"""ALL yamlgraph (0.5.18) + langgraph (1.2.10) knowledge isolated here.

Nothing outside this module imports yamlgraph or langgraph directly (Task 1
Global Constraint). Verified by reading the installed package source
(no bundled reference/*.md docs shipped in this wheel — RECORD has none)
plus `uv run yamlgraph graph <verb> --help`. Every deviation from the plan's
assumed dialect/API, found while writing the spike, is recorded below.

Probe-by-probe results (plan Task 1, Step 3):

- **Brief-lands-in-state** (the load-bearing one): ASSUMPTION HELD. No
  fallback needed. `yamlgraph.node_factory.control_nodes.create_interrupt_node`
  splits every `type: interrupt` node into two LangGraph nodes,
  `<name>_prepare` -> `<name>`; `prepare_fn` commits
  `{state_key: payload, "current_step": name}` to state *before*
  `interrupt()` fires (FR-060 in their source), so `state[state_key]`
  (`brief` here) is readable in the `invoke()`/`resume()` result dict even
  while parked on the interrupt. `resume_key`'s value is whatever object is
  passed to `Command(resume=...)`, written back into
  `{resume_key: response}` by `interrupt_fn` on the next tick.

- **loop_limits / loop_exits / resume_key existence**: all three EXIST, but
  with a dialect trap the plan's literal fixture text (`loop_limits:
  {step_one: 3}`) falls into: `loop_limits`/`loop_exits` are graph-level
  dicts keyed by node name, but `check_loop_limit` is only consulted by the
  node-factory closures for `llm`, `tool`, `python` and `passthrough` nodes
  (`node_compiler.py` enriches every node's config with `loop_limit` from
  `config.loop_limits[node_name]`, but `create_interrupt_node` never reads
  it). Putting the limit on the *interrupt* node name is a silent no-op —
  the counter must be keyed on the node that actually re-executes each
  iteration, here the `python` validator node (`validate_one`), not the
  interrupt it loops back to (`step_one`). Fixture uses
  `loop_limits: {validate_one: 2}`.
  Value semantics also differ from the "3 tries" reading: the guard
  (`check_loop_limit(name, limit, current_count)` in `error_handlers.py`)
  runs *before* the node executes and increments, as `current_count >=
  limit`; count starts at 0. So `loop_limits: {validate_one: N}` allows
  exactly N real executions and blocks (sets `_loop_limit_reached`,
  routes via `loop_exits`) starting on the would-be (N+1)th. To make "3
  consecutive fail-resumes trigger escalate on the 3rd" hold (the spike
  tests below), the fixture sets N=2, not the plan's literal 3.

- **Conditional-edge dialect**: the plan's pre-decided fallback IS the only
  dialect that exists for this shape. `graph_schema.EdgeConfig` is
  `{from, to, condition}` — one condition string per edge, evaluated by
  `yamlgraph.utils.conditions.evaluate_condition` (a small safe grammar:
  `left OP literal-or-state-path`, `OP` in `< > <= >= == !=`, `and`/`or`
  compound, quoted strings are literals). `type: conditional` + a
  `conditions:` list is a *different* edge shape (`EdgeShape.
  ROUTER_CONDITIONAL`, list `to:` + router-style routing) not needed for a
  single validator's pass/fail fan-out. Frozen: `{from, to, condition:
  "verdict_status == 'pass'"}` style, one edge per branch.

- **CLI validate/lint existence**: both `yamlgraph graph validate` and
  `yamlgraph graph lint` EXIST as CLI subcommands, but their Python entry
  points (`cmd_graph_validate`/`cmd_graph_lint` in
  `yamlgraph/cli/graph_validate.py`) are `argparse`-driven, `print()` +
  `sys.exit()` — not usable as a clean `(bool, str)`-returning library
  call without capturing stdout and intercepting SystemExit. Engaged the
  plan's pre-decided fallback: `cli_validate()` here does
  `load_graph_config()` + `compile_graph()` under try/except — compiling
  under Pydantic schema validation (`GraphConfigSchema`, via
  `utils.validators.validate_config`) plus full node/edge wiring *is* the
  validation (it raises on the same malformed-graph shapes the CLI command
  reports, e.g. unknown node type, dangling edge target, router pointing
  nowhere).

- **Checkpointer-vs-CLI conflict**: not applicable — never triggered.
  `compile_graph()` in `yamlgraph.compile.graph_loader` returns a bare
  (uncompiled) `StateGraph`; the *engine* (this module) calls
  `.compile(checkpointer=...)` itself, exactly mirroring the pattern
  `yamlgraph/cli/graph_commands.py` uses for `graph run`. `cli_validate()`
  above never calls `.compile()` at all, so there is no live checkpointer
  to conflict with a recipe-level `checkpointer:` block in the first
  place — `profile_check.py` (Task 3) still forbids that block on other
  grounds (decision 8: only the engine controls persistence).

- **Route log probe**: ASSUMPTION HELD, not wired in this task (the frozen
  `compile_recipe`/`start`/`resume` signatures carry no route-log
  parameter — that lands with Task 5/6, which own `route_log_path`).
  `YAMLGRAPH_ROUTE_LOG=<path>` (env var, read fresh on every routing
  decision by `yamlgraph.utils.route_log.emit_route`) appends one JSON
  line per taken edge, tagged with the LangGraph `configurable.thread_id`
  set via `route_thread_id_from_config()` around `invoke()`. Confirmed the
  exact caveat the plan (Task 6) already anticipated: `_ensure_file_sink()`
  *adds* a new `logging.FileHandler` per distinct env-var path seen in the
  process and never removes earlier ones, so pointing the env var at a
  different path per run mid-process does not isolate runs — every
  handler ever attached keeps receiving every line. Per-run isolation (if
  wanted) has to come from a separate process per run or from the
  `thread_id` field already embedded in each line, not from swapping the
  env var.

- **Unknown-top-level-key tolerance**: ASSUMPTION HELD.
  `GraphConfigSchema.model_config = {"extra": "allow"}` (graph_schema.py) —
  an unrecognized top-level key like `baseline_globs:` passes Pydantic
  validation untouched; `GraphConfig.__init__` (graph_loader.py) reads only
  the keys it knows via `config.get(...)` and ignores the rest. No fallback
  needed; Task 8's `baseline_globs:` can live at the top level as the plan
  assumes.

- **Subagent PreToolUse inheritance probe**: PROBE DEFERRED — no live
  Claude Code session available in this execution context to configure a
  scratch-project PreToolUse deny hook and observe subagent behavior.
  Per the plan's own instruction for this case: Task 7 must keep `Task` in
  the PreToolUse deny matcher set (the conservative default) until this is
  verified live.

Two further findings, discovered only by running the loop/escalate spike
tests (not named probes in the plan, but load-bearing for decision 9 —
"loop_exits routes to the escalate interrupt" — and worth flagging loudly
for Task 3's profile and Task 8's example recipe, which both lean on the
same escalate-marker mechanism):

- **`loop_exits` targeting an interrupt node directly is broken.** Normal
  authored edges (`edges:` list) get their `to:` redirected from
  `<interrupt>` to `<interrupt>_prepare` in `_process_edge` (so the
  interrupt's `prepare_fn` — the thing that actually commits the message
  to `state[state_key]` — runs first). `loop_exits` targets are NOT part
  of the `edges:` list; they are consulted only inside
  `_add_conditional_edges`/`build_expression_route_mapping`/
  `make_expr_router_fn`, none of which apply that redirect. A direct
  `loop_exits: {validate_one: escalate}` therefore jumps straight to
  `escalate`'s bare `interrupt_fn`, skipping `escalate_prepare` — the
  interrupt fires with whatever `state["brief"]` happened to hold already
  (the stale work-step brief), never the `{step: escalate}` marker.
  Confirmed empirically: `_loop_limit_reached` fires correctly, but
  `adv.brief.step` came back `"one"`, not `"escalate"`.
  Fixture workaround (frozen for later tasks): route `loop_exits` to a
  plain `type: passthrough` gate node (`escalate_gate`, no-op `output:
  {}`) instead of the interrupt directly, then a normal authored edge
  `escalate_gate -> escalate`. That edge IS in the `edges:` list, so it
  gets the standard redirect to `escalate_prepare` and the marker commits
  correctly. **Task 3's profile check for "every `loop_exits` target
  carries the `{step: escalate}` marker" (decision 9) must be adjusted**:
  the `loop_exits` target itself will legitimately be a passthrough gate,
  not the marked interrupt — the rule needs to follow the gate's own
  single outgoing plain edge (or, more simply, require the gate's edge
  target to carry the marker) rather than checking the `loop_exits` value
  directly.
- **Interrupt `idempotent: true` (the default) is dangerous across shared
  `state_key`s.** `prepare_fn` skips recomputing `message` whenever
  `state[state_key]` is already truthy (`if idempotent and existing_payload
  is not None: payload = existing_payload`) — regardless of whether
  `message` is static or templated. The architecture (decision 3) reuses
  one `state_key: brief` across every step, work or escalate, so the park
  BEFORE the CURRENT one always leaves `brief` non-empty; a second
  interrupt sharing that key inherits the FIRST interrupt's stale payload
  unless it opts out. Fixture sets `idempotent: false` on `escalate`
  (and any future work step reusing `state_key: brief` after a prior
  step must do the same — Task 3's profile should require `idempotent:
  false` on every interrupt node, not just escalate, since `skip_if_exists`
  idempotence is never wanted for a fresh step's brief).

Other incidental findings, not separate plan probes but load-bearing for
this module's implementation:

- `type: python` nodes take `tool: <name>` (or `function:`), resolved
  against the graph's `tools:` registry (`parse_python_tools` only reads
  entries with `type: python`) — never `module:`/`function:` directly on
  the node itself. The `tools:` entry (not the node) carries
  `module`/`function`. Matches the plan's `tools: {run_checks: {type:
  python, module: ..., function: ...}}` + node `tool: run_checks` shape
  exactly.
- If the tool function returns a `dict`, `create_python_node`'s wrapper
  merges it directly into the state update (plus `current_step`/
  `_loop_counts`) — the node's own `state_key` is only used as a fallback
  for a non-dict return. `validators.run_checks` returning
  `{"verdict_status": ..., "verdict_reasons": [...]}` therefore lands
  those as flat top-level state keys with no node-name nesting, exactly
  what decision 10 (flat verdict shape) and the `verdict_status == 'pass'`
  edge conditions need — no `state_key` required on `validate_one`.
- `checks`/`evidence_schema` are NOT `NodeConfig` fields (`NodeConfig.
  model_config = {"extra": "forbid"}`) — they only exist because decision 2
  nests them inside the free-form `message:` dict, which yamlgraph never
  schema-checks (`message: str | dict[str, Any]`).
- `langgraph.checkpoint.sqlite.SqliteSaver(conn)` does **not** create its
  tables on construction — confirmed empirically (`sqlite_master` empty
  right after `SqliteSaver(conn)`, populated only after `.setup()`).
  yamlgraph's own `storage/checkpointer_factory.get_checkpointer()` never
  calls `.setup()` for the `sqlite` type, which looks like a latent gap in
  yamlgraph itself; `compile_recipe()` below calls it explicitly so a
  fresh db file works on the first `start()`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from yamlgraph.compile.graph_loader import compile_graph, load_graph_config
from yamlgraph.mermaid_export import parse_route_lines, render_mermaid, render_overlay


@dataclass
class StepBrief:
    step: str
    task: str = ""
    exit_criterion: str = ""
    evidence_schema: dict | None = None
    checks: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class Advance:
    done: bool
    brief: StepBrief | None
    state: dict


def _parse_brief(raw: dict) -> StepBrief:
    return StepBrief(
        step=raw.get("step", ""),
        task=raw.get("task", ""),
        exit_criterion=raw.get("exit_criterion", ""),
        evidence_schema=raw.get("evidence_schema"),
        checks=list(raw.get("checks") or []),
        raw=raw,
    )


def _advance_from_result(result: dict) -> Advance:
    done = "__interrupt__" not in result
    raw_brief = None if done else result.get("brief")
    brief = _parse_brief(raw_brief) if raw_brief is not None else None
    return Advance(done=done, brief=brief, state=dict(result))


def compile_recipe(recipe_path: Path, db_path: Path | None) -> object:
    """Load + compile a recipe YAML, injecting the checkpointer ourselves
    (decision 8: the engine, never a recipe `checkpointer:` block, owns
    persistence). `db_path is None` -> in-memory (MemorySaver); otherwise a
    sqlite file survivable across fresh `compile_recipe()` calls (fresh
    process simulation)."""
    config = load_graph_config(Path(recipe_path))
    graph = compile_graph(config)
    if db_path is None:
        checkpointer = MemorySaver()
    else:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        checkpointer.setup()
    return graph.compile(checkpointer=checkpointer)


def start(app, vars: dict, thread_id: str) -> Advance:
    config = {"configurable": {"thread_id": thread_id}}
    result = app.invoke(dict(vars), config=config)
    return _advance_from_result(result)


def resume(app, payload: dict, thread_id: str) -> Advance:
    config = {"configurable": {"thread_id": thread_id}}
    result = app.invoke(Command(resume=payload), config=config)
    return _advance_from_result(result)


def cli_validate(recipe_path: Path) -> tuple[bool, str]:
    """Fallback engaged (see module docstring): compile-under-try/except
    IS the validation — the CLI's own `graph validate`/`lint` commands are
    print+sys.exit, not usable as a library call."""
    try:
        config = load_graph_config(Path(recipe_path))
        compile_graph(config)
    except Exception as e:  # noqa: BLE001 - validation surface, not a bug
        return False, str(e)
    return True, "ok"


def cli_mermaid(recipe_path: Path, overlay: Path | None) -> str:
    with open(Path(recipe_path)) as f:
        config = yaml.safe_load(f)
    if overlay is not None:
        lines = Path(overlay).read_text().splitlines()
        route = parse_route_lines(lines)
        return render_overlay(config, route)
    return render_mermaid(config)
