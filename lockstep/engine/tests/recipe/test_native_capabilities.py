from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

import lockstep.recipe.yamlgraph_adapter as yg
from lockstep.recipe.authority import RecipeAuthorityPolicy, StrictRecipeIngress
from lockstep.runtime.recipe_bundles import RecipeBundleStore

FIXTURES = Path(__file__).parents[1] / "fixtures" / "native"
CHILD_INTERRUPT = FIXTURES / "child_interrupt.recipe.yaml"
CHILD_THEN_PARENT = FIXTURES / "child_then_parent_interrupt.recipe.yaml"
PARENT_DIRECT = FIXTURES / "parent_direct.recipe.yaml"
PARENT_INVOKE = FIXTURES / "parent_invoke.recipe.yaml"
PARALLEL_INTERRUPTS = FIXTURES / "parallel_interrupts.recipe.yaml"
SEQUENTIAL_INTERRUPTS = FIXTURES / "sequential_interrupts.recipe.yaml"
WRAPPED_PARENT_DIRECT = FIXTURES / "wrapped_parent_direct.recipe.yaml"


@pytest.fixture(autouse=True)
def _restore_materialization_permissions(tmp_path):
    yield
    for path in sorted(
        tmp_path.rglob("*"), key=lambda item: len(item.parts), reverse=True
    ):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            path.chmod(0o600)


def _results(snapshot, value_by_message: dict[str, str]) -> dict[str, str]:
    return {
        item.coordinate.interrupt_id: value_by_message[item.value]
        for item in snapshot.pending
    }


def _child_then_parent(app, thread_id):
    child = app.invoke({}, thread_id=thread_id).pending[0]
    parent = app.resume(
        thread_id=thread_id,
        results_by_interrupt_id={child.coordinate.interrupt_id: "yes"},
    ).pending[0]
    return child, parent


def _authorized(path: Path, state_root: Path):
    store = RecipeBundleStore(state_root / "recipe-authority")
    return (
        StrictRecipeIngress(path.parent)
        .inspect(path.name)
        .authorize(RecipeAuthorityPolicy())
        .capture(store)
        .materialize(store)
    )


def test_direct_child_interrupt_survives_sqlite_restart(tmp_path):
    """Wrapping a direct child as a callable loses its durable native checkpoint."""
    db = tmp_path / "checkpoints.sqlite"
    recipe = _authorized(PARENT_DIRECT, tmp_path)
    first = yg.open_native_app(recipe, db)
    parked = first.invoke({}, thread_id="parent-a")
    coordinate = parked.pending[0].coordinate
    first.close()

    restarted = yg.open_native_app(recipe, db)
    completed = restarted.resume(
        thread_id="parent-a",
        results_by_interrupt_id={coordinate.interrupt_id: "yes"},
    )
    restarted.close()

    assert completed.values["answer"] == "yes"
    assert completed.values["phase"] == "complete"
    assert completed.pending == ()


def test_parallel_interrupts_support_partial_then_batch_resume(tmp_path):
    """Collapsing native interrupt IDs would make one branch resume the other."""
    app = yg.open_native_app(_authorized(PARALLEL_INTERRUPTS, tmp_path))
    parked = app.invoke({}, thread_id="parallel-partial")
    assert {item.value for item in parked.pending} == {"Branch A?", "Branch B?"}

    first = next(item for item in parked.pending if item.value == "Branch A?")
    waiting = app.resume(
        thread_id="parallel-partial",
        results_by_interrupt_id={first.coordinate.interrupt_id: "alpha"},
    )
    assert waiting.values["answer_a"] == "alpha"
    assert [item.value for item in waiting.pending] == ["Branch B?"]
    assert waiting.values.get("joined") is not True

    completed = app.resume(
        thread_id="parallel-partial",
        results_by_interrupt_id=_results(waiting, {"Branch B?": "beta"}),
    )
    app.close()
    assert completed.pending == ()
    assert completed.values["answer_b"] == "beta"
    assert completed.values["joined"] is True
    assert sorted(completed.values["contributions"]) == ["a", "b"]


