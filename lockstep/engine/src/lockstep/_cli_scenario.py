"""Stateless CLI adaptation for public scenario service operations."""

from __future__ import annotations

import argparse

from lockstep import _cli_support as support
from lockstep.runtime.config import state_dir


def _decode_object(raw: str, label: str) -> dict:
    return support.decode_object(raw, label)


def _observation_result(engine: object, args: argparse.Namespace, project: str) -> object:
    if args.action == "status":
        return engine.status(args.run_id, project)
    if args.action == "wait":
        return engine.wait(args.run_id, args.timeout, project)
    if args.action == "history":
        return engine.history(args.run_id, project)
    if args.action == "events":
        return engine.events(args.run_id, project)
    raise support.AuthoringError("unknown scenario action")


def _command_result(engine: object, args: argparse.Namespace, project: str) -> object:
    if args.action == "start":
        return engine.start(args.recipe, _decode_object(args.input, "input"), project)
    if args.action == "done":
        return engine.done(
            args.run_id,
            args.step,
            _decode_object(args.evidence, "evidence"),
            session_id=args.session_id,
            project=project,
        )
    if args.action == "escalate":
        return engine.escalate(
            args.run_id,
            args.reason,
            session_id=args.session_id,
            project=project,
        )
    if args.action == "abort":
        return engine.abort(
            args.run_id,
            session_id=args.session_id,
            project=project,
        )
    if args.action == "recover":
        return engine.scenario_recover(project, limit=args.limit)
    raise support.AuthoringError("unknown scenario action")


def run_scenario_command(args: argparse.Namespace) -> int:
    from lockstep.runtime import engine as engine_module

    project = support.current_project()
    recipes = project / ".lockstep" / "recipes"
    observing = args.action in {"status", "wait", "history", "events"}
    engine = (
        engine_module.Engine.observe(state_dir(), recipes)
        if observing
        else engine_module.Engine.command(state_dir(), recipes)
    )
    try:
        result = (
            _observation_result(engine, args, str(project))
            if observing
            else _command_result(engine, args, str(project))
        )
        support.write_json(result)
        return 0
    finally:
        engine.close()
