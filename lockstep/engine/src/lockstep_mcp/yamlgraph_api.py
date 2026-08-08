"""ALL yamlgraph (0.5.18) + langgraph (1.2.10) knowledge isolated here.

Nothing outside this module imports yamlgraph or langgraph directly. The
dialect below is what the installed packages actually do (read off their
source, plus `yamlgraph graph <verb> --help`); several points are traps
that the recipe profile (`profile_check.py`) exists to enforce against.

Interrupt mechanics
-------------------
`yamlgraph.node_factory.control_nodes.create_interrupt_node` splits every
`type: interrupt` node into two LangGraph nodes, `<name>_prepare` ->
`<name>`. `prepare_fn` commits `{state_key: payload, "current_step": name}`
to state *before* `interrupt()` fires, so `state[state_key]` (`brief` here)
is readable in the `invoke()`/`resume()` result dict while parked.
`resume_key`'s value is whatever object is passed to `Command(resume=...)`,
written back into `{resume_key: response}` by `interrupt_fn` on the next
tick.

`idempotent: true` (the default) is dangerous across a shared `state_key`:
`prepare_fn` skips recomputing `message` whenever `state[state_key]` is
already truthy (`if idempotent and existing_payload is not None`),
templated or not. Recipes reuse one `state_key: brief` across every step,
so the previous park always leaves `brief` non-empty and the next
interrupt would inherit the stale payload. Every interrupt therefore
declares `idempotent: false`.

Loop caps
---------
`loop_limits`/`loop_exits` are graph-level dicts keyed by node name, but
`check_loop_limit` is consulted only by the node-factory closures for
`llm`, `tool`, `python` and `passthrough` nodes — `create_interrupt_node`
never reads it. Keying a limit on the INTERRUPT name is a silent no-op:
the counter belongs on the node that re-executes each iteration, i.e. the
`python` validator.

The guard (`check_loop_limit(name, limit, current_count)` in
`error_handlers.py`) runs BEFORE the node executes and increments, as
`current_count >= limit`, counting from 0. So `loop_limits: {v: N}` allows
exactly N real executions and blocks — setting `_loop_limit_reached` and
routing via `loop_exits` — on the would-be (N+1)th. "3 consecutive
fail-resumes escalate on the 3rd" is therefore written `N = 2`.

`loop_exits` may not target an interrupt directly. Authored edges (the
`edges:` list) get their `to:` redirected from `<interrupt>` to
`<interrupt>_prepare` in `_process_edge`, so the interrupt's `prepare_fn`
runs first; `loop_exits` targets are consulted only inside
`_add_conditional_edges`/`build_expression_route_mapping`/
`make_expr_router_fn`, none of which apply that redirect. A direct
`loop_exits: {validate_one: escalate}` jumps to `escalate`'s bare
`interrupt_fn`, skipping `escalate_prepare`, and parks with whatever
`state["brief"]` already held — the stale work-step brief, never the
`{step: escalate}` marker. Recipes therefore route `loop_exits` to a plain
`type: passthrough` gate, then an ordinary authored edge from that gate to
the marked escalate interrupt.

Edges
-----
`graph_schema.EdgeConfig` is `{from, to, condition}` — one condition
string per edge, evaluated by `yamlgraph.utils.conditions.evaluate_condition`
(a small safe grammar: `left OP literal-or-state-path`, `OP` in
`< > <= >= == !=`, `and`/`or` compound, quoted strings are literals).
`type: conditional` + a `conditions:` list is a different edge shape
(`EdgeShape.ROUTER_CONDITIONAL`) and is not used here.

Validation
----------
`yamlgraph graph validate`/`lint` exist as CLI subcommands, but their
Python entry points (`cmd_graph_validate`/`cmd_graph_lint`) are
argparse-driven `print()` + `sys.exit()` — not usable as a library call
without capturing stdout and intercepting SystemExit. `cli_validate()`
below instead does `load_graph_config()` + `compile_graph()` under
try/except: compiling under Pydantic schema validation
(`GraphConfigSchema`, via `utils.validators.validate_config`) plus full
node/edge wiring IS the validation — it raises on the same malformed-graph
shapes the CLI reports (unknown node type, dangling edge target, router
pointing nowhere).

`compile_graph()` returns a bare (uncompiled) `StateGraph`; this module
calls `.compile(checkpointer=...)` itself. `cli_validate()` never compiles,
so a recipe-level `checkpointer:` block could not conflict there — it is
forbidden by the profile for a different reason: the engine owns
persistence.

Python nodes
------------
`type: python` nodes take `tool: <name>` (or `function:`), resolved against
the graph's `tools:` registry (`parse_python_tools` reads only entries with
`type: python`) — never `module:`/`function:` on the node itself; the
`tools:` entry carries those.

If the tool function returns a `dict`, `create_python_node`'s wrapper
merges it directly into the state update (plus `current_step`/
`_loop_counts`); the node's `state_key` is only a fallback for a non-dict
return. `validators.run_checks` returning `{"verdict_status": ...,
"verdict_reasons": [...]}` therefore lands those as FLAT top-level state
keys with no node-name nesting — which is what the `verdict_status ==
'pass'` edge conditions read, so validator nodes need no `state_key`.

`checks`/`evidence_schema` are not `NodeConfig` fields (`NodeConfig.
model_config = {"extra": "forbid"}`); they live inside the free-form
`message:` dict, which yamlgraph never schema-checks
(`message: str | dict[str, Any]`).

Top-level keys
--------------
`GraphConfigSchema.model_config = {"extra": "allow"}`, and
`GraphConfig.__init__` reads only the keys it knows via `config.get(...)`.
An unrecognized top-level key like `baseline_globs:` passes validation
untouched and is ours to read.

Checkpointer
------------
`langgraph.checkpoint.sqlite.SqliteSaver(conn)` does NOT create its tables
on construction (`sqlite_master` is empty until `.setup()`), and
yamlgraph's `storage/checkpointer_factory.get_checkpointer()` never calls
`.setup()` for the `sqlite` type. `compile_recipe()` below calls it
explicitly so a fresh db file works on the first `start()`.

Route log
---------
`YAMLGRAPH_ROUTE_LOG=<path>` (env var, read fresh on every routing decision
by `yamlgraph.utils.route_log.emit_route`) appends one JSON line per taken
edge, tagged with the LangGraph `configurable.thread_id`. It is not wired
into `compile_recipe`/`start`/`resume`, and cannot be used for per-run
files: `_ensure_file_sink()` ADDS a `logging.FileHandler` per distinct path
seen in the process and never removes earlier ones, so every handler ever
attached keeps receiving every line. Per-run isolation has to come from a
separate process or from the `thread_id` field already in each line.
`engine.py` writes its own transition JSONL instead.

`peek()`
--------
Reading a parked run's checkpoint state must not resume it (resume is
mutating: it requires a verdict payload and advances the graph). The
compiled `app` is a real `CompiledStateGraph` whose public `get_state
(config)` is the read-only counterpart — `.next` is a non-empty tuple while
parked and empty once the graph reaches END, and `.values` holds the same
`brief`/`evidence` dict `start`/`resume` expose. Trapping that call here
keeps all yamlgraph/langgraph knowledge in this one module; `peek()` reuses
the same `Advance`/`_parse_brief` machinery, fed from `get_state()`.
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
    (the engine, never a recipe `checkpointer:` block, owns persistence). `db_path is None` -> in-memory (MemorySaver); otherwise a
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


def peek(app, thread_id: str) -> Advance:
    """Read-only counterpart to `start`/`resume` (see the module
    docstring): current checkpoint state for `thread_id` via `get_state`,
    without resuming. `done=True` once `.next` is empty AND the checkpoint
    holds state; otherwise the parked step's brief, exactly like
    `start`/`resume`.

    The `values` half is load-bearing: LangGraph answers with an EMPTY
    snapshot (`values={}`, `next=()`) for a thread_id it has never seen, so
    "this run is absent from this checkpointer" — a lost or freshly-created
    sqlite file, a restarted memory-only engine — is otherwise
    indistinguishable from "reached END", and reconcile would flip a run
    whose steps never ran to a terminal `done`. A graph that really
    finished always left values behind."""
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = app.get_state(config)
    values = dict(snapshot.values or {})
    done = not snapshot.next and bool(values)
    raw_brief = None if done else values.get("brief")
    brief = _parse_brief(raw_brief) if raw_brief is not None else None
    return Advance(done=done, brief=brief, state=values)


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
