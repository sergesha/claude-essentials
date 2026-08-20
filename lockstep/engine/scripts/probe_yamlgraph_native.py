#!/usr/bin/env python3
"""Behavior probe for the yamlgraph capabilities Lockstep requires."""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from pathlib import Path


CHILD = '''
version: "1.0"
name: probe-child
state:
  phase: str
  answer: str
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

DIRECT = '''
version: "1.0"
name: probe-direct
state: {phase: str, answer: str}
nodes:
  child: {type: subgraph, graph: child.yaml, mode: direct}
edges:
  - {from: START, to: child}
  - {from: child, to: END}
'''

INVOKE = '''
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


def _compile(path: Path, saver):
    from yamlgraph.compile.graph_loader import compile_graph, load_graph_config

    return compile_graph(load_graph_config(path)).compile(checkpointer=saver)


def _probe_wrappers() -> None:
    from yamlgraph.compile.node_otel import _maybe_wrap_otel
    from yamlgraph.node_factory.subgraph_nodes import _build_child_config
    from yamlgraph.node_timeout import _maybe_wrap_timeout

    seen = []

    def config_node(state, config):
        seen.append(config)
        return {"out": state["in"] * 2}

    config = {"configurable": {"thread_id": "probe-parent"}}
    wrapped = _maybe_wrap_otel(
        _maybe_wrap_timeout(config_node, {"timeout": 1}, "probe"),
        "probe",
        "python",
    )
    assert wrapped({"in": 21}, config) == {"out": 42}
    assert seen == [config]

    child = _build_child_config(
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
    assert child["configurable"] == {
        "thread_id": "probe-parent:child",
        "tenant": "kept",
    }


def _probe_graphs() -> None:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command

    with tempfile.TemporaryDirectory(prefix="lockstep-yamlgraph-probe-") as raw:
        root = Path(raw)
        child = root / "child.yaml"
        direct = root / "direct.yaml"
        invoke = root / "invoke.yaml"
        database = root / "checkpoints.sqlite"
        child.write_text(CHILD)
        direct.write_text(DIRECT)
        invoke.write_text(INVOKE)
        config = {"configurable": {"thread_id": "probe-direct"}}

        connection = sqlite3.connect(database, check_same_thread=False)
        saver = SqliteSaver(connection)
        saver.setup()
        app = _compile(direct, saver)
        first = app.invoke({}, config)
        assert [item.value for item in first["__interrupt__"]] == ["Answer?"]
        nested = app.get_state(config, subgraphs=True)
        assert nested.tasks[0].state.config["configurable"]["checkpoint_ns"]
        connection.close()

        restarted_connection = sqlite3.connect(database, check_same_thread=False)
        restarted_saver = SqliteSaver(restarted_connection)
        restarted_saver.setup()
        restarted = _compile(direct, restarted_saver)
        completed = restarted.invoke(Command(resume="yes"), config)
        restarted_connection.close()
        assert completed["answer"] == "yes"
        assert "__interrupt__" not in completed

        invoke_app = _compile(invoke, MemorySaver())
        config_a = {"configurable": {"thread_id": "probe-a"}}
        config_b = {"configurable": {"thread_id": "probe-b"}}
        assert "__interrupt__" in invoke_app.invoke({}, config_a)
        assert "__interrupt__" in invoke_app.invoke({}, config_b)
        assert "__interrupt__" not in invoke_app.invoke(Command(resume="a"), config_a)
        assert "__interrupt__" not in invoke_app.invoke(Command(resume="b"), config_b)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    try:
        _probe_wrappers()
        _probe_graphs()
    except BaseException as exc:  # noqa: BLE001 - executable capability boundary
        if not args.quiet:
            print(f"yamlgraph native capability probe failed: {type(exc).__name__}: {exc}")
        return 1
    if not args.quiet:
        print("yamlgraph native capability probe passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
