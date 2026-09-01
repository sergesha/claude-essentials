"""Construction of the frozen public Lockstep command grammar."""

from __future__ import annotations

import argparse


def _add_policy_parser(sub: argparse._SubParsersAction) -> None:
    policy = sub.add_parser("policy").add_subparsers(dest="action")
    require = policy.add_parser("require")
    require.add_argument("--project", required=True)
    require.add_argument("--recipe", required=True)
    clear = policy.add_parser("clear")
    clear.add_argument("--project", required=True)


def _add_recipe_parser(sub: argparse._SubParsersAction) -> None:
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


def _add_template_parser(sub: argparse._SubParsersAction) -> None:
    template = sub.add_parser("template").add_subparsers(
        dest="action", required=True
    )
    template.add_parser("list")
    show = template.add_parser("show")
    show.add_argument("template")
    show.add_argument("name")
    init = template.add_parser("init")
    init.add_argument("template")
    init.add_argument("name")


def _add_scenario_parser(sub: argparse._SubParsersAction) -> None:
    scenario = sub.add_parser("scenario").add_subparsers(
        dest="action", required=True
    )
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


def _add_consent_parser(sub: argparse._SubParsersAction) -> None:
    consent = sub.add_parser("consent").add_subparsers(
        dest="action", required=True
    )
    issue = consent.add_parser("issue")
    issue.add_argument("--run", dest="run_id", required=True)
    issue.add_argument("--step", required=True)
    consent.add_parser("accept")
    consent.add_parser("revoke")


def _add_owner_parser(sub: argparse._SubParsersAction) -> None:
    owner = sub.add_parser("owner").add_subparsers(dest="action", required=True)
    listing = owner.add_parser("list-runtime-requirements")
    listing.add_argument("--project", required=True)
    listing.add_argument("--recipe", action="append", required=True)
    provision = owner.add_parser("provision-runtime")
    provision.add_argument("--config", required=True)
    provision.add_argument("--project", required=True)
    provision.add_argument("--recipe", action="append", required=True)
    provision.add_argument("--replace-grants", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lockstep")
    parser.add_argument(
        "--version", action="store_true", help="print the installed version and exit"
    )
    sub = parser.add_subparsers(dest="verb")
    for verb in (
        "serve",
        "hook-stop",
        "hook-session-start",
        "hook-pretool",
        "hook-posttool",
    ):
        sub.add_parser(verb)
    _add_policy_parser(sub)
    sub.add_parser("doctor")
    _add_recipe_parser(sub)
    _add_template_parser(sub)
    _add_scenario_parser(sub)
    _add_consent_parser(sub)
    _add_owner_parser(sub)
    return parser
