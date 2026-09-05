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
from typing import Literal, Mapping, Sequence

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_DISCOVERY_DIRS = {
    ".git",
    ".codegraph",
    ".code-review-graph",
    "node_modules",
    ".venv",
    "vendor",
    ".cache",
    "__pycache__",
}
AI_MARKERS = ("CLAUDE.md", "AGENTS.md", ".claude", ".codex", ".cursorrules")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    executable: str
    version: str
    mise_package: str


@dataclass(frozen=True)
class RepoScope:
    kind: Literal["repository", "worktree", "umbrella", "none"]
    root: Path
    repositories: tuple[Path, ...]


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


def remaining(deadline: float) -> float:
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise UserError("Operation deadline has expired.")
    return timeout


def _has_git_marker(path: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


def _repository_scope(path: Path, *, deadline: float) -> RepoScope:
    result = run_child(
        [
            "git",
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        ],
        cwd=path,
        timeout=remaining(deadline),
    )
    lines = result.stdout.splitlines()
    if len(lines) != 3 or not all(line.strip() for line in lines):
        raise UserError(f"Cannot resolve Git repository identity at {path}.")
    root, git_dir, common_dir = (Path(line).resolve() for line in lines)
    kind: Literal["repository", "worktree"] = (
        "worktree" if git_dir != common_dir else "repository"
    )
    return RepoScope(kind, root, (root,))


def _has_ai_marker(path: Path) -> bool:
    return any((path / marker).exists() for marker in AI_MARKERS) or (
        path / ".github" / "copilot-instructions.md"
    ).is_file()


def _nested_repositories(path: Path, *, deadline: float) -> tuple[Path, ...]:
    repositories: set[Path] = set()
    for current_name, directories, _files in os.walk(
        path, topdown=True, followlinks=False
    ):
        remaining(deadline)
        current = Path(current_name)
        directories[:] = sorted(
            name
            for name in directories
            if name not in EXCLUDED_DISCOVERY_DIRS
            and not (current / name).is_symlink()
        )
        if current == path or not (current / ".git").exists():
            continue
        repositories.add(_repository_scope(current, deadline=deadline).root)
    return tuple(sorted(repositories))


def discover_scope(path: Path, *, deadline: float) -> RepoScope:
    root = Path(path).expanduser().resolve()
    remaining(deadline)
    if not root.is_dir():
        raise UserError(f"Not a directory: {root}")
    if _has_git_marker(root):
        return _repository_scope(root, deadline=deadline)

    repositories = _nested_repositories(root, deadline=deadline)
    nearby = sum(
        len(repository.relative_to(root).parts) + 1 <= 3
        for repository in repositories
        if repository.is_relative_to(root)
    )
    if _has_ai_marker(root) and nearby >= 2:
        return RepoScope("umbrella", root, repositories)
    return RepoScope("none", root, ())


def setup_roots(
    scope: RepoScope,
) -> tuple[tuple[Path, tuple[str, ...]], ...]:
    if scope.kind in ("repository", "worktree"):
        return ((scope.root, ("codegraph", "crg")),)
    if scope.kind == "umbrella":
        return tuple(
            (root, ("codegraph", "crg")) for root in sorted(scope.repositories)
        ) + ((scope.root, ("codegraph",)),)
    raise UserError("No Git repository or umbrella scope at this path.")


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


def ensure_local_excludes(root: Path, *, deadline: float) -> None:
    root = Path(root).resolve()
    result = run_child(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=root,
        timeout=remaining(deadline),
    )
    output = result.stdout.strip()
    if not output:
        raise UserError(f"Cannot locate Git exclude file at {root}.")
    exclude = Path(output)
    if not exclude.is_absolute():
        exclude = root / exclude
    try:
        existing = exclude.read_bytes().decode("utf-8") if exclude.exists() else ""
        lines = existing.splitlines()
        missing = [
            entry
            for entry in (".codegraph/", ".code-review-graph/")
            if entry not in lines
        ]
        if not missing:
            return
        separator = "" if not existing or existing.endswith(("\n", "\r")) else "\n"
        updated = existing + separator + "".join(entry + "\n" for entry in missing)
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_bytes(updated.encode("utf-8"))
    except (OSError, UnicodeError) as exc:
        raise UserError(f"Cannot update Git exclude file at {exclude}: {exc}") from exc


def initialize_indexes_locked(
    root: Path,
    tools: Mapping[str, Path],
    *,
    force: bool,
    deadline: float,
) -> None:
    root = Path(root).resolve()
    if "codegraph" in tools and (force or not (root / ".codegraph").is_dir()):
        run_child(
            [str(tools["codegraph"]), "init", str(root)],
            cwd=root,
            timeout=remaining(deadline),
        )
    if (root / ".git").exists() and tools:
        ensure_local_excludes(root, deadline=deadline)
    if "crg" in tools and (
        force or not (root / ".code-review-graph").is_dir()
    ):
        run_child(
            [str(tools["crg"]), "build", "--repo", str(root)],
            cwd=root,
            timeout=remaining(deadline),
        )


def update_indexes_locked(
    root: Path, tools: Mapping[str, Path], *, deadline: float
) -> None:
    root = Path(root).resolve()
    required = {"codegraph": ".codegraph", "crg": ".code-review-graph"}
    if not tools or any(
        name not in required or not (root / required[name]).is_dir()
        for name in tools
    ):
        raise UserError("Missing index; authorize setup-project first.")
    if (root / ".git").exists():
        ensure_local_excludes(root, deadline=deadline)
    if "codegraph" in tools:
        run_child(
            [str(tools["codegraph"]), "sync", str(root)],
            cwd=root,
            timeout=remaining(deadline),
        )
    if "crg" in tools:
        run_child(
            [
                str(tools["crg"]),
                "update",
                "--skip-flows",
                "--repo",
                str(root),
            ],
            cwd=root,
            timeout=remaining(deadline),
        )


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
