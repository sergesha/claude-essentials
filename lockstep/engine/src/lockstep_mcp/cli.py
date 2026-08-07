"""Task 6 base: `main()` argparse-routes the console-script verbs. `serve`
(the default when no verb is given) runs the FastMCP app over stdio.
`hook-stop`, `hook-session-start`, `hook-pretool`, `policy`, `doctor` are
exit-0 stubs here — Task 7 fills their real behavior (hook handlers,
policy require/clear, doctor diagnostics). The verb-routing test in
`tests/test_cli.py` asserts DISPATCH (the right handler runs for each
named verb, `serve` is the default), not that this verb set is closed —
Task 7 is free to add subcommands under `policy`/`doctor` without breaking
this contract.
"""

from __future__ import annotations

import argparse
import sys


def _cmd_serve(args: argparse.Namespace) -> int:
    from lockstep_mcp.server import app

    app.run()
    return 0


def _cmd_stub(args: argparse.Namespace) -> int:
    """Task 7 replaces this per-verb. Exit 0: a stub verb must never look
    like a failure to whatever invoked the CLI (a hook, a human)."""
    return 0


_HANDLERS = {
    "serve": _cmd_serve,
    "hook-stop": _cmd_stub,
    "hook-session-start": _cmd_stub,
    "hook-pretool": _cmd_stub,
    "policy": _cmd_stub,
    "doctor": _cmd_stub,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lockstep-mcp")
    sub = parser.add_subparsers(dest="verb")
    for verb in _HANDLERS:
        sub.add_parser(verb)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args, _unknown = parser.parse_known_args(argv)
    verb = args.verb or "serve"
    return _HANDLERS[verb](args)


if __name__ == "__main__":
    sys.exit(main())
