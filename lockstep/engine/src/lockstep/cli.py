"""Console argument parsing and stdin/stdout adapters for lockstep."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from lockstep import __version__
from lockstep._cli_consent import run_consent_command as _cmd_consent
from lockstep._cli_parser import build_parser as _build_parser
from lockstep._cli_scenario import run_scenario_command as _cmd_scenario
from lockstep.errors import AuthoringError
from lockstep.runtime.config import recipes_dir, state_dir

CliError = AuthoringError


def json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n"


def _read_stdin_json() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - hook stdin must fail open at the adapter
        return {}


def _read_owner_input(path_value: str, *, label: str, max_bytes: int) -> bytes:
    from lockstep.runtime.bounded_files import read_bounded_regular_file

    path = Path(path_value)
    error = f"{label} must be an absolute existing regular non-symlink file"
    if not path.is_absolute() or path.is_symlink():
        raise CliError(error)
    try:
        data = read_bounded_regular_file(
            path,
            max_bytes=max_bytes,
            label=label,
        )
    except ValueError as exc:
        if str(exc) == f"{label} exceeds {max_bytes} bytes":
            raise CliError(str(exc)) from exc
        raise CliError(error) from exc
    except OSError as exc:
        raise CliError(error) from exc
    if data is None:  # missing_ok is false; keep the adapter total for typing.
        raise CliError(error)
    return data


def _cmd_serve(args: argparse.Namespace) -> int:
    from lockstep.mcp.server import app

    app.run()
    return 0


def _cmd_hook_stop(args: argparse.Namespace) -> int:
    from lockstep.runtime.hooks import hook_stop

    stdin_json = _read_stdin_json()
    _code, out = hook_stop(stdin_json, state_dir(), stdin_json.get("cwd") or os.getcwd())
    if out:
        sys.stdout.write(out)
    return 0


def _cmd_hook_session_start(args: argparse.Namespace) -> int:
    from lockstep.runtime.hooks import hook_session_start

    stdin_json = _read_stdin_json()
    text = hook_session_start(state_dir(), stdin_json.get("cwd") or os.getcwd())
    if text:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    return 0


def _cmd_hook_pretool(args: argparse.Namespace) -> int:
    from lockstep.runtime.hooks import hook_pretool

    _code, out = hook_pretool(_read_stdin_json(), state_dir())
    if out:
        sys.stdout.write(out)
    return 0


def _cmd_hook_posttool(args: argparse.Namespace) -> int:
    from lockstep.runtime.hooks import hook_posttool

    hook_posttool(_read_stdin_json(), state_dir())
    return 0


def _cmd_policy(args: argparse.Namespace) -> int:
    from lockstep.runtime.hooks import policy_clear, policy_require

    if args.action == "require":
        policy_require(state_dir(), args.project, args.recipe)
    elif args.action == "clear":
        policy_clear(state_dir(), args.project)
    else:
        print("usage: lockstep policy require --project PATH --recipe NAME")
        print("       lockstep policy clear --project PATH")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from lockstep.runtime.hooks import doctor

    ok, report = doctor(state_dir(), recipes_dir())
    print(report)
    return 0 if ok else 1


def _cmd_recipe(args: argparse.Namespace) -> int:
    from lockstep.authoring import (
        check_all_recovered_recipes,
        check_recovered_recipe,
        diff_recovered_recipe,
        estimate_recipe,
        initialize_minimal,
        publish_project_compilation,
        render_recipe,
    )
    from lockstep.authoring_publisher import observe_authoring_project

    project = Path.cwd()
    if args.action == "init":
        initialize_minimal(
            project, args.name, state_dir=state_dir().absolute()
        )
        print(f"initialized {args.name}")
        return 0
    if args.action == "compile":
        publish_project_compilation(
            project, args.name, state_dir=state_dir().absolute()
        )
        print(f"compiled {args.name}")
        return 0
    if args.action == "check":
        if args.name is None and not args.all:
            raise CliError("recipe check requires a name or --all")
        if args.name is not None:
            result = check_recovered_recipe(
                project, args.name, state_dir=state_dir().absolute()
            )
            results = ((args.name, result),)
        else:
            results = check_all_recovered_recipes(
                project, state_dir=state_dir().absolute()
            )
        failed = False
        for name, result in results:
            failed = failed or not bool(result["ok"])
            sys.stdout.write(json_text({"name": name, **result}))
        return 1 if failed else 0
    if args.action == "diff":
        sys.stdout.write(
            diff_recovered_recipe(
                project, args.name, state_dir=state_dir().absolute()
            )
        )
        return 0
    if args.action == "render":
        sys.stdout.write(
            observe_authoring_project(
                state_dir().absolute(),
                project,
                lambda: render_recipe(project, args.name, args.view),
            )
        )
        return 0
    if args.action == "estimate":
        # The stable JSON schema is also the human-readable representation in
        # v1; --json is retained so callers can request that contract explicitly.
        del args.json
        result = observe_authoring_project(
            state_dir().absolute(),
            project,
            lambda: estimate_recipe(project, args.name),
        )
        sys.stdout.write(json_text(result))
        return 0
    raise CliError("unknown recipe action")


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
        install_template(
            args.template,
            args.name,
            Path.cwd(),
            state_dir=state_dir().absolute(),
        )
        print(f"initialized {args.name}")
        return 0
    raise CliError("unknown template action")


def _cmd_owner(args: argparse.Namespace) -> int:
    if args.action == "list-runtime-requirements":
        from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
        from lockstep.runtime.service import preflight_recipe

        project = Path(args.project).resolve()
        recipes = project / ".lockstep" / "recipes"
        index = RuntimeRequirementIndex.for_authorized_closures(
            tuple(preflight_recipe(recipes, name) for name in args.recipe),
            project_identity=str(project),
        )
        sys.stdout.write(json_text(index.listing_document()))
        return 0
    if args.action == "provision-runtime":
        from lockstep.runtime.effects.owner_policy_ingress import (
            parse_runtime_provision_documents,
        )

        config_bytes = _read_owner_input(
            args.config,
            label="runtime provision config",
            max_bytes=64 * 1024,
        )
        replacement_bytes = _read_owner_input(
            args.replace_grants,
            label="runtime replacement grants",
            max_bytes=512 * 1024,
        )
        codex, pinned, replacement_keys = parse_runtime_provision_documents(
            config_bytes,
            replacement_bytes,
        )
        from lockstep.runtime.effects.owner_snapshot_file import (
            preflight_runtime_snapshot_file,
        )

        preflight_runtime_snapshot_file(state_dir())
        from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
        from lockstep.runtime.effects.owner_provisioning import (
            provision_runtime_snapshot,
        )
        from lockstep.runtime.service import preflight_recipe

        project = Path(args.project).resolve(strict=True)
        recipes = project / ".lockstep" / "recipes"
        index = RuntimeRequirementIndex.for_authorized_closures(
            tuple(preflight_recipe(recipes, name) for name in args.recipe),
            project_identity=str(project),
        )
        provision_runtime_snapshot(
            state_dir=state_dir(),
            codex=codex,
            pinned=pinned,
            replacement_keys=replacement_keys,
            index=index,
            project=project,
        )
        return 0
    raise CliError("unknown owner action")


_HANDLERS = {
    "serve": _cmd_serve,
    "hook-stop": _cmd_hook_stop,
    "hook-session-start": _cmd_hook_session_start,
    "hook-pretool": _cmd_hook_pretool,
    "hook-posttool": _cmd_hook_posttool,
    "policy": _cmd_policy,
    "doctor": _cmd_doctor,
    "recipe": _cmd_recipe,
    "template": _cmd_template,
    "scenario": _cmd_scenario,
    "consent": _cmd_consent,
    "owner": _cmd_owner,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.verb is None:
        parser.error("the following arguments are required: verb")
    try:
        return _HANDLERS[args.verb](args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