def test_parallel_interrupts_support_one_batch_resume_and_native_join(tmp_path):
    """Resuming a batch one-at-a-time can expose a synthetic join race."""
    app = yg.open_native_app(_authorized(PARALLEL_INTERRUPTS, tmp_path))
    parked = app.invoke({}, thread_id="parallel-batch")
    completed = app.resume(
        thread_id="parallel-batch",
        results_by_interrupt_id=_results(
            parked,
            {"Branch A?": "alpha", "Branch B?": "beta"},
        ),
    )
    app.close()

    assert completed.pending == ()
    assert completed.values["joined"] is True
    assert sorted(completed.values["contributions"]) == ["a", "b"]


def test_native_ancestry_rejects_obsolete_checkpoint_fork(tmp_path):
    app = yg.open_native_app(_authorized(SEQUENTIAL_INTERRUPTS, tmp_path))
    try:
        thread_id = "obsolete-fork"
        first = app.invoke({}, thread_id=thread_id).pending[0]
        branch_a = app.resume(
            thread_id=thread_id,
            results_by_interrupt_id={first.coordinate.interrupt_id: "branch-a"},
        ).pending[0]

        fork_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": first.coordinate.checkpoint_ns,
                "checkpoint_id": first.coordinate.checkpoint_id,
            }
        }
        app._app.invoke(  # noqa: SLF001 - create a real public native checkpoint fork
            yg.Command(resume={first.coordinate.interrupt_id: "branch-b"}),
            config=fork_config,
        )
        branch_b = app.snapshot(thread_id=thread_id, subgraphs=True).pending[0]
        occurrences = tuple(
            app.interrupt_history(
                thread_id=thread_id,
                checkpoint_ns=branch_a.coordinate.checkpoint_ns,
                snapshot_limit=64,
            )
        )
        assert branch_a.coordinate != branch_b.coordinate
        assert branch_a.value == branch_b.value == "Second?"
        assert branch_a.coordinate in {item.coordinate for item in occurrences}
        assert branch_b.coordinate in {item.coordinate for item in occurrences}
        assert not app.checkpoint_is_ancestor(
            thread_id=thread_id,
            ancestor_checkpoint_ns=branch_a.coordinate.checkpoint_ns,
            ancestor_checkpoint_id=branch_a.coordinate.checkpoint_id,
            descendant_checkpoint_ns=branch_b.coordinate.checkpoint_ns,
            descendant_checkpoint_id=branch_b.coordinate.checkpoint_id,
            snapshot_limit=64,
        )
    finally:
        app.close()


def test_cross_namespace_ancestry_traversal_has_one_exact_ceiling(
    tmp_path, monkeypatch
):
    app = yg.open_native_app(_authorized(CHILD_THEN_PARENT, tmp_path))
    try:
        thread_id = "bounded-cross-namespace"
        child, parent = _child_then_parent(app, thread_id)
        public_reads = []
        real_get_state = app._app.get_state  # noqa: SLF001 - public-read counter

        def counted_get_state(config, *args, **kwargs):
            public_reads.append(config)
            return real_get_state(config, *args, **kwargs)

        monkeypatch.setattr(app._app, "get_state", counted_get_state)  # noqa: SLF001
        with pytest.raises(yg.NativeHistoryLimitExceeded):
            app.checkpoint_is_ancestor(
                thread_id=thread_id,
                ancestor_checkpoint_ns=child.coordinate.checkpoint_ns,
                ancestor_checkpoint_id=child.coordinate.checkpoint_id,
                descendant_checkpoint_ns=parent.coordinate.checkpoint_ns,
                descendant_checkpoint_id=parent.coordinate.checkpoint_id,
                snapshot_limit=1,
            )
        assert len(public_reads) == 1
    finally:
        app.close()


