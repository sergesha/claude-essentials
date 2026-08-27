"""ALL yamlgraph (0.5.22 + reviewed source patch) knowledge is isolated here.

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
`.setup()` for the `sqlite` type. The native adapter calls it explicitly so
a fresh database works on the first invocation.

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
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from pathlib import Path
from typing import Any, Self, TypedDict

import yaml
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from yamlgraph.compile.graph_loader import compile_graph, load_graph_config
from yamlgraph.compile.node_otel import _maybe_wrap_otel
from yamlgraph.mermaid_export import render_mermaid
from yamlgraph.node_factory.subgraph_nodes import _build_child_config
from yamlgraph.node_timeout import _maybe_wrap_timeout

from lockstep.recipe.authority import (
    AuthorizedMaterialization,
    StrictRecipeIngress,
)
from lockstep.recipe.profile import CompilerProvenance, check_recipe_full
from lockstep.runtime.native_models import (
    NativeCoordinate,
    NativeEvent,
    NativeHistoryLimitExceeded,
    NativeInterrupt,
    NativeInterruptOccurrence,
    NativeSnapshot,
)
from lockstep.runtime.owner_state import seal_owner_file, sqlite_readonly_uri


def _neutral(value: Any) -> Any:
    """Convert native/package objects into stable standard-library values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _neutral(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_neutral(item) for item in value)
    if isinstance(value, list):
        return [_neutral(item) for item in value]
    if isinstance(value, set):
        return tuple(sorted((_neutral(item) for item in value), key=repr))
    if is_dataclass(value):
        return {
            item.name: _neutral(getattr(value, item.name))
            for item in dataclass_fields(value)
        }
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _neutral(model_dump())
    return str(value)


def _snapshot_config(snapshot: Any) -> tuple[str, str, str]:
    configurable = (getattr(snapshot, "config", None) or {}).get("configurable", {})
    return (
        str(configurable.get("thread_id") or ""),
        str(configurable.get("checkpoint_id") or ""),
        str(configurable.get("checkpoint_ns") or ""),
    )


def _checkpoint_ancestors(snapshot: Any) -> tuple[tuple[str, str], ...]:
    configurable = (getattr(snapshot, "config", None) or {}).get("configurable", {})
    ancestors = dict(configurable.get("checkpoint_map") or {})
    _thread_id, checkpoint_id, checkpoint_ns = _snapshot_config(snapshot)
    if checkpoint_id:
        ancestors[checkpoint_ns] = checkpoint_id
    return tuple(sorted((str(key), str(value)) for key, value in ancestors.items()))


def _config_checkpoint(config: Any) -> tuple[str, str, str]:
    configurable = (config or {}).get("configurable", {})
    return (
        str(configurable.get("thread_id") or ""),
        str(configurable.get("checkpoint_id") or ""),
        str(configurable.get("checkpoint_ns") or ""),
    )


def _completed_subgraph_snapshots(
    snapshot: Any, parent: tuple[str, str, str]
) -> tuple[Any, ...]:
    parent_thread, parent_checkpoint_id, parent_checkpoint_ns = parent
    completed = []
    for task in getattr(snapshot, "tasks", ()) or ():
        child = getattr(task, "state", None)
        if (
            getattr(task, "result", None) is None
            or not hasattr(child, "tasks")
            or not hasattr(child, "config")
        ):
            continue
        child_thread, _child_checkpoint_id, _child_checkpoint_ns = (
            _snapshot_config(child)
        )
        configurable = (getattr(child, "config", None) or {}).get(
            "configurable", {}
        )
        checkpoint_map = dict(configurable.get("checkpoint_map") or {})
        if (
            child_thread == parent_thread
            and str(checkpoint_map.get(parent_checkpoint_ns) or "")
            == parent_checkpoint_id
        ):
            completed.append(child)
    return tuple(completed)


