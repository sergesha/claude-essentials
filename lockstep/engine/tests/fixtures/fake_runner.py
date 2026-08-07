"""Fake CLI agent. Usage: fake_runner.py --mode ok|fail [--write PATH] [--sleep N]

Accepts the exact argv shape ``runners.build_argv`` produces: ``-p`` is a
FLAG (print mode) and the prompt is a positional behind ``--`` — an
optional-with-value ``-p`` would choke on the following ``--output-format``
token and could never be driven by the real argv builder.
"""
import argparse
import json
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="ok")
    ap.add_argument("--write", default=None)
    ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("-p", dest="print_mode", action="store_true")
    ap.add_argument("--output-format", default="json")
    ap.add_argument("--model", default=None)
    ap.add_argument("--resume", default=None)
    ap.add_argument("prompt", nargs="?", default="")
    args, _unknown = ap.parse_known_args()
    if args.sleep:
        time.sleep(args.sleep)
    if args.write:
        Path(args.write).parent.mkdir(parents=True, exist_ok=True)
        Path(args.write).write_text("Verdict: PASS\n")
    if args.mode == "fail":
        print("boom", file=sys.stderr)
        return 3
    print(json.dumps({"session_id": "fake-session-1", "result": f"done:{args.prompt[:20]}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
