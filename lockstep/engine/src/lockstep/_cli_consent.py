"""Stateless CLI adaptation for owner publication consent operations."""

from __future__ import annotations

import argparse

from lockstep import _cli_support as support
from lockstep.runtime.config import state_dir


def _require_owner_tty() -> None:
    support.require_owner_tty()


def _read_consent_token() -> str:
    return support.read_consent_token()


def _issue(engine: object, args: argparse.Namespace, project: str) -> int:
    preview = engine.preview_publication_consent(
        args.run_id, args.step, project=project
    )
    support.write_json(preview)
    expected = str(preview["digest"])
    entered = input("Type the exact commitment digest to issue consent: ")
    if entered != expected:
        raise support.AuthoringError("publication consent issuance cancelled")
    issued = engine.issue_publication_consent(
        args.run_id,
        args.step,
        expected,
        project=project,
    )
    print(issued.token)
    return 0


def _accept(engine: object, project: str) -> int:
    result = engine.scenario_accept_artifact(
        _read_consent_token(), project=project
    )
    support.write_json(result)
    return 0


def _revoke(engine: object, project: str) -> int:
    expected = f"REVOKE {project}"
    entered = input(f"Type {expected!r} to revoke project publication consent: ")
    if entered != expected:
        raise support.AuthoringError("publication consent revocation cancelled")
    epoch = engine.revoke_publication_consents(project=project)
    print(f"publication consent epoch {epoch}")
    return 0


def run_consent_command(args: argparse.Namespace) -> int:
    from lockstep.runtime import engine as engine_module

    if args.action in {"issue", "revoke"}:
        _require_owner_tty()
    project = support.current_project()
    engine = engine_module.Engine.command(
        state_dir(), project / ".lockstep" / "recipes"
    )
    try:
        if args.action == "issue":
            return _issue(engine, args, str(project))
        if args.action == "accept":
            return _accept(engine, str(project))
        if args.action == "revoke":
            return _revoke(engine, str(project))
        raise support.AuthoringError("unknown consent action")
    finally:
        engine.close()