def _checkpoint_path_contains(
    app: Any,
    *,
    thread_id: str,
    ancestor_checkpoint_ns: str,
    ancestor_checkpoint_id: str,
    descendant_checkpoint_ns: str,
    descendant_checkpoint_id: str,
    snapshot_limit: int,
) -> bool:
    pending: list[Any] = [
        {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": descendant_checkpoint_ns,
                "checkpoint_id": descendant_checkpoint_id,
            }
        }
    ]
    visited: set[tuple[str, str, str]] = set()
    examined = 0
    while pending:
        candidate = pending.pop()
        expected = (
            _config_checkpoint(candidate)
            if isinstance(candidate, dict)
            else _snapshot_config(candidate)
        )
        if expected in visited:
            continue
        if examined >= snapshot_limit:
            raise NativeHistoryLimitExceeded(
                "native ancestry traversal exceeds validation limit"
            )
        snapshot = (
            app.get_state(candidate, subgraphs=True)
            if isinstance(candidate, dict)
            else candidate
        )
        current = _snapshot_config(snapshot)
        examined += 1
        if current != expected or current[0] != thread_id or not current[1]:
            return False
        visited.add(current)
        _current_thread, checkpoint_id, checkpoint_ns = current
        if (
            checkpoint_ns == ancestor_checkpoint_ns
            and checkpoint_id == ancestor_checkpoint_id
        ):
            return True
        parent = getattr(snapshot, "parent_config", None)
        if isinstance(parent, dict):
            pending.append(parent)
        pending.extend(_completed_subgraph_snapshots(snapshot, current))
    return False


def _pending_interrupts(snapshot: Any) -> tuple[NativeInterrupt, ...]:
    pending: list[NativeInterrupt] = []
    seen: set[str] = set()

    def visit(current: Any) -> None:
        thread_id, checkpoint_id, checkpoint_ns = _snapshot_config(current)
        for task in getattr(current, "tasks", ()) or ():
            child = getattr(task, "state", None)
            if hasattr(child, "tasks") and hasattr(child, "config"):
                visit(child)
            # LangGraph retains the original Interrupt tuple as task metadata
            # after a partial batch resume.  A non-None result means that task
            # has completed and its interrupt is no longer pending.
            if getattr(task, "result", None) is not None:
                continue
            for interrupt in getattr(task, "interrupts", ()) or ():
                interrupt_id = str(getattr(interrupt, "id", ""))
                if not interrupt_id or interrupt_id in seen:
                    continue
                seen.add(interrupt_id)
                pending.append(
                    NativeInterrupt(
                        coordinate=NativeCoordinate(
                            thread_id=thread_id,
                            checkpoint_id=checkpoint_id,
                            checkpoint_ns=checkpoint_ns,
                            task_id=str(getattr(task, "id", "")),
                            interrupt_id=interrupt_id,
                        ),
                        value=_neutral(getattr(interrupt, "value", None)),
                        ancestor_checkpoints=_checkpoint_ancestors(current),
                        state_values=dict(
                            _neutral(getattr(current, "values", {}) or {})
                        ),
                    )
                )

    visit(snapshot)
    return tuple(pending)


def _interrupt_occurrences(snapshot: Any) -> tuple[NativeInterruptOccurrence, ...]:
    """Project exact tasks from one namespace-scoped public history snapshot."""

    thread_id, checkpoint_id, checkpoint_ns = _snapshot_config(snapshot)
    return tuple(
        NativeInterruptOccurrence(
            coordinate=NativeCoordinate(
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
                checkpoint_ns=checkpoint_ns,
                task_id=str(getattr(task, "id", "")),
                interrupt_id=str(getattr(interrupt, "id", "")),
            ),
            value=_neutral(getattr(interrupt, "value", None)),
        )
        for task in (getattr(snapshot, "tasks", ()) or ())
        for interrupt in (getattr(task, "interrupts", ()) or ())
        if str(getattr(task, "id", "")) and str(getattr(interrupt, "id", ""))
    )


