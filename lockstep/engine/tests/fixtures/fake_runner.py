"""Fake CLI agent accepting the exact Claude and Codex runner grammars."""
import argparse
import json
import sys
import time
from pathlib import Path


def _test_controls(argv: list[str]):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--mode", default="ok")
    ap.add_argument("--write", default=None)
    ap.add_argument("--sleep", type=float, default=0.0)
    return ap.parse_known_args(argv)


def _parse_agent_argv(argv: list[str]) -> tuple[str, str | None, str]:
    codex_overrides = []
    while len(argv) >= 2 and argv[0] == "-c":
        codex_overrides.append(argv[1])
        argv = argv[2:]
    if argv and argv[0] == "exec":
        if any(not value.startswith("mcp_servers.lockstep.command=") for value in codex_overrides):
            raise SystemExit(2)
        ap = argparse.ArgumentParser()
        ap.add_argument("command")
        ap.add_argument("--json", action="store_true")
        ap.add_argument("--sandbox", required=True)
        ap.add_argument("--model", required=True)
        ap.add_argument("prompt")
        args = ap.parse_args(argv)
        if args.command != "exec" or not args.json or args.sandbox != "workspace-write":
            raise SystemExit(2)
        return "codex", args.model, args.prompt

    ap = argparse.ArgumentParser()
    ap.add_argument("-p", dest="print_mode", action="store_true")
    ap.add_argument("--output-format", default="json")
    ap.add_argument("--model", required=True)
    ap.add_argument("--resume", default=None)
    ap.add_argument("prompt")
    args = ap.parse_args(argv)
    if not args.print_mode or args.output_format != "json":
        raise SystemExit(2)
    return "claude", args.model, args.prompt


def main() -> int:
    controls, agent_argv = _test_controls(sys.argv[1:])
    driver, _model, prompt = _parse_agent_argv(agent_argv)
    if controls.sleep:
        time.sleep(controls.sleep)
    if controls.write:
        Path(controls.write).parent.mkdir(parents=True, exist_ok=True)
        Path(controls.write).write_text("Verdict: PASS\n")
    if controls.mode == "fail":
        print("boom", file=sys.stderr)
        return 3
    if driver == "codex":
        print(json.dumps({"type": "thread.started", "thread_id": "fake-thread-1"}))
        print(json.dumps({"type": "turn.completed", "result": f"done:{prompt[:20]}"}))
    else:
        print(json.dumps({"session_id": "fake-session-1", "result": f"done:{prompt[:20]}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
