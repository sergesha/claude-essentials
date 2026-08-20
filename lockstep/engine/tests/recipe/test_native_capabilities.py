from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import lockstep.recipe.yamlgraph_adapter as yg


FIXTURES = Path(__file__).parents[1] / "fixtures" / "native"
CHILD_INTERRUPT = FIXTURES / "child_interrupt.recipe.yaml"
PARENT_DIRECT = FIXTURES / "parent_direct.recipe.yaml"
PARENT_INVOKE = FIXTURES / "parent_invoke.recipe.yaml"
PARALLEL_INTERRUPTS = FIXTURES / "parallel_interrupts.recipe.yaml"
WRAPPED_PARENT_DIRECT = FIXTURES / "wrapped_parent_direct.recipe.yaml"


def _results(snapshot, value_by_message: dict[str, str]) -> dict[str, str]:
    return {
        item.coordinate.interrupt_id: value_by_message[item.value]
        for item in snapshot.pending
    }


def _native_capture():
    path = FIXTURES / "native_tools.py"
    spec = importlib.util.spec_from_file_location("lockstep_native_tools", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.capture


def test_direct_child_interrupt_survives_sqlite_restart(tmp_path):
    """Wrapping a direct child as a callable loses its durable native checkpoint."""
    db = tmp_path / "checkpoints.sqlite"
    first = yg.open_native_app(PARENT_DIRECT, db)
    parked = first.invoke({}, thread_id="parent-a")
    coordinate = parked.pending[0].coordinate
    first.close()

    restarted = yg.open_native_app(PARENT_DIRECT, db)
    completed = restarted.resume(
        thread_id="parent-a",
        results_by_interrupt_id={coordinate.interrupt_id: "yes"},
    )
    restarted.close()

    assert completed.values["answer"] == "yes"
    assert completed.values["phase"] == "complete"
    assert completed.pending == ()


def test_parallel_interrupts_support_partial_then_batch_resume():
    """Collapsing native interrupt IDs would make one branch resume the other."""
    app = yg.open_native_app(PARALLEL_INTERRUPTS)
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


def test_parallel_interrupts_support_one_batch_resume_and_native_join():
    """Resuming a batch one-at-a-time can expose a synthetic join race."""
    app = yg.open_native_app(PARALLEL_INTERRUPTS)
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


def test_cycle_honors_yamlgraph_loop_limit(tmp_path):
    """Replacing native cycles with an outer scheduler would bypass yamlgraph's cap."""
    recipe = tmp_path / "bounded-cycle.recipe.yaml"
    recipe.write_text(
        '''
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
'''
    )

    app = yg.open_native_app(recipe)
    completed = app.invoke({"count": 0}, thread_id="bounded-cycle")
    app.close()

    assert completed.pending == ()
    assert completed.values["count"] == 2
    assert completed.values["_loop_limit_reached"] is True


def test_ainvoke_is_a_real_async_direct_smoke():
    """An async facade implemented by calling sync invoke cannot prove native async use."""

    async def run():
        app = yg.open_native_app(PARENT_DIRECT)
        parked = await app.ainvoke({}, thread_id="async-parent")
        app.close()
        return parked

    parked = asyncio.run(run())
    assert [item.value for item in parked.pending] == ["Answer?"]


def test_subgraph_snapshot_exposes_child_native_coordinate():
    """Flattening a child pause without namespace identity makes resume ambiguous."""
    app = yg.open_native_app(PARENT_DIRECT)
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


def test_invoke_children_isolate_two_parent_checkpoint_identities():
    """Dropping parent RunnableConfig aliases both child runs to one checkpoint."""
    app = yg.open_native_app(PARENT_INVOKE)
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


def test_stream_yields_only_native_neutral_dtos():
    """Returning LangGraph chunks would leak native runtime types past the adapter."""
    app = yg.open_native_app(PARENT_DIRECT)
    events = tuple(app.stream({}, thread_id="stream-parent"))
    app.close()

    assert events
    assert all(isinstance(event, yg.NativeEvent) for event in events)
    assert all(type(event.data) in {dict, list, tuple, str, int, float, bool, type(None)} for event in events)


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

    first = yg.open_native_app(WRAPPED_PARENT_DIRECT, db)
    parked = first.invoke({"seed": "kept"}, thread_id="wrapped-parent")
    assert parked.pending[0].coordinate.checkpoint_ns
    coordinate = parked.pending[0].coordinate
    assert coordinate.thread_id == "wrapped-parent"
    assert coordinate.checkpoint_id
    observed = yg.run_wrapped_config_probe(
        _native_capture(),
        {"seed": "kept"},
        thread_id=coordinate.thread_id,
        checkpoint_ns=coordinate.checkpoint_ns,
        checkpoint_id=coordinate.checkpoint_id,
        node_name="observe",
    )
    assert observed == {
        "seen": "kept",
        "observed_thread_id": "wrapped-parent",
        "observed_checkpoint_ns": coordinate.checkpoint_ns,
        "observed_checkpoint_id": coordinate.checkpoint_id,
    }
    first.close()

    restarted = yg.open_native_app(WRAPPED_PARENT_DIRECT, db)
    completed = restarted.resume(
        thread_id="wrapped-parent",
        results_by_interrupt_id={coordinate.interrupt_id: observed},
    )
    restarted.close()

    node_names = [
        span.attributes["yamlgraph.node.name"]
        for span in exporter.get_finished_spans()
        if span.name == "yamlgraph.node.execute"
    ]
    assert completed.values["seen"] == "kept"
    assert completed.values["answer"] == observed
    assert completed.values["observed_thread_id"] == "wrapped-parent"
    assert completed.values["observed_checkpoint_ns"] == coordinate.checkpoint_ns
    assert completed.values["observed_checkpoint_id"] == coordinate.checkpoint_id
    assert "observe" in node_names
    assert "finish" in node_names
    assert "done" in node_names
    assert "child" not in node_names