def _to_native_snapshot(snapshot: Any) -> NativeSnapshot:
    _thread_id, checkpoint_id, checkpoint_ns = _snapshot_config(snapshot)
    return NativeSnapshot(
        values=dict(_neutral(getattr(snapshot, "values", {}) or {})),
        pending=_pending_interrupts(snapshot),
        next=tuple(str(item) for item in (getattr(snapshot, "next", ()) or ())),
        checkpoint_id=checkpoint_id,
        checkpoint_ns=checkpoint_ns,
        metadata=dict(_neutral(getattr(snapshot, "metadata", {}) or {})),
        created_at=getattr(snapshot, "created_at", None),
        task_errors=tuple(
            str(error)
            for task in (getattr(snapshot, "tasks", ()) or ())
            if (error := getattr(task, "error", None)) is not None
        ),
    )


class NativeApp:
    """Narrow facade over one compiled yamlgraph/LangGraph application."""

    def __init__(
        self,
        app: Any,
        connection: sqlite3.Connection | None = None,
        database_path: Path | None = None,
    ) -> None:
        self._app = app
        self._connection = connection
        self._database_path = database_path
        self._closed = False
        self._seal_sqlite_files()

    def _seal_sqlite_files(self) -> None:
        if self._database_path is None:
            return
        for path in (
            self._database_path,
            Path(f"{self._database_path}-journal"),
            Path(f"{self._database_path}-wal"),
            Path(f"{self._database_path}-shm"),
        ):
            if path.exists():
                seal_owner_file(path, writable=True)

    @staticmethod
    def _config(thread_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": thread_id}}

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("NativeApp is closed")

    def invoke(self, values: dict, *, thread_id: str) -> NativeSnapshot:
        self._ensure_open()
        config = self._config(thread_id)
        try:
            self._app.invoke(dict(values), config=config)
            return _to_native_snapshot(self._app.get_state(config, subgraphs=True))
        finally:
            self._seal_sqlite_files()

    async def ainvoke(self, values: dict, *, thread_id: str) -> NativeSnapshot:
        self._ensure_open()
        config = self._config(thread_id)
        try:
            await self._app.ainvoke(dict(values), config=config)
            return _to_native_snapshot(self._app.get_state(config, subgraphs=True))
        finally:
            self._seal_sqlite_files()

    def resume(
        self,
        *,
        thread_id: str,
        results_by_interrupt_id: Mapping[str, Any],
    ) -> NativeSnapshot:
        self._ensure_open()
        if not results_by_interrupt_id:
            raise ValueError("at least one interrupt result is required")
        config = self._config(thread_id)
        try:
            self._app.invoke(Command(resume=dict(results_by_interrupt_id)), config=config)
            return _to_native_snapshot(self._app.get_state(config, subgraphs=True))
        finally:
            self._seal_sqlite_files()

    async def aresume(
        self,
        *,
        thread_id: str,
        results_by_interrupt_id: Mapping[str, Any],
    ) -> NativeSnapshot:
        self._ensure_open()
        if not results_by_interrupt_id:
            raise ValueError("at least one interrupt result is required")
        config = self._config(thread_id)
        try:
            await self._app.ainvoke(
                Command(resume=dict(results_by_interrupt_id)), config=config
            )
            snapshot = await self._app.aget_state(config, subgraphs=True)
            return _to_native_snapshot(snapshot)
        finally:
            self._seal_sqlite_files()

    def stream(
        self,
        values_or_command: object,
        *,
        thread_id: str,
    ) -> Iterable[NativeEvent]:
        self._ensure_open()
        try:
            for chunk in self._app.stream(
                values_or_command,
                config=self._config(thread_id),
                stream_mode="updates",
                subgraphs=True,
            ):
                yield NativeEvent(mode="updates", data=_neutral(chunk))
        finally:
            self._seal_sqlite_files()

    def snapshot(self, *, thread_id: str, subgraphs: bool = False) -> NativeSnapshot:
        self._ensure_open()
        try:
            snapshot = self._app.get_state(self._config(thread_id), subgraphs=subgraphs)
            return _to_native_snapshot(snapshot)
        finally:
            self._seal_sqlite_files()

    def history(self, *, thread_id: str) -> Iterable[NativeSnapshot]:
        self._ensure_open()
        try:
            for snapshot in self._app.get_state_history(self._config(thread_id)):
                yield _to_native_snapshot(snapshot)
        finally:
            self._seal_sqlite_files()

    def interrupt_history(
        self, *, thread_id: str, checkpoint_ns: str, snapshot_limit: int
    ) -> Iterable[NativeInterruptOccurrence]:
        """Read exact occurrences from the public history of one namespace."""

        self._ensure_open()
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
            }
        }
        try:
            for index, snapshot in enumerate(
                self._app.get_state_history(config, limit=snapshot_limit + 1)
            ):
                if index >= snapshot_limit:
                    raise NativeHistoryLimitExceeded(
                        "native lineage snapshot scan exceeds validation limit"
                    )
                yield from _interrupt_occurrences(snapshot)
        finally:
            self._seal_sqlite_files()

    def checkpoint_is_ancestor(
        self,
        *,
        thread_id: str,
        ancestor_checkpoint_ns: str,
        ancestor_checkpoint_id: str,
        descendant_checkpoint_ns: str,
        descendant_checkpoint_id: str,
        snapshot_limit: int,
    ) -> bool:
        """Traverse bounded public parent and completed-subgraph checkpoints."""

        self._ensure_open()
        try:
            return _checkpoint_path_contains(
                self._app,
                thread_id=thread_id,
                ancestor_checkpoint_ns=ancestor_checkpoint_ns,
                ancestor_checkpoint_id=ancestor_checkpoint_id,
                descendant_checkpoint_ns=descendant_checkpoint_ns,
                descendant_checkpoint_id=descendant_checkpoint_id,
                snapshot_limit=snapshot_limit,
            )
        finally:
            self._seal_sqlite_files()

    def close(self) -> None:
        if not self._closed and self._connection is not None:
            self._connection.close()
            self._seal_sqlite_files()
        self._closed = True

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _open_native_path(recipe_path: Path, db_path: Path | None = None) -> NativeApp:
    """Compile one path already proven to be an immutable authority artifact."""
    config = load_graph_config(recipe_path)
    graph = compile_graph(config)
    connection: sqlite3.Connection | None = None
    if db_path is None:
        checkpointer = MemorySaver()
    else:
        connection = sqlite3.connect(str(db_path), check_same_thread=False)
        checkpointer = SqliteSaver(connection)
        checkpointer.setup()
    try:
        return NativeApp(
            graph.compile(checkpointer=checkpointer),
            connection,
            db_path,
        )
    except BaseException:
        if connection is not None:
            connection.close()
        raise


