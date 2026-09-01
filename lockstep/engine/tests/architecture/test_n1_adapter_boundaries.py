"""N1 freezes public CLI/MCP schemas around stateless adapters."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lockstep.errors import AuthoringError
from lockstep.runtime.errors import LockstepError

_PARSER_SCHEMA_SHA256 = "f77e4a7a0f29a060d434087670fa78d70c91247d286c2a9fe6d7a285ee1da4d9"
_DRYRUN_PARAMETERS = {
    "properties": {
        "evidence": {"additionalProperties": True, "title": "Evidence", "type": "object"},
        "recipe": {"title": "Recipe", "type": "string"},
        "step": {"title": "Step", "type": "string"},
    },
    "required": ["recipe", "step", "evidence"],
    "title": "scenario_dryrunArguments",
    "type": "object",
}
_DRYRUN_DESCRIPTION = (
    "SHAPE-ONLY dryrun: applies the same `_`-prefix\n"
    "    rejection, schema validation, and path resolve+containment `done()`\n"
    "    applies (project root = server cwd, since there is no run). Runs only\n"
    "    shape checks; command/baseline checks report `skipped (dryrun)` and\n"
    "    never execute. No catalog entry, checkpoint, or baseline artifact —\n"
    "    nothing durable, nothing besides shape checks actually runs."
)
_MODULE_FUNCTIONS = {
    "lockstep._cli_support": {
        "current_project",
        "decode_object",
        "write_json",
        "require_owner_tty",
        "read_consent_token",
    },
    "lockstep._cli_scenario": {
        "_decode_object",
        "_observation_result",
        "_command_result",
        "run_scenario_command",
    },
    "lockstep._cli_consent": {
        "_require_owner_tty",
        "_read_consent_token",
        "_issue",
        "_accept",
        "_revoke",
        "run_consent_command",
    },
    "lockstep._cli_parser": {
        "_add_policy_parser",
        "_add_recipe_parser",
        "_add_template_parser",
        "_add_scenario_parser",
        "_add_consent_parser",
        "_add_owner_parser",
        "build_parser",
    },
    "lockstep.mcp._scenario_dryrun": {
        "_reserved_evidence_error",
        "prevalidate_scenario_evidence",
        "_materialize_brief",
        "_shape_check_result",
        "_shape_results",
        "evaluate_scenario_dryrun",
    },
}
_MODULE_IMPORTS = {
    "lockstep._cli_support": {
        ("__future__", "annotations", None),
        (None, "getpass", None),
        (None, "json", None),
        (None, "sys", None),
        ("pathlib", "Path", None),
        ("lockstep.errors", "AuthoringError", None),
    },
    "lockstep._cli_scenario": {
        ("__future__", "annotations", None),
        (None, "argparse", None),
        ("lockstep", "_cli_support", "support"),
        ("lockstep.runtime", "engine", "engine_module"),
        ("lockstep.runtime.config", "state_dir", None),
    },
    "lockstep._cli_consent": {
        ("__future__", "annotations", None),
        (None, "argparse", None),
        ("lockstep", "_cli_support", "support"),
        ("lockstep.runtime", "engine", "engine_module"),
        ("lockstep.runtime.config", "state_dir", None),
    },
    "lockstep._cli_parser": {
        ("__future__", "annotations", None),
        (None, "argparse", None),
    },
    "lockstep.mcp._scenario_dryrun": {
        ("__future__", "annotations", None),
        (None, "tempfile", None),
        ("collections.abc", "Callable", None),
        ("pathlib", "Path", None),
        ("lockstep.runtime", "evidence", "evidence_mod"),
        ("lockstep.runtime", "validators", None),
        ("lockstep.runtime._service_payloads", "validate_evidence_shape", None),
        ("lockstep.runtime.recipe_bundles", "RecipeBundleStore", None),
    },
}


def _parser_shape(parser: argparse.ArgumentParser) -> dict[str, object]:
    result = []
    for action in parser._actions:
        item: dict[str, object] = {
            "option_strings": action.option_strings,
            "dest": action.dest,
            "required": action.required,
            "nargs": action.nargs,
            "default": action.default,
            "const": action.const,
            "type": getattr(action.type, "__name__", None),
            "choices": tuple(action.choices) if action.choices is not None else None,
            "class": type(action).__name__,
            "help": action.help,
            "metavar": action.metavar,
        }
        if isinstance(action, argparse._SubParsersAction):
            item["subparsers"] = {
                name: _parser_shape(child) for name, child in action.choices.items()
            }
        result.append(item)
    return {
        "prog": parser.prog,
        "usage": parser.usage,
        "description": parser.description,
        "epilog": parser.epilog,
        "allow_abbrev": parser.allow_abbrev,
        "prefix_chars": parser.prefix_chars,
        "formatter_class": (
            parser.formatter_class.__module__,
            parser.formatter_class.__qualname__,
        ),
        "help": parser.format_help(),
        "actions": result,
    }


def _schema_digest(parser: argparse.ArgumentParser) -> str:
    payload = json.dumps(
        _parser_shape(parser), sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _body_call_names(function: object) -> tuple[str, ...]:
    node = ast.parse(inspect.getsource(function)).body[0]
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    return tuple(
        ast.unparse(call.func)
        for statement in node.body
        for call in ast.walk(statement)
        if isinstance(call, ast.Call)
    )


def _immutable_default(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) or (
        isinstance(node, ast.Tuple) and all(_immutable_default(item) for item in node.elts)
    )


@pytest.mark.parametrize(
    "module_name",
    (
        "lockstep._cli_support",
        "lockstep._cli_scenario",
        "lockstep._cli_consent",
        "lockstep._cli_parser",
        "lockstep.mcp._scenario_dryrun",
    ),
)
def test_n1_adapter_module_exists_independently(module_name: str) -> None:
    importlib.import_module(module_name)


@pytest.mark.parametrize(
    "modules",
    (
        (
            "lockstep._cli_support",
            "lockstep._cli_scenario",
            "lockstep._cli_consent",
            "lockstep._cli_parser",
            "lockstep.cli",
        ),
        (
            "lockstep.cli",
            "lockstep._cli_parser",
            "lockstep._cli_consent",
            "lockstep._cli_scenario",
            "lockstep._cli_support",
        ),
        ("lockstep.mcp._scenario_dryrun", "lockstep.mcp.server"),
        ("lockstep.mcp.server", "lockstep.mcp._scenario_dryrun"),
    ),
)
def test_n1_adapter_import_dag_is_fresh_process_safe(
    modules: tuple[str, ...],
) -> None:
    program = (
        "import importlib,json; "
        f"names={modules!r}; "
        "print(json.dumps([importlib.import_module(name).__name__ for name in names]))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == list(modules)


def test_cli_import_keeps_runtime_engine_lazy_for_non_scenario_commands() -> None:
    program = (
        "import json,sys; import lockstep.cli; "
        "print(json.dumps('lockstep.runtime.engine' in sys.modules))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) is False


@pytest.mark.parametrize("facade_first", (False, True))
def test_n1_facade_bindings_are_order_independent_in_fresh_process(
    facade_first: bool,
) -> None:
    program = f"""
