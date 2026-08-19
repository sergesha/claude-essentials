"""Console argument parsing and stdin/stdout adapters for lockstep."""

from __future__ import annotations

import argparse
import json
import os
import sys

from lockstep import __version__
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


_HANDLERS = {"serve": _cmd_serve, "hook-stop": _cmd_hook_stop, "hook-session-start": _cmd_hook_session_start, "hook-pretool": _cmd_hook_pretool, "hook-posttool": _cmd_hook_posttool, "policy": _cmd_policy, "doctor": _cmd_doctor}


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
    return _HANDLERS[args.verb](args)
