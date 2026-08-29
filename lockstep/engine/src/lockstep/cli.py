"""Console argument parsing and stdin/stdout adapters for lockstep."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from lockstep import __version__
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


def _decode_object(raw: str, label: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"{label} must be JSON") from exc
    if not isinstance(value, dict):
        raise CliError(f"{label} must be a JSON object")
    return value


def _cmd_scenario(args: argparse.Namespace) -> int:
    from lockstep.runtime.engine import Engine

    project = Path.cwd().resolve()
    recipes = project / ".lockstep" / "recipes"
    engine = (
        Engine.observe(state_dir(), recipes)
        if args.action in {"status", "wait", "history", "events"}
        else Engine.command(state_dir(), recipes)
    )
    try:
        if args.action == "start":
            result = engine.start(
                args.recipe, _decode_object(args.input, "input"), str(project)
            )
        elif args.action == "status":
            result = engine.status(args.run_id, str(project))
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
            result = engine.wait(args.run_id, args.timeout, str(project))
        elif args.action == "history":
            result = engine.history(args.run_id, str(project))
        elif args.action == "events":
            result = engine.events(args.run_id, str(project))
        elif args.action == "recover":
            result = engine.scenario_recover(str(project), limit=args.limit)
        else:
            raise CliError("unknown scenario action")
        sys.stdout.write(json_text(result))
        return 0
    finally:
        engine.close()


def _require_owner_tty() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise CliError("owner consent issuance and revocation require a TTY")


def _read_consent_token() -> str:
    if sys.stdin.isatty():
        token = getpass.getpass("Publication consent token: ")
    else:
        raw = sys.stdin.readline(4098)
        token = raw.rstrip("\r\n")
    if not token:
        raise CliError("publication consent token is required")
    if len(token.encode("utf-8")) > 4096:
        raise CliError("publication consent token is too long")
    return token


def _cmd_consent(args: argparse.Namespace) -> int:
    from lockstep.runtime.engine import Engine

    if args.action in {"issue", "revoke"}:
        _require_owner_tty()
    project = Path.cwd().resolve()
    engine = Engine.command(state_dir(), project / ".lockstep" / "recipes")
    try:
        if args.action == "issue":
            preview = engine.preview_publication_consent(
                args.run_id, args.step, project=str(project)
            )
            sys.stdout.write(json_text(preview))
            expected = str(preview["digest"])
            entered = input("Type the exact commitment digest to issue consent: ")
            if entered != expected:
                raise CliError("publication consent issuance cancelled")
            issued = engine.issue_publication_consent(
                args.run_id,
                args.step,
                expected,
                project=str(project),
            )
            print(issued.token)
            return 0
        if args.action == "accept":
            result = engine.scenario_accept_artifact(
                _read_consent_token(), project=str(project)
            )
            sys.stdout.write(json_text(result))
            return 0
        if args.action == "revoke":
            expected = f"REVOKE {project}"
            entered = input(f"Type {expected!r} to revoke project publication consent: ")
            if entered != expected:
                raise CliError("publication consent revocation cancelled")
            epoch = engine.revoke_publication_consents(project=str(project))
            print(f"publication consent epoch {epoch}")
            return 0
        raise CliError("unknown consent action")
    finally:
        engine.close()


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
        elif verb == "consent":
            consent = sub.add_parser("consent").add_subparsers(
                dest="action", required=True
            )
            issue = consent.add_parser("issue")
            issue.add_argument("--run", dest="run_id", required=True)
            issue.add_argument("--step", required=True)
            consent.add_parser("accept")
            consent.add_parser("revoke")
        elif verb == "owner":
            owner = sub.add_parser("owner").add_subparsers(
                dest="action", required=True
            )
            listing = owner.add_parser("list-runtime-requirements")
            listing.add_argument("--project", required=True)
            listing.add_argument("--recipe", action="append", required=True)
            provision = owner.add_parser("provision-runtime")
            provision.add_argument("--config", required=True)
            provision.add_argument("--project", required=True)
            provision.add_argument("--recipe", action="append", required=True)
            provision.add_argument("--replace-grants", required=True)
        else:
            sub.add_parser(verb)
    return parser


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
