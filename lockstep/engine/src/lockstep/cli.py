"""Console argument parsing and stdin/stdout adapters for lockstep."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from lockstep import __version__
from lockstep.authoring import (
    AuthoringError,
    check_recipe,
    diff_recipe,
    estimate_recipe,
    initialize_minimal,
    json_text,
    project_paths,
    render_recipe,
    write_compilation,
)
from lockstep.runtime.config import recipes_dir, state_dir
from lockstep.runtime.hooks import (
    doctor,
    hook_posttool,
    hook_pretool,
    hook_session_start,
    hook_stop,
    policy_clear,
    policy_require,
)


def _read_stdin_json() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - hook stdin must fail open at the adapter
        return {}


def _cmd_serve(args: argparse.Namespace) -> int:
    from lockstep.mcp.server import app

    app.run()
    return 0


def _cmd_hook_stop(args: argparse.Namespace) -> int:
    stdin_json = _read_stdin_json()
    _code, out = hook_stop(stdin_json, state_dir(), stdin_json.get("cwd") or os.getcwd())
    if out:
        sys.stdout.write(out)
    return 0


def _cmd_hook_session_start(args: argparse.Namespace) -> int:
    stdin_json = _read_stdin_json()
    text = hook_session_start(state_dir(), stdin_json.get("cwd") or os.getcwd())
    if text:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    return 0


def _cmd_hook_pretool(args: argparse.Namespace) -> int:
    _code, out = hook_pretool(_read_stdin_json(), state_dir())
    if out:
        sys.stdout.write(out)
    return 0


def _cmd_hook_posttool(args: argparse.Namespace) -> int:
    hook_posttool(_read_stdin_json(), state_dir())
    return 0


def _cmd_policy(args: argparse.Namespace) -> int:
    if args.action == "require":
        policy_require(state_dir(), args.project, args.recipe)
    elif args.action == "clear":
        policy_clear(state_dir(), args.project)
    else:
        print("usage: lockstep policy require --project PATH --recipe NAME")
        print("       lockstep policy clear --project PATH")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    ok, report = doctor(state_dir(), recipes_dir())
    print(report)
    return 0 if ok else 1


def _cmd_recipe(args: argparse.Namespace) -> int:
    project = Path.cwd()
    if args.action == "init":
        initialize_minimal(project, args.name)
        print(f"initialized {args.name}")
        return 0
    if args.action == "compile":
        write_compilation(project_paths(project, args.name))
        print(f"compiled {args.name}")
        return 0
    if args.action == "check":
        if args.name is None and not args.all:
            raise AuthoringError("recipe check requires a name or --all")
        names = [args.name] if args.name else sorted(
            path.name.removesuffix(".recipe.yaml")
            for path in (project / ".lockstep" / "recipes").glob("*.recipe.yaml")
        )
        if not names:
            raise AuthoringError("no recipes found")
        failed = False
        for name in names:
            result = check_recipe(project, name)
            failed = failed or not bool(result["ok"])
            sys.stdout.write(json_text({"name": name, **result}))
        return 1 if failed else 0
    if args.action == "diff":
        sys.stdout.write(diff_recipe(project, args.name))
        return 0
    if args.action == "render":
        sys.stdout.write(render_recipe(project, args.name, args.view))
        return 0
    if args.action == "estimate":
        # The stable JSON schema is also the human-readable representation in
        # v1; --json is retained so callers can request that contract explicitly.
        del args.json
        sys.stdout.write(json_text(estimate_recipe(project, args.name)))
        return 0
    raise AuthoringError("unknown recipe action")


def _cmd_template(args: argparse.Namespace) -> int:
    from lockstep.templates import install_template, list_templates, show_template

    if args.action == "list":
        for name in list_templates():
            print(name)
        return 0
    if args.action == "show":
        sys.stdout.write(json_text(show_template(args.template, args.name).to_dict()))
        return 0
    if args.action == "init":
        install_template(args.template, args.name, Path.cwd())
        print(f"initialized {args.name}")
        return 0
    raise AuthoringError("unknown template action")


def _decode_object(raw: str, label: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuthoringError(f"{label} must be JSON") from exc
    if not isinstance(value, dict):
        raise AuthoringError(f"{label} must be a JSON object")
    return value


def _cmd_scenario(args: argparse.Namespace) -> int:
    from lockstep.runtime.engine import Engine

    project = Path.cwd().resolve()
    engine = Engine(state_dir(), project / ".lockstep" / "recipes")
    try:
        if args.action == "start":
            result = engine.start(
                args.recipe, _decode_object(args.input, "input"), str(project)
            )
        elif args.action == "status":
            result = engine.scenario_status(args.run_id, str(project))
        elif args.action == "done":
            result = engine.done(
                args.run_id,
                args.step,
                _decode_object(args.evidence, "evidence"),
                session_id=args.session_id,
                project=str(project),
            )
        elif args.action == "escalate":
            result = engine.escalate(
                args.run_id,
                args.reason,
                session_id=args.session_id,
                project=str(project),
            )
        elif args.action == "abort":
            result = engine.abort(
                args.run_id, session_id=args.session_id, project=str(project)
            )
        elif args.action == "wait":
            result = engine.scenario_wait(args.run_id, args.timeout, str(project))
        elif args.action == "history":
            result = engine.scenario_history(args.run_id, str(project))
        elif args.action == "events":
            result = engine.scenario_events(args.run_id, str(project))
        elif args.action == "recover":
            result = engine.scenario_recover(str(project), limit=args.limit)
        else:
            raise AuthoringError("unknown scenario action")
        sys.stdout.write(json_text(result))
        return 0
    finally:
        engine.close()


_HANDLERS = {"serve": _cmd_serve, "hook-stop": _cmd_hook_stop, "hook-session-start": _cmd_hook_session_start, "hook-pretool": _cmd_hook_pretool, "hook-posttool": _cmd_hook_posttool, "policy": _cmd_policy, "doctor": _cmd_doctor, "recipe": _cmd_recipe, "template": _cmd_template, "scenario": _cmd_scenario}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lockstep")
    parser.add_argument("--version", action="store_true", help="print the installed version and exit")
    sub = parser.add_subparsers(dest="verb")
    for verb in _HANDLERS:
        if verb == "policy":
            policy = sub.add_parser("policy").add_subparsers(dest="action")
            require = policy.add_parser("require")
            require.add_argument("--project", required=True)
            require.add_argument("--recipe", required=True)
            clear = policy.add_parser("clear")
            clear.add_argument("--project", required=True)
        elif verb == "recipe":
            recipe = sub.add_parser("recipe").add_subparsers(dest="action", required=True)
            init = recipe.add_parser("init")
            init.add_argument("name")
            compile_cmd = recipe.add_parser("compile")
            compile_cmd.add_argument("name")
            check = recipe.add_parser("check")
            check.add_argument("name", nargs="?")
            check.add_argument("--all", action="store_true")
            diff = recipe.add_parser("diff")
            diff.add_argument("name")
            render = recipe.add_parser("render")
            render.add_argument("name")
            render.add_argument("--view", choices=("workflow", "generated"), required=True)
            estimate = recipe.add_parser("estimate")
            estimate.add_argument("name")
            estimate.add_argument("--json", action="store_true")
        elif verb == "template":
            template = sub.add_parser("template").add_subparsers(dest="action", required=True)
            template.add_parser("list")
            show = template.add_parser("show")
            show.add_argument("template")
            show.add_argument("name")
            init = template.add_parser("init")
            init.add_argument("template")
            init.add_argument("name")
        elif verb == "scenario":
            scenario = sub.add_parser("scenario").add_subparsers(dest="action", required=True)
            start = scenario.add_parser("start")
            start.add_argument("recipe")
            start.add_argument("--input", default="{}")
            status = scenario.add_parser("status")
            status.add_argument("run_id")
            done = scenario.add_parser("done")
            done.add_argument("run_id")
            done.add_argument("step")
            done.add_argument("--evidence", default="{}")
            done.add_argument("--session-id")
            escalate = scenario.add_parser("escalate")
            escalate.add_argument("run_id")
            escalate.add_argument("reason")
            escalate.add_argument("--session-id")
            abort = scenario.add_parser("abort")
            abort.add_argument("run_id")
            abort.add_argument("--session-id")
            wait = scenario.add_parser("wait")
            wait.add_argument("run_id")
            wait.add_argument("--timeout", type=int, default=30)
            history = scenario.add_parser("history")
            history.add_argument("run_id")
            events = scenario.add_parser("events")
            events.add_argument("run_id")
            recover = scenario.add_parser("recover")
            recover.add_argument("--limit", type=int, default=128)
        else:
            sub.add_parser(verb)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args, _unknown = parser.parse_known_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.verb is None:
        parser.error("the following arguments are required: verb")
    try:
        return _HANDLERS[args.verb](args)
    except (OSError, ValueError, RuntimeError, AuthoringError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
