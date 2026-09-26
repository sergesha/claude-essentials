# Bug: a Python node's timeout drops LangGraph runnable context

Published as https://github.com/sheikkinen/yamlgraph/issues/708.

Follow-up to [#474](https://github.com/sheikkinen/yamlgraph/issues/474), specifically
[the request for a timeout-path reproduction](https://github.com/sheikkinen/yamlgraph/issues/474#issuecomment-5800222508).

## Versions

Reproduced with Python 3.12.14, `yamlgraph==0.6.0`, `langgraph==1.2.10`,
`langchain-core==1.5.6`. No LLM provider, API key, checkpointer, or Lockstep is
needed. The reproduction uses the real YAML loader/compiler and an ordinary
state-only Python tool. It does not call private wrappers or monkeypatch them.

## Summary

Adding `timeout: 2` to an otherwise working Python node makes
`langgraph.config.get_config()` fail with:

```text
RuntimeError: Called get_config outside of a runnable context
```

The caller supplies `configurable.sentinel` to `app.invoke`. Without a timeout,
the Python tool reads it successfully. With the timeout, the node runs on a new
`ThreadPoolExecutor` thread whose context does not contain the runnable config.
This is distinct from the subgraph defects fixed in #673.

## Minimal reproduction

Save these three files in one directory.

`repro_tools.py`:

```python
from langgraph.config import get_config


def observe(_state):
    return {"observed": get_config()["configurable"]["sentinel"]}
```

`graph.yaml`:

```yaml
version: "1.0"
name: timeout-context
state:
  observed: str
tools:
  observe:
    type: python
    module: repro_tools
    function: observe
nodes:
  observe:
    type: python
    tool: observe
    on_error: fail
    timeout: 2
edges:
  - {from: START, to: observe}
  - {from: observe, to: END}
```

`reproduce.py`:

```python
from pathlib import Path
from yamlgraph.compile.graph_loader import compile_graph, load_graph_config

path = Path(__file__).with_name("graph.yaml")
app = compile_graph(load_graph_config(path)).compile()
result = app.invoke({}, {"configurable": {"sentinel": "present"}})
assert result["observed"] == "present"
```

Run in a clean environment:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python \
  'yamlgraph==0.6.0' 'langgraph==1.2.10' 'langchain-core==1.5.6'
.venv/bin/python reproduce.py
```

Actual: `RuntimeError: Called get_config outside of a runnable context`.
Delete only `timeout: 2` and rerun: the assertion passes.
Expected: both versions return `observed: present`.

Our local regression harness also runs both variants with two distinct sentinel
values. On the official release, both untimed controls pass and both timed cases
fail. With the proposed fix all four cases pass.

## Cause and proposed fix

The actual path is:

```text
compile_graph -> _compile_python_node
  -> _maybe_wrap_otel(_maybe_wrap_timeout(create_python_node(...)))
  -> timed_fn -> ThreadPoolExecutor.submit(node_fn, state)
  -> Python tool -> get_config()
```

The thread boundary drops contextvars. The earlier timeout hunk proposed in
#474 only forwarded an explicit `config` argument; that alone does **not** fix
this YAML reproduction, because `create_python_node` calls a state-only tool.

Copy the caller's context for each invocation and enter it in the worker:

```python
from contextvars import copy_context

return pool.submit(copy_context().run, node_fn, state).result(timeout=timeout)
```

The downstream patch also retains explicit config forwarding for config-aware
callables (the separate wrapper-composition case originally reported in #474):

```python
# Keep these annotations concrete; no future-annotations import in this module.
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import call_func_with_variable_args


def timed_fn(state: dict, config: RunnableConfig | None = None) -> dict:
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(
            copy_context().run,
            call_func_with_variable_args,
            node_fn,
            state,
            config or {},
        ).result(timeout=timeout)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
```

That excerpt shows the changed success path and cleanup; retain the existing
`except TimeoutError` branch as well when applying it to the real function. Context must be copied per invocation, not once at compile time.

## Regression coverage

- Compile this YAML with and without a timeout; assert the supplied config is
  observable through `get_config()` in both cases.
- Invoke the same compiled graph with two distinct config values; neither may
  reuse the other's context.
- Cover OTel-enabled and disabled wrapper paths without requiring an exporter.
- Retain the config-aware callable composition test from #474 as a separate
  lower-level contract; it is not a substitute for the YAML reproduction.
- Preserve existing timeout/error semantics. This fix does not attempt to cancel
  an already-running Python function when its timeout expires.

The standalone YAML reproduction fails on the released wheel and passes with
context copying. It still fails with the old argument-forwarding-only patch.

## Related issues

- #474 / #673 fixed subgraph config injection; the maintainer explicitly excluded
  the timeout wrapper and requested a reachable reproduction. This report supplies
  one through the YAML Python-node compiler path.
- [#438](https://github.com/sheikkinen/yamlgraph/issues/438) concerns timeout
  cancellation/lifecycle. This report concerns context on a promptly returning
  tool, before any timeout expires.
- The timeout wrapper on upstream `main` was also inspected on 2026-09-26: it
  still submits `node_fn, state` without copying context. The executable results
  above are for the released 0.6.0 wheel.
