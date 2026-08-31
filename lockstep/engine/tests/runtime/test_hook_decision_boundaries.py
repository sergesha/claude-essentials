"""Ownership boundaries for the three R4 hook decision cores."""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import textwrap

_CORES = {
    "lockstep.runtime._hook_stop_decision": (
        "_matching_stop_runs",
        "_render_stop_decision",
    ),
    "lockstep.runtime._hook_pretool_decision": (
        "_matching_policy",
        "_policy_binding",
        "_matching_candidates",
        "_session_decision",
        "decide_pretool",
    ),
    "lockstep.runtime._hook_posttool_decision": (
        "_posttool_identity",
        "_worker_awaiting",
    ),
}
_UNCHANGED_DIGESTS = {
    "_active_native": "ab83b7dc533f64eb808770cb9d961fb9d3b671b72eeaa784357450635761bf7d",
    "_fast_path_empty": "24fabec2350bea0553783cfaa38c0c5cd02c4b816bc185cd7c59a9b8d76a3cda",
    "_find_marked_run_id": "746a78c47a9c67cf2ecf68e74d7d4cd33ed6127c59bc075d89aa9aa32bcf6d8d",
    "_find_run_id": "2e7cb0aca4d64e6adf221c30a915b712e09bd162094c5779862de377cf4cbfeb",
    "_owned_by_another_live_session": "626c1c4b37f03e621ed2aa5a06c2e4f069f9ce451faaa0619fc4436230d8f8bd",
    "_policy_path": "d659fbabb03d1f261f8e3c444137b3f0998781c3da0dc43a1a8da7cb52033774",
    "_policy_slug": "cb783bddb00fe9193c6beff25e00cb040539cbf8b0ce54b786d5bcf1dd790855",
    "_read_only_legacy_authoring_diagnostics": "aee912ebc5da661ed5053f4cfe86d44a8cc94ce86005ca0798bb48a7fa3302b4",
    "doctor": "6258964bd77f155d646706c9bf4f65871ee0c29ca48519bb5149bc68edca9c1e",
    "hook_session_start": "3739ef475de22a897e86231e5b4540521c36865bdd69c881846aaceb88d6686b",
    "policy_clear": "ed901a8487875f03a27624211e3c6ad9ed32f64b85306f29b8c6d90b72d70365",
    "policy_require": "7e7877b8b953f7f681fab4a78f717b5c9f6fc6a0a62210229473e2d10b536543",
}


def _functions(module: object) -> tuple[str, ...]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(module)))
    return tuple(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _function_nodes(module: object) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(module)))
    return {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }


def _digest(node: ast.AST) -> str:
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


def _imports(module: object) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(module)))
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_hook_decision_cores_are_acyclic_and_have_exact_owners() -> None:
    for module_name, expected_functions in _CORES.items():
        module = importlib.import_module(module_name)
        assert _functions(module) == expected_functions
        assert "lockstep.runtime.hooks" not in _imports(module)
        assert not ((set(_CORES) - {module_name}) & _imports(module))


def test_public_hook_surface_stays_in_hooks_facade() -> None:
    hooks = importlib.import_module("lockstep.runtime.hooks")
    assert hooks.hook_stop.__module__ == "lockstep.runtime.hooks"
    assert hooks.hook_pretool.__module__ == "lockstep.runtime.hooks"
    assert hooks.hook_posttool.__module__ == "lockstep.runtime.hooks"
    assert tuple(inspect.signature(hooks.hook_stop).parameters) == (
        "stdin_json",
        "state_dir",
        "cwd",
    )
    assert tuple(inspect.signature(hooks.hook_pretool).parameters) == (
        "stdin_json",
        "state_dir",
    )
    assert tuple(inspect.signature(hooks.hook_posttool).parameters) == (
        "stdin_json",
        "state_dir",
    )


def test_unchanged_hook_policy_and_diagnostic_surface_is_ast_frozen() -> None:
    hooks = importlib.import_module("lockstep.runtime.hooks")
    nodes = _function_nodes(hooks)
    assert {name: _digest(nodes[name]) for name in _UNCHANGED_DIGESTS} == (
        _UNCHANGED_DIGESTS
    )


def test_hook_facade_retains_exact_exception_polarity() -> None:
    hooks = importlib.import_module("lockstep.runtime.hooks")
    nodes = _function_nodes(hooks)
    stop_try = next(node for node in nodes["hook_stop"].body if isinstance(node, ast.Try))
    pre_try = next(node for node in nodes["hook_pretool"].body if isinstance(node, ast.Try))
    post_try = next(node for node in nodes["hook_posttool"].body if isinstance(node, ast.Try))

    assert ast.unparse(stop_try.handlers[0].body[0].value) == "(0, '')"
    assert ast.unparse(pre_try.handlers[0].body[0].value.func) == "_deny"
    assert isinstance(post_try.handlers[0].body[0], ast.Pass)
    assert "_render_stop_decision" in ast.unparse(stop_try)
    assert "decide_pretool" in ast.unparse(pre_try)
    assert "sessions.touch" in ast.unparse(post_try)


def test_policy_free_pretool_does_not_consult_stale_session_config(
    tmp_path, monkeypatch
) -> None:
    hooks = importlib.import_module("lockstep.runtime.hooks")
    monkeypatch.setattr(
        hooks,
        "_session_stale_minutes",
        lambda: (_ for _ in ()).throw(AssertionError("must stay lazy")),
    )

    assert hooks.hook_pretool({"cwd": str(tmp_path)}, tmp_path / "state") == (0, "")