def test_cross_namespace_ancestry_rejects_missing_completed_subgraph_bridge(
    tmp_path, monkeypatch
):
    app = yg.open_native_app(_authorized(CHILD_THEN_PARENT, tmp_path))
    try:
        thread_id = "missing-completed-bridge"
        child, parent = _child_then_parent(app, thread_id)
        exact_occurrences = tuple(
            item
            for item in app.interrupt_history(
                thread_id=thread_id,
                checkpoint_ns=child.coordinate.checkpoint_ns,
                snapshot_limit=64,
            )
            if item.coordinate == child.coordinate
        )
        assert len(exact_occurrences) == 1

        real_get_state = app._app.get_state  # noqa: SLF001 - adversarial topology
        current_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": parent.coordinate.checkpoint_ns,
                "checkpoint_id": parent.coordinate.checkpoint_id,
            }
        }
        completed_bridge_seen = False
        for _index in range(64):
            snapshot = real_get_state(current_config, subgraphs=True)
            if any(
                task.result is not None and hasattr(task.state, "tasks")
                for task in snapshot.tasks
            ):
                completed_bridge_seen = True
                break
            if snapshot.parent_config is None:
                break
            current_config = snapshot.parent_config
        assert completed_bridge_seen

        def without_completed_bridge(config, *args, **kwargs):
            snapshot = real_get_state(config, *args, **kwargs)
            return snapshot._replace(
                tasks=tuple(
                    task
                    for task in snapshot.tasks
                    if not (
                        task.result is not None and hasattr(task.state, "tasks")
                    )
                )
            )

        monkeypatch.setattr(  # noqa: SLF001 - remove only the causal bridge
            app._app, "get_state", without_completed_bridge
        )
        assert not app.checkpoint_is_ancestor(
            thread_id=thread_id,
            ancestor_checkpoint_ns=child.coordinate.checkpoint_ns,
            ancestor_checkpoint_id=child.coordinate.checkpoint_id,
            descendant_checkpoint_ns=parent.coordinate.checkpoint_ns,
            descendant_checkpoint_id=parent.coordinate.checkpoint_id,
            snapshot_limit=64,
        )
    finally:
        app.close()


def test_cycle_honors_yamlgraph_loop_limit(tmp_path):
    """Replacing native cycles with an outer scheduler would bypass yamlgraph's cap."""
    recipe = tmp_path / "bounded-cycle.recipe.yaml"
    recipe.write_text(
        """
version: "1.0"
name: bounded-cycle
state:
  count: int
nodes:
  tick:
    type: passthrough
    output:
      count: "{state.count + 1}"
edges:
  - {from: START, to: tick}
  - {from: tick, to: tick, condition: "count >= 0"}
loop_limits:
  tick: 2
loop_exits:
  tick: END
"""
    )

    app = yg.open_native_app(_authorized(recipe, tmp_path))
    completed = app.invoke({"count": 0}, thread_id="bounded-cycle")
    app.close()

    assert completed.pending == ()
    assert completed.values["count"] == 2
    assert completed.values["_loop_limit_reached"] is True


def test_ainvoke_is_a_real_async_direct_smoke(tmp_path):
    """An async facade implemented by calling sync invoke cannot prove native async use."""

    async def run():
        app = yg.open_native_app(_authorized(PARENT_DIRECT, tmp_path))
        parked = await app.ainvoke({}, thread_id="async-parent")
        app.close()
        return parked

    parked = asyncio.run(run())
    assert [item.value for item in parked.pending] == ["Answer?"]


def test_subgraph_snapshot_exposes_child_native_coordinate(tmp_path):
    """Flattening a child pause without namespace identity makes resume ambiguous."""
    app = yg.open_native_app(_authorized(PARENT_DIRECT, tmp_path))
    app.invoke({}, thread_id="subgraph-snapshot")
    snapshot = app.snapshot(thread_id="subgraph-snapshot", subgraphs=True)
    history = tuple(app.history(thread_id="subgraph-snapshot"))
    app.close()

    assert [item.value for item in snapshot.pending] == ["Answer?"]
    coordinate = snapshot.pending[0].coordinate
    assert coordinate.task_id
    assert coordinate.interrupt_id
    assert coordinate.checkpoint_ns
    assert history
    assert all(isinstance(item, yg.NativeSnapshot) for item in history)