def open_native_app(
    recipe: AuthorizedMaterialization,
    db_path: Path | None = None,
) -> NativeApp:
    """Compile only an authorized immutable canonical materialization."""
    if not isinstance(recipe, AuthorizedMaterialization):
        raise TypeError("open_native_app requires an AuthorizedMaterialization")
    return _open_native_path(recipe.source_path, db_path)


def open_native_app_readonly(
    recipe: AuthorizedMaterialization,
    db_path: Path,
) -> NativeApp:
    """Open an existing saver for hook/doctor projection without setup writes."""
    if not isinstance(recipe, AuthorizedMaterialization):
        raise TypeError("open_native_app_readonly requires an AuthorizedMaterialization")
    config = load_graph_config(recipe.source_path)
    graph = compile_graph(config)
    database = Path(db_path)
    connection = sqlite3.connect(
        sqlite_readonly_uri(database), uri=True, check_same_thread=False
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('checkpoints', 'writes')"
            )
        }
        if tables != {"checkpoints", "writes"}:
            raise RuntimeError("native checkpoint schema is missing")
        checkpointer = SqliteSaver(connection)
        # The schema was verified above.  SqliteSaver otherwise calls setup()
        # from its first read and attempts DDL/PRAGMA writes on this read-only
        # connection.
        checkpointer.is_setup = True
        return NativeApp(
            graph.compile(checkpointer=checkpointer),
            connection,
        )
    except BaseException:
        connection.close()
        raise