import importlib, sys
facade_first = {facade_first!r}
if facade_first:
    cli = importlib.import_module('lockstep.cli')
else:
    scenario = importlib.import_module('lockstep._cli_scenario')
    consent = importlib.import_module('lockstep._cli_consent')
    parser = importlib.import_module('lockstep._cli_parser')
    assert 'lockstep.cli' not in sys.modules
    cli = importlib.import_module('lockstep.cli')
scenario = importlib.import_module('lockstep._cli_scenario')
consent = importlib.import_module('lockstep._cli_consent')
parser = importlib.import_module('lockstep._cli_parser')
assert cli._cmd_scenario is scenario.run_scenario_command
assert cli._cmd_consent is consent.run_consent_command
assert cli._build_parser is parser.build_parser
if facade_first:
    server = importlib.import_module('lockstep.mcp.server')
else:
    dryrun = importlib.import_module('lockstep.mcp._scenario_dryrun')
    assert 'lockstep.mcp.server' not in sys.modules
    server = importlib.import_module('lockstep.mcp.server')
dryrun = importlib.import_module('lockstep.mcp._scenario_dryrun')
assert server.evaluate_scenario_dryrun is dryrun.evaluate_scenario_dryrun
"""
    subprocess.run([sys.executable, "-c", program], check=True)


def test_cli_entrypoints_preserve_signatures_and_delegate_identity() -> None:
    cli = importlib.import_module("lockstep.cli")
    scenario = importlib.import_module("lockstep._cli_scenario")
    consent = importlib.import_module("lockstep._cli_consent")
    parser = importlib.import_module("lockstep._cli_parser")

    assert cli._cmd_scenario is scenario.run_scenario_command
    assert cli._cmd_consent is consent.run_consent_command
    assert cli._build_parser is parser.build_parser
    assert str(inspect.signature(cli._cmd_scenario)) == "(args: 'argparse.Namespace') -> 'int'"
    assert str(inspect.signature(cli._cmd_consent)) == "(args: 'argparse.Namespace') -> 'int'"
    assert str(inspect.signature(cli._build_parser)) == "() -> 'argparse.ArgumentParser'"
    assert _schema_digest(cli._build_parser()) == _PARSER_SCHEMA_SHA256

    tree = ast.parse(inspect.getsource(cli))
    moved = {"_cmd_scenario", "_cmd_consent", "_build_parser"}
    assert not {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    } & moved
    imported = {
        alias.asname: (node.module, alias.name)
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname in moved
    }
    assert imported == {
        "_cmd_scenario": ("lockstep._cli_scenario", "run_scenario_command"),
        "_cmd_consent": ("lockstep._cli_consent", "run_consent_command"),
        "_build_parser": ("lockstep._cli_parser", "build_parser"),
    }


def test_n1_adapter_modules_cannot_become_state_owners() -> None:
    for module_name, expected_functions in _MODULE_FUNCTIONS.items():
        module = importlib.import_module(module_name)
        tree = ast.parse(inspect.getsource(module))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update((None, alias.name, alias.asname) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.update(
                    (node.module, alias.name, alias.asname) for alias in node.names
                )
        assert imports == _MODULE_IMPORTS[module_name]
        assert not any(
            isinstance(node, (ast.ClassDef, ast.Global)) for node in ast.walk(tree)
        )
        assert all(
            isinstance(
                statement,
                (ast.Expr, ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign),
            )
            for statement in tree.body
        )
        expressions = [
            statement for statement in tree.body if isinstance(statement, ast.Expr)
        ]
        assert len(expressions) == 1
        assert expressions[0] is tree.body[0]
        assert isinstance(expressions[0].value, ast.Constant)
        assert isinstance(expressions[0].value.value, str)
        assert {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        } == expected_functions
        assert not any(isinstance(node, ast.AsyncFunctionDef) for node in tree.body)
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        assert all(not function.decorator_list for function in functions)
        assert all(
            _immutable_default(default)
            for function in functions
            for default in (*function.args.defaults, *function.args.kw_defaults)
            if default is not None
        )
        assert not any(
            isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            and any(
                isinstance(target, (ast.Attribute, ast.Subscript))
                for target in (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else (node.target,)
                )
            )
            for node in ast.walk(tree)
        )
        assert not any(
            isinstance(node, ast.Call)
            and ast.unparse(node.func) in {
                "setattr",
                "getattr",
                "eval",
                "exec",
                "__import__",
                "vars",
                "globals",
                "locals",
            }
            for node in ast.walk(tree)
        )
        assert not any(
            isinstance(node, ast.Attribute)
            and node.attr in {"__dict__", "__getattribute__"}
            for node in ast.walk(tree)
        )
        owner_calls = [
            ast.unparse(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and any(
                name in ast.unparse(node.func)
                for name in ("Engine", "LockstepCommandService", "_command_for", "_projection_for")
            )
        ]
        expected_owner_calls = {
            "lockstep._cli_support": [],
            "lockstep._cli_scenario": [
                "engine_module.Engine.observe",
                "engine_module.Engine.command",
            ],
            "lockstep._cli_consent": ["engine_module.Engine.command"],
            "lockstep._cli_parser": [],
            "lockstep.mcp._scenario_dryrun": [],
        }
        assert sorted(owner_calls) == sorted(expected_owner_calls[module_name])
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        owner_references = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and "Engine" in ast.unparse(node)
            and not (
                isinstance(parents.get(node), ast.Attribute)
                and parents[node].value is node
            )
        ]
        assert all(
            isinstance(parents.get(node), ast.Call) and parents[node].func is node
            for node in owner_references
        )
        assignments = [
            statement
            for statement in tree.body
            if isinstance(statement, ast.Assign)
        ]
        expected_assignments = 1 if module_name == "lockstep.mcp._scenario_dryrun" else 0
        assert len(assignments) == expected_assignments
        for assignment in assignments:
            assert len(assignment.targets) == 1
            assert isinstance(assignment.targets[0], ast.Name)
            assert assignment.targets[0].id == "SHAPE_CHECK_TYPES"
            assert isinstance(assignment.value, ast.Call)
            assert isinstance(assignment.value.func, ast.Name)
            assert assignment.value.func.id == "frozenset"
            assert not assignment.value.keywords
            assert len(assignment.value.args) == 1
            values = assignment.value.args[0]
            assert isinstance(values, (ast.Tuple, ast.List, ast.Set))
            assert len(values.elts) == 4
            assert all(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in values.elts
            )
            assert {item.value for item in values.elts} == {
                "file_exists",
                "file_nonempty",
                "md_has_sections",
                "file_matches",
            }
    dryrun = importlib.import_module("lockstep.mcp._scenario_dryrun")
    assert dryrun.SHAPE_CHECK_TYPES == frozenset(
        {"file_exists", "file_nonempty", "md_has_sections", "file_matches"}
    )


def test_mcp_dryrun_preserves_tool_schema_and_delegates_exact_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = importlib.import_module("lockstep.mcp.server")

    assert str(inspect.signature(server.scenario_dryrun)) == (
        "(recipe: 'str', step: 'str', evidence: 'dict', "
        "ctx: 'Context | None' = None) -> 'dict'"
    )
    tools = {tool.name: tool for tool in server.app._tool_manager.list_tools()}
    tool = tools["scenario_dryrun"]
    assert tool.fn is server.scenario_dryrun
    assert tool.parameters == _DRYRUN_PARAMETERS
    assert tool.description == _DRYRUN_DESCRIPTION
    assert tool.context_kwarg == "ctx"
    assert tool.is_async is False
    assert tool.annotations is None
    assert tool.fn_metadata.output_schema is None
    assert tool.fn_metadata.wrap_output is False
    assert _body_call_names(server.scenario_dryrun) == (
        "prevalidate_scenario_evidence",
        "_project_for_context",
        "_configured_paths",
        "evaluate_scenario_dryrun",
    )

    project = Path("/host/project")
    recipes = Path("/owner/recipes")
    context = object()
    evidence = {"answer": "yes"}
    original_evidence = {"answer": "yes"}
    calls = []
    prevalidation_calls = []
    project_calls = []
    configured_calls = []
    validated_evidence = {"answer": "yes"}

    def prevalidate(actual):
        prevalidation_calls.append(actual)
        return validated_evidence, None

    def project_for_context(actual):
        project_calls.append(actual)
        return project

    def configured_paths(actual):
        configured_calls.append(actual)
        return Path("/owner/state"), recipes

    monkeypatch.setattr(server, "_project_for_context", project_for_context)
    monkeypatch.setattr(server, "_configured_paths", configured_paths)
    monkeypatch.setattr(server, "prevalidate_scenario_evidence", prevalidate)

    def evaluate(*args, **kwargs):
        calls.append((args, kwargs))
        return {"accepted": True, "results": []}

    monkeypatch.setattr(server, "evaluate_scenario_dryrun", evaluate)
    result = server.scenario_dryrun("release", "review", evidence, ctx=context)
    assert result == {"accepted": True, "results": []}
    assert evidence == original_evidence
    assert project_calls == [context]
    assert configured_calls == [project]
    assert calls == [
        (
            (recipes, "release", "review", validated_evidence),
            {
                "project_root": project,
                "containment_errors": server._containment_errors,
                "load_step_brief": server._load_step_brief,
                "preflight": server.preflight_recipe,
            },
        )
    ]
    assert prevalidation_calls == [evidence]
    assert prevalidation_calls[0] is evidence
    assert calls[0][0][3] is validated_evidence


def test_mcp_dryrun_rejects_untrusted_evidence_before_ambient_workspace() -> None:
    server = importlib.import_module("lockstep.mcp.server")
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            meta={"x-codex-turn-metadata": {"workspaces": {"\0": {}}}}
        )
    )

    with pytest.raises(LockstepError, match="scalar exceeds byte limit"):
        server.scenario_dryrun(
            "missing",
            "review",
            {"huge": "x" * 70_000},
            ctx=context,
        )

    assert server.scenario_dryrun(
        "missing",
        "review",
        {"_forged": True},
        ctx=context,
    ) == {
        "accepted": False,
        "errors": ["reserved evidence key(s) rejected: ['_forged']"],
    }


def test_dryrun_evaluator_enforces_containment_without_runtime_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dryrun = importlib.import_module("lockstep.mcp._scenario_dryrun")
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "format": "project-path"}},
    }
    brief = {"evidence_schema": schema, "checks": []}
    project = Path("/host/project")
    recipes = Path("/owner/recipes")
    materialize_calls = []
    containment_calls = []
    shape_calls = []

    def materialize(*args):
        materialize_calls.append(args)
        return brief

    def containment(actual_schema, actual_evidence, actual_project):
        containment_calls.append((actual_schema, actual_evidence, actual_project))
        path = actual_evidence["path"]
        return ["path escapes project root"] if path.startswith("..") else []

    def shape_results(*args):
        shape_calls.append(args)
        return []

    monkeypatch.setattr(dryrun, "_materialize_brief", materialize)
    monkeypatch.setattr(dryrun, "_shape_results", shape_results)
    schema_calls = []

    def validate_schema(actual_schema, actual_evidence):
        schema_calls.append((actual_schema, actual_evidence))
        return ["schema invalid"] if actual_evidence.get("path") == 7 else []

    monkeypatch.setattr(dryrun.evidence_mod, "validate_evidence", validate_schema)
    loader = object()
    preflight = object()
    invalid = {"path": 7}
    invalid_result = dryrun.evaluate_scenario_dryrun(
        recipes,
        "release",
        "review",
        invalid,
        project_root=project,
        containment_errors=containment,
        load_step_brief=loader,
        preflight=preflight,
    )
    assert invalid_result == {"accepted": False, "errors": ["schema invalid"]}
    assert containment_calls == []
    assert shape_calls == []
    outside = {"path": "../../outside"}
    rejected = dryrun.evaluate_scenario_dryrun(
        recipes,
        "release",
        "review",
        outside,
        project_root=project,
        containment_errors=containment,
        load_step_brief=loader,
        preflight=preflight,
    )
    assert rejected == {"accepted": False, "errors": ["path escapes project root"]}
    assert shape_calls == []

    inside = {"path": "reports/review.md"}
    accepted = dryrun.evaluate_scenario_dryrun(
        recipes,
        "release",
        "review",
        inside,
        project_root=project,
        containment_errors=containment,
        load_step_brief=loader,
        preflight=preflight,
    )
    assert accepted == {"accepted": True, "results": []}
    assert materialize_calls == [
        (recipes, "release", "review", loader, preflight),
        (recipes, "release", "review", loader, preflight),
        (recipes, "release", "review", loader, preflight),
    ]
    assert schema_calls == [(schema, invalid), (schema, outside), (schema, inside)]
    assert containment_calls == [
        (schema, outside, str(project)),
        (schema, inside, str(project)),
    ]
    assert shape_calls == [(brief, inside, {"_project": str(project)})]


def test_dryrun_shape_results_preserve_order_and_never_execute_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dryrun = importlib.import_module("lockstep.mcp._scenario_dryrun")
    calls = []

    class ShapeFailure(Exception):
        pass

    def passed(check, evidence, context):
        calls.append(("pass", check, evidence, context))
        return []

    def failed(check, evidence, context):
        calls.append(("fail", check, evidence, context))
        return ["empty"]

    def errored(check, evidence, context):
        calls.append(("error", check, evidence, context))
        raise ShapeFailure("bad recipe path")

    def forbidden(*_args):
        pytest.fail("dryrun executed a command/baseline check")

    monkeypatch.setattr(
        dryrun.validators,
        "CHECKS",
        {
            "file_exists": passed,
            "file_nonempty": failed,
            "md_has_sections": errored,
            "cmd_ok": forbidden,
        },
    )
    checks = [
        {"type": "file_exists", "path": "a"},
        {"type": "file_nonempty", "path": "b"},
        {"type": "md_has_sections", "path": "c"},
        {"type": "file_matches", "path": "d"},
        {"type": "cmd_ok", "command": "forbidden"},
    ]
    brief = {"checks": checks}
    evidence = {"answer": "yes"}
    context = {"_project": "/project"}
    assert dryrun._shape_results(brief, evidence, context) == [
        {"type": "file_exists", "verdict": "pass", "reasons": []},
        {"type": "file_nonempty", "verdict": "fail", "reasons": ["empty"]},
        {"type": "md_has_sections", "verdict": "error", "reasons": ["bad recipe path"]},
        {"type": "file_matches", "verdict": "fail", "reasons": ["unknown check type: 'file_matches'"]},
        {"type": "cmd_ok", "verdict": "skipped (dryrun)"},
    ]
    assert calls == [
        ("pass", checks[0], evidence, context),
        ("fail", checks[1], evidence, context),
        ("error", checks[2], evidence, context),
    ]


@pytest.mark.parametrize("loader_fails", (False, True))
def test_dryrun_materialization_is_ephemeral_and_loads_before_cleanup(
    loader_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dryrun = importlib.import_module("lockstep.mcp._scenario_dryrun")
    events = []
    source_path = Path("/transient/recipe.yaml")

    class Temporary:
        def __enter__(self):
            events.append("enter")
            return "/transient"

        def __exit__(self, *_args):
            events.append("exit")

    class Authorized:
        def capture(self, store):
            events.append(("capture", store))
            return self

        def materialize(self, store):
            events.append(("materialize", store))
            return SimpleNamespace(source_path=source_path)

    def preflight(actual_recipes, actual_recipe):
        events.append(("preflight", actual_recipes, actual_recipe))
        return Authorized()

    def store(path):
        events.append(("store", path))
        return "ephemeral-store"

    def loader(actual_path, actual_step):
        events.append(("loader", actual_path, actual_step))
        assert "exit" not in events
        if loader_fails:
            raise RuntimeError("loader failed")
        return {"checks": []}

    temporary_calls = []

    def temporary_directory(**kwargs):
        temporary_calls.append(kwargs)
        return Temporary()

    monkeypatch.setattr(dryrun.tempfile, "TemporaryDirectory", temporary_directory)
    monkeypatch.setattr(dryrun, "RecipeBundleStore", store)
    if loader_fails:
        with pytest.raises(RuntimeError, match="loader failed"):
            dryrun._materialize_brief(Path("/recipes"), "release", "review", loader, preflight)
    else:
        assert dryrun._materialize_brief(
            Path("/recipes"), "release", "review", loader, preflight
        ) == {"checks": []}
    assert events == [
        ("preflight", Path("/recipes"), "release"),
        "enter",
        ("store", Path("/transient") / "owner-state"),
        ("capture", "ephemeral-store"),
        ("materialize", "ephemeral-store"),
        ("loader", source_path, "review"),
        "exit",
    ]
    assert temporary_calls == [{"prefix": "lockstep-dryrun-"}]


def test_dryrun_preflight_failure_creates_no_ephemeral_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dryrun = importlib.import_module("lockstep.mcp._scenario_dryrun")
    calls = []
    sentinel = RuntimeError("preflight rejected")

    def preflight(actual_recipes, actual_recipe):
        calls.append((actual_recipes, actual_recipe))
        raise sentinel

    monkeypatch.setattr(
        dryrun.tempfile,
        "TemporaryDirectory",
        lambda **_kwargs: pytest.fail("preflight failure created a temp directory"),
    )
    monkeypatch.setattr(
        dryrun,
        "RecipeBundleStore",
        lambda *_args: pytest.fail("preflight failure created a bundle store"),
    )
    with pytest.raises(RuntimeError, match="preflight rejected") as raised:
        dryrun._materialize_brief(
            Path("/recipes"), "release", "review", object(), preflight
        )
    assert raised.value is sentinel
    assert calls == [(Path("/recipes"), "release")]


@pytest.mark.parametrize(
    ("action", "values", "mode"),
    (
        ("start", {"recipe": "release", "input": '{"x": 1}'}, "command"),
        ("status", {"run_id": "run-1"}, "observe"),
        ("done", {"run_id": "run-1", "step": "review", "evidence": '{"ok": true}', "session_id": "s-1"}, "command"),
        ("escalate", {"run_id": "run-1", "reason": "blocked", "session_id": "s-1"}, "command"),
        ("abort", {"run_id": "run-1", "session_id": "s-1"}, "command"),
        ("wait", {"run_id": "run-1", "timeout": 7}, "observe"),
        ("history", {"run_id": "run-1"}, "observe"),
        ("events", {"run_id": "run-1"}, "observe"),
        ("recover", {"limit": 9}, "command"),
    ),
)
def test_scenario_cli_dispatch_preserves_engine_mode_and_arguments(
    action: str,
    values: dict[str, object],
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario = importlib.import_module("lockstep._cli_scenario")
    runtime_engine = importlib.import_module("lockstep.runtime.engine")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    calls = []
    owner_state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    monkeypatch.setattr(scenario, "state_dir", lambda: owner_state)

    class FakeEngine:
        def __getattr__(self, name):
            def invoke(*args, **kwargs):
                calls.append((name, args, kwargs))
                return {"action": action}

            return invoke

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(
        runtime_engine.Engine,
        "command",
        lambda *args: calls.append(("factory", "command", args)) or FakeEngine(),
    )
    monkeypatch.setattr(
        runtime_engine.Engine,
        "observe",
        lambda *args: calls.append(("factory", "observe", args)) or FakeEngine(),
    )
    args = SimpleNamespace(action=action, **values)
    assert scenario.run_scenario_command(args) == 0
    project_text = str(project)
    expected_calls = {
        "start": ("start", ("release", {"x": 1}, project_text), {}),
        "status": ("status", ("run-1", project_text), {}),
        "done": ("done", ("run-1", "review", {"ok": True}), {"session_id": "s-1", "project": project_text}),
        "escalate": ("escalate", ("run-1", "blocked"), {"session_id": "s-1", "project": project_text}),
        "abort": ("abort", ("run-1",), {"session_id": "s-1", "project": project_text}),
        "wait": ("wait", ("run-1", 7, project_text), {}),
        "history": ("history", ("run-1", project_text), {}),
        "events": ("events", ("run-1", project_text), {}),
        "recover": ("scenario_recover", (project_text,), {"limit": 9}),
    }
    assert calls == [
        ("factory", mode, (owner_state, recipes)),
        expected_calls[action],
        ("close",),
    ]
    assert json.loads(capsys.readouterr().out) == {"action": action}


@pytest.mark.parametrize(
    ("action", "values", "mode"),
    (
        ("start", {"recipe": "release", "input": "{}"}, "command"),
        ("status", {"run_id": "run-1"}, "observe"),
        ("done", {"run_id": "run-1", "step": "review", "evidence": "{}", "session_id": "s-1"}, "command"),
        ("escalate", {"run_id": "run-1", "reason": "blocked", "session_id": "s-1"}, "command"),
        ("abort", {"run_id": "run-1", "session_id": "s-1"}, "command"),
        ("wait", {"run_id": "run-1", "timeout": 7}, "observe"),
        ("history", {"run_id": "run-1"}, "observe"),
        ("events", {"run_id": "run-1"}, "observe"),
        ("recover", {"limit": 9}, "command"),
    ),
)
def test_scenario_cli_always_closes_engine_after_failure(
    action: str,
    values: dict[str, object],
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario = importlib.import_module("lockstep._cli_scenario")
    runtime_engine = importlib.import_module("lockstep.runtime.engine")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    owner_state = tmp_path / "owner-state"
    monkeypatch.setattr(scenario, "state_dir", lambda: owner_state)
    calls = []
    sentinel = RuntimeError("scenario failure")

    class FailingEngine:
        def __getattr__(self, name):
            def fail(*_args, **_kwargs):
                calls.append(("invoke", name))
                raise sentinel

            return fail

        def close(self):
            calls.append(("close",))

    factory = lambda *_args: calls.append(("factory", mode)) or FailingEngine()
    monkeypatch.setattr(runtime_engine.Engine, mode, factory)
    with pytest.raises(RuntimeError, match="scenario failure") as raised:
        scenario.run_scenario_command(SimpleNamespace(action=action, **values))
    assert raised.value is sentinel
    invoked = "scenario_recover" if action == "recover" else action
    assert calls == [("factory", mode), ("invoke", invoked), ("close",)]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("action", "values"),
    (
        ("start", {"recipe": "release", "input": "{"}),
        ("done", {"run_id": "run-1", "step": "review", "evidence": "{", "session_id": "s-1"}),
    ),
)
def test_scenario_cli_closes_engine_when_json_decode_fails(
    action: str,
    values: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario = importlib.import_module("lockstep._cli_scenario")
    runtime_engine = importlib.import_module("lockstep.runtime.engine")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    owner_state = tmp_path / "owner-state"
    monkeypatch.setattr(scenario, "state_dir", lambda: owner_state)
    calls = []

    class Engine:
        def __getattr__(self, name):
            def forbidden(*_args, **_kwargs):
                calls.append(("unexpected", name))

            return forbidden

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(
        runtime_engine.Engine,
        "command",
        lambda *_args: calls.append(("factory",)) or Engine(),
    )
    with pytest.raises(AuthoringError, match="must be JSON"):
        scenario.run_scenario_command(SimpleNamespace(action=action, **values))
    assert calls == [("factory",), ("close",)]
    assert capsys.readouterr().out == ""
