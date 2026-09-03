"""Ownership boundary for the Codex supervisor transaction."""

from __future__ import annotations

import ast
import inspect
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from lockstep.runtime.providers import _codex_supervisor as supervisor

_HELPERS = {
    "_atomic_json",
    "_read_spec",
    "_read_identity",
    "_verify_bound_files",
    "_capture",
    "_kill_group",
    "_terminate_group",
    "_group_is_dead",
    "_wait_group_dead",
    "_finish_capture",
    "_publish_terminal",
    "_launch_inputs",
    "_publish_prelaunch_terminal",
    "_await_launch_permission",
    "_spawn_inner_process",
    "_start_capture",
    "_monitor_process",
    "_terminal_reason",
    "_contain_spawned_process",
}


def _top_level_definitions(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _call_name(node: ast.Call) -> str:
    current = node.func
    parts: list[str] = []
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def test_supervisor_has_one_transaction_owner_and_thin_run() -> None:
    tree = ast.parse(inspect.getsource(supervisor))
    definitions = _top_level_definitions(tree)

    assert set(definitions) == _HELPERS | {
        "_CodexSupervisorTransaction",
        "run",
        "main",
    }
    transaction = definitions["_CodexSupervisorTransaction"]
    assert isinstance(transaction, ast.ClassDef)
    methods = {
        node.name
        for node in transaction.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert methods == {"__init__", "execute"}
    assert supervisor._CodexSupervisorTransaction.__module__ == supervisor.__name__

    initializer = next(
        node
        for node in transaction.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    stored_fields = {
        node.attr
        for node in ast.walk(initializer)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }
    assert stored_fields == {"_spec", "_argv", "_environment"}

    run_node = definitions["run"]
    execute = next(
        node
        for node in transaction.body
        if isinstance(node, ast.FunctionDef) and node.name == "execute"
    )
    assert isinstance(run_node, ast.FunctionDef)
    run_calls = [
        _call_name(node) for node in ast.walk(run_node) if isinstance(node, ast.Call)
    ]
    run_calls = ["execute" if name.endswith(".execute") else name for name in run_calls]
    assert Counter(run_calls) == Counter(
        {
            "_read_spec": 1,
            "_launch_inputs": 1,
            "_CodexSupervisorTransaction": 1,
            "execute": 1,
        }
    )
    assert all(
        isinstance(node, (ast.Assign, ast.Return))
        for node in run_node.body
    )
    execute_calls = {
        _call_name(node) for node in ast.walk(execute) if isinstance(node, ast.Call)
    }
    assert {"os.open", "fcntl.flock", "os.close"} <= execute_calls
    assert {
        "_await_launch_permission",
        "_spawn_inner_process",
        "_start_capture",
        "_monitor_process",
        "_finish_capture",
        "_publish_terminal",
        "_contain_spawned_process",
    } <= execute_calls


def test_run_and_execute_retain_return_and_module_monkeypatch_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = {
        "alive": "/probe/alive",
        "supervisor_ready": "/probe/ready",
        "go": "/probe/go",
        "cancel": "/probe/cancel",
    }
    argv = ["codex", "exec"]
    environment = {"LOCKSTEP_PROBE": "1"}
    events: list[tuple[object, ...]] = []

    class ProbeTransaction:
        def __init__(self, observed_spec, observed_argv, observed_environment) -> None:
            events.append(
                ("construct", observed_spec, observed_argv, observed_environment)
            )

        def execute(self) -> int:
            events.append(("execute",))
            return 37

    with monkeypatch.context() as orchestration:
        orchestration.setattr(
            supervisor,
            "_read_spec",
            lambda path, digest: events.append(("read", path, digest)) or spec,
        )
        orchestration.setattr(
            supervisor,
            "_launch_inputs",
            lambda observed: events.append(("inputs", observed))
            or (argv, environment),
        )
        orchestration.setattr(
            supervisor, "_CodexSupervisorTransaction", ProbeTransaction
        )

        path = Path("supervisor.json")
        assert supervisor.run(path, "a" * 64) == 37
        assert events == [
            ("read", path, "a" * 64),
            ("inputs", spec),
            ("construct", spec, argv, environment),
            ("execute",),
        ]

    events.clear()
    fake_os = SimpleNamespace(
        O_RDWR=supervisor.os.O_RDWR,
        O_CREAT=supervisor.os.O_CREAT,
        O_EXCL=supervisor.os.O_EXCL,
        open=lambda path, flags, mode: events.append(
            ("open", path, flags, mode)
        )
        or 41,
        close=lambda descriptor: events.append(("close", descriptor)),
        getpid=lambda: 1234,
    )
    fake_fcntl = SimpleNamespace(
        LOCK_EX=supervisor.fcntl.LOCK_EX,
        LOCK_NB=supervisor.fcntl.LOCK_NB,
        flock=lambda descriptor, flags: events.append(("flock", descriptor, flags)),
    )
    monkeypatch.setattr(supervisor, "os", fake_os)
    monkeypatch.setattr(supervisor, "fcntl", fake_fcntl)
    monkeypatch.setattr(
        supervisor,
        "_atomic_json",
        lambda path, value: events.append(("ready", path, value)),
    )
    monkeypatch.setattr(
        supervisor,
        "_await_launch_permission",
        lambda observed, go, cancel: events.append(
            ("await", observed, go, cancel)
        )
        or "cancelled",
    )
    monkeypatch.setattr(
        supervisor,
        "_publish_prelaunch_terminal",
        lambda observed, reason: events.append(("terminal", observed, reason)),
    )

    transaction = supervisor._CodexSupervisorTransaction(spec, argv, environment)
    assert transaction.execute() == 0
    assert events == [
        ("open", Path("/probe/alive"), fake_os.O_RDWR | fake_os.O_CREAT | fake_os.O_EXCL, 0o600),
        ("flock", 41, fake_fcntl.LOCK_EX | fake_fcntl.LOCK_NB),
        (
            "ready",
            Path("/probe/ready"),
            {"schema": "lockstep.codex-supervisor-ready/v1", "pid": 1234},
        ),
        ("await", spec, Path("/probe/go"), Path("/probe/cancel")),
        ("terminal", spec, "cancelled"),
        ("close", 41),
    ]