class _InjectedConfigProbeState(TypedDict, total=False):
    observed_sentinel: str


def _run_wrapped_injected_config_probe(sentinel: str) -> dict[str, Any]:
    """Prove a wrapped graph node receives LangGraph's injected config.

    This deliberately small graph exists only for the native dependency gate.
    The callable observes a value available solely through RunnableConfig, so a
    successful state update cannot be produced by reconstructing checkpoint
    coordinates or by calling the wrappers directly.
    """

    def capture_injected_config(
        _state: _InjectedConfigProbeState,
        config: Mapping[str, Any],
    ) -> _InjectedConfigProbeState:
        configurable = config.get("configurable") or {}
        return {"observed_sentinel": str(configurable["lockstep_probe_sentinel"])}

    node_name = "injected_config_probe"
    wrapped = _maybe_wrap_otel(
        _maybe_wrap_timeout(
            capture_injected_config,
            {"timeout": 1},
            node_name,
        ),
        node_name,
        "python",
    )
    builder = StateGraph(_InjectedConfigProbeState)
    builder.add_node(node_name, wrapped)
    builder.add_edge(START, node_name)
    builder.add_edge(node_name, END)
    result = _neutral(
        builder.compile().invoke(
            {},
            config={
                "configurable": {
                    "thread_id": f"lockstep-config-probe-{sentinel}",
                    "lockstep_probe_sentinel": sentinel,
                }
            },
        )
    )
    if not isinstance(result, dict):
        raise TypeError("native injected-config probe did not return a mapping")
    return result


_PROBE_CHILD = '''
version: "1.0"
name: probe-child
state: {phase: str, answer: str}
nodes:
  prepare: {type: passthrough, output: {phase: waiting}}
  ask:
    type: interrupt
    message: Answer?
    state_key: question
    resume_key: answer
    idempotent: false
  finish: {type: passthrough, output: {phase: complete}}
edges:
  - {from: START, to: prepare}
  - {from: prepare, to: ask}
  - {from: ask, to: finish}
  - {from: finish, to: END}
'''

_PROBE_DIRECT = '''
version: "1.0"
name: probe-direct
state: {phase: str, answer: str}
nodes:
  child: {type: subgraph, graph: child.yaml, mode: direct}
edges:
  - {from: START, to: child}
  - {from: child, to: END}
'''

_PROBE_INVOKE = '''
version: "1.0"
name: probe-invoke
state: {child_phase: str}
nodes:
  child:
    type: subgraph
    graph: child.yaml
    mode: invoke
    input_mapping: {}
    output_mapping: {child_phase: phase}
    interrupt_output_mapping: {child_phase: phase}
edges:
  - {from: START, to: child}
  - {from: child, to: END}
'''


