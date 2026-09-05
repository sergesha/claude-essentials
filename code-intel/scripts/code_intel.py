#!/usr/bin/env python3
"""Shared code intelligence controller (Python 3.11+, macOS and Linux)."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Sequence

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    executable: str
    version: str
    mise_package: str


TOOLS: dict[str, ToolSpec] = {
    "codegraph": ToolSpec(
        "codegraph", "codegraph", "1.6.0", "npm:@colbymchenry/codegraph@1.6.0"
    ),
    "crg": ToolSpec(
        "crg", "code-review-graph", "2.3.8", "pipx:code-review-graph@2.3.8"
    ),
}


class UserError(Exception):
    """An actionable failure that the CLI reports without a traceback."""


def _group_running(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS may briefly deny probing an orphan while launchd reaps it.
        # Keep waiting; permission denial is not proof of exit.
        return True
    if sys.platform.startswith("linux"):
        # An orphan can remain a zombie under a container's non-reaping PID 1.
        # Zombies have exited and cannot write; do not wait for PID 1 to reap them.
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                # hidepid=1 can expose unrelated PID directories but deny stat.
                # Skip only proven nonmembers or PIDs that have since exited.
                try:
                    member_group = os.getpgid(int(entry.name))
                except ProcessLookupError:
                    continue
                except PermissionError:
                    return True
                if member_group == pgid:
                    return True
                continue
            if int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
                return True
        return False
    return True


def _kill_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _wait_group_exit(process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 2
    while _group_running(process.pid):
        if time.monotonic() >= deadline:
            raise UserError("Child process group did not exit after termination.")
        time.sleep(0.01)


def run_child(
    argv: Sequence[str], *, cwd: Path | None, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run an argv-only child; finish all group members before returning.

    MCP servers must use execv instead: this runner captures command output.
    Supported platforms are macOS and Linux; unsupported systems fail closed.
    """
    if isinstance(argv, (str, bytes)) or not argv or not all(
        isinstance(arg, str) and "\0" not in arg for arg in argv
    ):
        raise UserError("Child command must be a nonempty array of strings.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise UserError("Child command deadline has expired.")
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise UserError("Process supervision requires macOS or Linux.")
    args = list(argv)
    try:
        process = subprocess.Popen(
            args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise UserError(f"Cannot start {args[0]}: {exc}") from exc
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(process)
        stdout, stderr = process.communicate()
    finally:
        # A successful parent may have left a background index writer behind.
        _kill_group(process)
        process.wait()
        _wait_group_exit(process)
    if timed_out:
        raise UserError(f"{args[0]} timed out after {timeout:g}s.")
    if process.returncode:
        detail = " ".join((stderr or stdout).split())[:500]
        raise UserError(
            f"{args[0]} exited with status {process.returncode}"
            + (f": {detail}" if detail else ".")
        )
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def resolve_verified_tool(spec: ToolSpec, *, deadline: float) -> Path:
    binary = shutil.which(spec.executable)
    if not binary:
        shim_dir = Path.home() / ".local/share/mise/shims"
        binary = shutil.which(spec.executable, path=str(shim_dir))
    remedy = "Run install-tools explicitly, then start a new host session."
    if not binary:
        raise UserError(f"Missing {spec.executable} {spec.version}. {remedy}")
    # Keep the shim name: resolving its symlink would change argv[0] to mise.
    path = Path(binary).absolute()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise UserError(f"Deadline expired checking {spec.executable}. {remedy}")
    try:
        result = run_child([str(path), "--version"], cwd=None, timeout=remaining)
    except UserError as exc:
        raise UserError(f"Cannot verify {spec.executable}: {exc} {remedy}") from exc
    match = re.fullmatch(
        rf"(?:{re.escape(spec.executable)} )?(\d+\.\d+\.\d+)", result.stdout.strip()
    )
    if not match:
        raise UserError(f"Unparseable {spec.executable} version. Expected {spec.version}. {remedy}")
    if match[1] != spec.version:
        raise UserError(
            f"{spec.executable} version {match[1]} does not match required {spec.version}. {remedy}"
        )
    return path


def install_tools() -> int:
    mise = shutil.which("mise")
    if not mise:
        raise UserError("Install mise, then invoke install-tools explicitly.")
    for spec in TOOLS.values():
        run_child([mise, "use", "-g", spec.mise_package], cwd=None, timeout=300)
    for spec in TOOLS.values():
        resolve_verified_tool(spec, deadline=time.monotonic() + 10)
    return 0


def serve(engine: str) -> int:
    binary = str(resolve_verified_tool(TOOLS[engine], deadline=time.monotonic() + 10))
    args = [binary, "serve"] + (["--mcp"] if engine == "codegraph" else [])
    os.execv(binary, args)
    return 0


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UserError(message)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install-tools", help="Explicitly install the tested tool pins")
    server = commands.add_parser("serve", help="Launch a verified MCP server")
    server.add_argument("engine", choices=TOOLS)
    try:
        args = parser.parse_args(argv)
        if args.command == "install-tools":
            return install_tools()
        return serve(args.engine)
    except (UserError, OSError) as exc:
        print("code-intel: " + " ".join(str(exc).split()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