def test_invoke_children_isolate_two_parent_checkpoint_identities(tmp_path):
    """Dropping parent RunnableConfig aliases both child runs to one checkpoint."""
    app = yg.open_native_app(_authorized(PARENT_INVOKE, tmp_path))
    parked_a = app.invoke({}, thread_id="parent-a")
    parked_b = app.invoke({}, thread_id="parent-b")

    assert parked_a.pending[0].coordinate != parked_b.pending[0].coordinate
    completed_a = app.resume(
        thread_id="parent-a",
        results_by_interrupt_id={parked_a.pending[0].coordinate.interrupt_id: "a"},
    )
    completed_b = app.resume(
        thread_id="parent-b",
        results_by_interrupt_id={parked_b.pending[0].coordinate.interrupt_id: "b"},
    )
    app.close()

    assert completed_a.pending == ()
    assert completed_b.pending == ()
    assert completed_a.values["child_phase"] == "complete"
    assert completed_b.values["child_phase"] == "complete"


def test_stream_yields_only_native_neutral_dtos(tmp_path):
    """Returning LangGraph chunks would leak native runtime types past the adapter."""
    app = yg.open_native_app(_authorized(PARENT_DIRECT, tmp_path))
    events = tuple(app.stream({}, thread_id="stream-parent"))
    app.close()

    assert events
    assert all(isinstance(event, yg.NativeEvent) for event in events)
    assert all(
        type(event.data) in {dict, list, tuple, str, int, float, bool, type(None)}
        for event in events
    )


def test_otel_timeout_wrappers_keep_direct_child_native_across_restart(
    tmp_path, monkeypatch
):
    """Wrapping the direct graph would add an outer span and lose child lineage."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    monkeypatch.setenv("YAMLGRAPH_OTEL_EXPORT", "otlp")
    db = tmp_path / "wrapped.sqlite"

    recipe = _authorized(WRAPPED_PARENT_DIRECT, tmp_path)
    first = yg.open_native_app(recipe, db)
    parked = first.invoke({"seed": "kept"}, thread_id="wrapped-parent")
    assert parked.pending[0].coordinate.checkpoint_ns
    coordinate = parked.pending[0].coordinate
    assert coordinate.thread_id == "wrapped-parent"
    assert coordinate.checkpoint_id
    first.close()

    restarted = yg.open_native_app(recipe, db)
    completed = restarted.resume(
        thread_id="wrapped-parent",
        results_by_interrupt_id={coordinate.interrupt_id: "yes"},
    )
    restarted.close()

    node_names = [
        span.attributes["yamlgraph.node.name"]
        for span in exporter.get_finished_spans()
        if span.name == "yamlgraph.node.execute"
    ]
    assert completed.values["seen"] == "kept"
    assert completed.values["answer"] == "yes"
    assert "finish" in node_names
    assert "done" in node_names
    assert "child" not in node_names


@pytest.mark.parametrize("otel_enabled", [False, True])
def test_wrapped_node_receives_langgraph_injected_config(monkeypatch, otel_enabled):
    """Calling a wrapper directly can only prove a caller-synthesized config."""
    if otel_enabled:
        monkeypatch.setenv("YAMLGRAPH_OTEL_EXPORT", "otlp")
    else:
        monkeypatch.delenv("YAMLGRAPH_OTEL_EXPORT", raising=False)
    sentinel = f"actual-injected-{uuid4()}"

    observed = yg._run_wrapped_injected_config_probe(sentinel)

    assert observed == {"observed_sentinel": sentinel}