def probe_native_capabilities() -> None:
    """Raise unless the installed yamlgraph satisfies Lockstep's native gate."""

    wrapper_result = _run_wrapped_injected_config_probe("probe-configurable")
    assert wrapper_result == {"observed_sentinel": "probe-configurable"}

    child_config = _build_child_config(
        {
            "configurable": {
                "thread_id": "probe-parent",
                "tenant": "kept",
                "checkpoint_id": "private",
                "checkpoint_ns": "private",
                "checkpoint_map": {"private": "private"},
                "__pregel_send": object(),
            }
        },
        "child",
    )
    assert child_config["configurable"] == {
        "thread_id": "probe-parent:child",
        "tenant": "kept",
    }

    with tempfile.TemporaryDirectory(prefix="lockstep-yamlgraph-probe-") as raw:
        root = Path(raw)
        child = root / "child.yaml"
        direct = root / "direct.yaml"
        invoke = root / "invoke.yaml"
        database = root / "checkpoints.sqlite"
        child.write_text(_PROBE_CHILD)
        direct.write_text(_PROBE_DIRECT)
        invoke.write_text(_PROBE_INVOKE)

        first = _open_native_path(direct, database)
        parked = first.invoke({}, thread_id="probe-direct")
        first.close()
        restarted = _open_native_path(direct, database)
        completed = restarted.resume(
            thread_id="probe-direct",
            results_by_interrupt_id={
                parked.pending[0].coordinate.interrupt_id: "yes"
            },
        )
        restarted.close()
        assert completed.values["answer"] == "yes"
        assert completed.pending == ()

        invoke_app = _open_native_path(invoke)
        parked_a = invoke_app.invoke({}, thread_id="probe-a")
        parked_b = invoke_app.invoke({}, thread_id="probe-b")
        completed_a = invoke_app.resume(
            thread_id="probe-a",
            results_by_interrupt_id={
                parked_a.pending[0].coordinate.interrupt_id: "a"
            },
        )
        completed_b = invoke_app.resume(
            thread_id="probe-b",
            results_by_interrupt_id={
                parked_b.pending[0].coordinate.interrupt_id: "b"
            },
        )
        invoke_app.close()
        assert completed_a.pending == ()
        assert completed_b.pending == ()


def _validate_path(recipe_path: Path) -> tuple[bool, str]:
    """Fallback engaged (see module docstring): compile-under-try/except
    IS the validation — the CLI's own `graph validate`/`lint` commands are
    print+sys.exit, not usable as a library call."""
    try:
        config = load_graph_config(Path(recipe_path))
        compile_graph(config)
    except Exception as e:  # noqa: BLE001 - validation surface, not a bug
        return False, str(e)
    return True, "ok"


def validate_native(recipe: AuthorizedMaterialization) -> tuple[bool, str]:
    """Check the same immutable authority artifact accepted by native start."""
    if not isinstance(recipe, AuthorizedMaterialization):
        raise TypeError("validate_native requires an AuthorizedMaterialization")
    return _validate_path(recipe.source_path)


def validate_compiler_bundle(
    *,
    root_relative_path: str,
    execution_files: Mapping[str, bytes],
    provenance: CompilerProvenance,
) -> tuple[bool, str]:
    """Run the final recursive profile and real yamlgraph compilation gates.

    Compiler output is validated in the exact canonical form later admitted by
    ``StrictRecipeIngress``.  The temporary directory is only a private adapter
    detail needed because yamlgraph resolves direct subgraphs by relative path.
    """
    if not isinstance(provenance, CompilerProvenance):
        raise TypeError("compiler bundle validation requires exact provenance")
    if set(execution_files) != {
        item.relative_path for item in provenance.files
    }:
        return False, "compiler bundle file set does not match provenance"
    with tempfile.TemporaryDirectory(prefix="lockstep-compiler-gate-") as raw:
        root = Path(raw)
        for relative_path, content in execution_files.items():
            target = root / relative_path
            try:
                target.resolve().relative_to(root.resolve())
            except ValueError:
                return False, "compiler bundle contains a non-contained path"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        recipe = root / root_relative_path
        try:
            candidate = StrictRecipeIngress(root).inspect(root_relative_path)
        except ValueError as exc:
            return False, str(exc)
        admitted_files = {item.path: item.bytes for item in candidate.files}
        if admitted_files != dict(execution_files):
            return (
                False,
                "strict ingress canonical file set differs from compiler execution files",
            )
        errors, _warnings = check_recipe_full(recipe, provenance)
        if errors:
            return False, "; ".join(errors)
        return _validate_path(recipe)


def render_native(recipe: AuthorizedMaterialization) -> str:
    """Render only the immutable authority artifact accepted by native start."""
    if not isinstance(recipe, AuthorizedMaterialization):
        raise TypeError("render_native requires an AuthorizedMaterialization")
    with open(recipe.source_path) as f:
        config = yaml.safe_load(f)
    return render_mermaid(config)
