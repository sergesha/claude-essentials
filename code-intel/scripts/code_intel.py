#!/usr/bin/env python3
"""Deterministic CodeGraph + code-review-graph setup for Claude and Codex."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable


HOME = Path.home()
CODEGRAPH = shutil.which("codegraph") or "codegraph"
CRG = shutil.which("code-review-graph") or "code-review-graph"

EXCLUDED_DIRS = {"node_modules", ".venv", "vendor", ".cache", "__pycache__"}
CODE_SUFFIXES = {".py", ".ts", ".js", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".cs"}
AI_MARKERS = ("CLAUDE.md", "AGENTS.md", ".claude", ".codex", ".cursorrules")

Runner = Callable[[list[str]], int]
INDEX_COMMAND_TIMEOUT = 1800


def run(command: list[str]) -> int:
    print("+", shlex.join(command), flush=True)
    try:
        return subprocess.run(command, check=False).returncode
    except FileNotFoundError:
        print(f"missing executable: {command[0]}", file=sys.stderr)
        return 127


def atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    encoded = content.encode("utf-8")
    if path.exists() and path.is_file() and path.read_bytes() == encoded:
        if mode is not None and path.stat().st_mode & 0o777 != mode:
            path.chmod(mode)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        if mode is not None:
            temp_path.chmod(mode)
        elif path.exists():
            temp_path.chmod(path.stat().st_mode & 0o777)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def is_excluded(path: Path) -> bool:
    return any(part in EXCLUDED_DIRS for part in path.parts)


def discover_repos(base: Path, max_depth: int | None = None) -> list[Path]:
    repos: list[Path] = []
    base = base.resolve()
    for root, directories, files in os.walk(base):
        depth = len(Path(root).relative_to(base).parts)
        directories[:] = sorted(d for d in directories if d not in EXCLUDED_DIRS)
        if ".git" in directories or ".git" in files:
            repo = Path(root).resolve()
            if not is_excluded(repo.relative_to(base)):
                repos.append(repo)
            if ".git" in directories:
                directories.remove(".git")
        if max_depth is not None and depth >= max_depth:
            directories[:] = []
    return sorted(set(repos))


def code_file_count(repo: Path) -> int:
    count = 0
    repo = repo.resolve()
    for root, directories, files in os.walk(repo):
        current = Path(root)
        depth = len(current.relative_to(repo).parts)
        directories[:] = sorted(
            d for d in directories if d not in EXCLUDED_DIRS and d != ".git"
        )
        if depth >= 5:
            directories[:] = []
        for filename in files:
            if len((current / filename).relative_to(repo).parts) <= 5 and Path(filename).suffix in CODE_SUFFIXES:
                count += 1
    return count


def has_ai_marker(path: Path) -> bool:
    if any((path / marker).exists() for marker in AI_MARKERS):
        return True
    return (path / ".github" / "copilot-instructions.md").is_file()


def subrepo_count_within(parent: Path, repos: Iterable[Path], max_depth: int = 3) -> int:
    count = 0
    for repo in repos:
        try:
            relative = repo.relative_to(parent)
        except ValueError:
            continue
        git_depth = len(relative.parts) + 1
        if git_depth <= max_depth:
            count += 1
    return count


def detect_umbrellas(base: Path, repos: list[Path]) -> list[Path]:
    base = base.resolve()
    umbrellas: list[Path] = []
    seen: set[Path] = set()
    for repo in repos:
        parent = repo.parent
        while parent != base and parent != parent.parent:
            if parent not in seen and not (parent / ".git").exists() and not (parent / ".codegraph").is_dir():
                if has_ai_marker(parent) and subrepo_count_within(parent, repos) >= 2:
                    umbrellas.append(parent)
                    seen.add(parent)
            parent = parent.parent
    return umbrellas


def git_exclude_path(repo: Path) -> Path | None:
    dot_git = repo / ".git"
    if dot_git.is_dir():
        return dot_git / "info" / "exclude"
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else repo / path


def add_local_index_excludes(repo: Path) -> None:
    path = git_exclude_path(repo)
    if path is None:
        raise RuntimeError(f"Cannot locate Git exclude file: {repo}")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    for entry in (".codegraph/", ".code-review-graph/"):
        if entry not in lines:
            lines.append(entry)
    atomic_write(path, "\n".join(lines).lstrip("\n") + "\n")


def setup_repo(repo: Path, runner: Runner, codegraph: str, crg: str) -> int:
    repo = repo.resolve()
    if runner([codegraph, "init", str(repo)]) != 0:
        return 1
    try:
        add_local_index_excludes(repo)
    except (OSError, UnicodeError, RuntimeError) as error:
        print(f"Git exclude update failed for {repo}: {error}", file=sys.stderr)
        return 1
    if runner([crg, "build", "--repo", str(repo)]) != 0:
        return 1
    if runner([crg, "register", str(repo), "--alias", repo.name]) != 0:
        return 1
    return 0


def update_repo(repo: Path, runner: Runner = run, codegraph: str = CODEGRAPH, crg: str = CRG) -> int:
    repo = repo.resolve()
    commands: list[list[str]] = []
    if (repo / ".codegraph").is_dir():
        commands.append([codegraph, "sync", str(repo)])
    if (repo / ".code-review-graph").is_dir():
        commands.append([crg, "update", "--skip-flows", "--repo", str(repo)])
    if not commands:
        print(f"[skip: not initialized] {repo}", file=sys.stderr)
        return 1
    try:
        add_local_index_excludes(repo)
    except (OSError, UnicodeError, RuntimeError) as error:
        print(f"Git exclude update failed for {repo}: {error}", file=sys.stderr)
        return 1
    return sum(runner(command) != 0 for command in commands)


def update_project(
    path: Path,
    runner: Runner = run,
    codegraph: str = CODEGRAPH,
    crg: str = CRG,
) -> int:
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Not a directory: {path}")
    if (path / ".git").exists():
        return update_repo(path, runner, codegraph, crg)
    errors = 0
    found = False
    for repo in discover_repos(path):
        if (repo / ".codegraph").is_dir() or (repo / ".code-review-graph").is_dir():
            found = True
            errors += update_repo(repo, runner, codegraph, crg)
    if (path / ".codegraph").is_dir():
        found = True
        errors += runner([codegraph, "sync", str(path)]) != 0
    if not found:
        print(f"[skip: not initialized] {path}", file=sys.stderr)
        return 1
    return int(errors)


def update_batch(
    base: Path,
    runner: Runner = run,
    codegraph: str = CODEGRAPH,
    crg: str = CRG,
) -> int:
    base = base.expanduser().resolve()
    if not base.is_dir():
        raise ValueError(f"Not a directory: {base}")
    errors = 0
    repos = discover_repos(base)
    repo_set = set(repos)
    for repo in repos:
        if (repo / ".codegraph").is_dir() or (repo / ".code-review-graph").is_dir():
            print(f"=== UPDATE: {repo.name} — {repo} ===", flush=True)
            errors += update_repo(repo, runner, codegraph, crg)
    umbrellas = sorted(
        index.parent.resolve()
        for index in base.rglob(".codegraph")
        if index.is_dir() and index.parent.resolve() not in repo_set and not is_excluded(index.parent.relative_to(base))
    )
    for umbrella in umbrellas:
        print(f"=== UPDATE UMBRELLA: {umbrella.name} — {umbrella} ===", flush=True)
        errors += runner([codegraph, "sync", str(umbrella)]) != 0
    return int(errors)


def setup_project(
    path: Path,
    runner: Runner = run,
    codegraph: str = CODEGRAPH,
    crg: str = CRG,
    force: bool = False,
) -> int:
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Not a directory: {path}")
    if (path / ".git").exists():
        return setup_repo(path, runner, codegraph, crg)
    if not has_ai_marker(path) and not force:
        raise RuntimeError(
            "Directory is not a Git repository and has no AI workspace marker; rerun with --force after confirmation."
        )
    errors = 0
    for repo in discover_repos(path):
        errors += setup_repo(repo, runner, codegraph, crg)
    errors += runner([codegraph, "init", str(path)]) != 0
    return int(errors)


def setup_batch(
    base: Path,
    runner: Runner = run,
    codegraph: str = CODEGRAPH,
    crg: str = CRG,
) -> int:
    base = base.expanduser().resolve()
    if not base.is_dir():
        raise ValueError(f"Not a directory: {base}")
    all_repos = discover_repos(base)
    errors = 0
    for repo in all_repos:
        count = code_file_count(repo)
        if count < 5:
            continue
        print(f"=== {repo.name} ({count} files) — {repo} ===", flush=True)
        errors += setup_repo(repo, runner, codegraph, crg)
    for umbrella in detect_umbrellas(base, all_repos):
        count = subrepo_count_within(umbrella, all_repos)
        print(f"=== UMBRELLA: {umbrella.name} ({count} sub-repos) — {umbrella} ===", flush=True)
        errors += runner([codegraph, "init", str(umbrella)]) != 0
    return int(errors)


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def hook_prompt(codegraph: str = CODEGRAPH) -> int:
    """Return CodeGraph context in the JSON shape shared by Claude and Codex."""
    raw_payload = sys.stdin.read()
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        cwd = Path(payload.get("cwd") or os.getcwd()).expanduser().resolve()
        repo = git_root(cwd)
        if repo is not None:
            if project_index_gaps(repo, umbrella=False):
                queue_index_update({"cwd": str(repo), "paths": [], "discover": False})
                return 0
            elif _index_ready(repo / ".codegraph"):
                try:
                    result = run_hook_command([codegraph, "sync", str(repo)], timeout=5)
                    if result.returncode:
                        raise RuntimeError(result.stderr)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    queue_index_update({"cwd": str(repo), "paths": [], "discover": False, "force": True})
                    return 0
    try:
        result = run_hook_command(
            [codegraph, "prompt-hook"],
            input=raw_payload,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if result.returncode != 0 or not result.stdout:
        return 0
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": result.stdout,
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


def tool_executable(name: str) -> str | None:
    executable = shutil.which(name)
    if executable and Path(executable).resolve().name == "mise":
        mise = shutil.which("mise")
        if not mise:
            raise RuntimeError(f"Cannot resolve mise shim {executable}")
        result = subprocess.run([mise, "which", name], capture_output=True, text=True, timeout=15)
        if result.returncode or not result.stdout.strip():
            raise RuntimeError(f"Cannot resolve mise shim {name}: {result.stderr.strip()}")
        return result.stdout.strip()
    return executable


def mise_install_command(tool: str, executable: str) -> list[str] | None:
    """Keep mise-owned tools in their declared config, including version pins."""
    mise = shutil.which("mise")
    if not mise:
        return None
    result = subprocess.run([mise, "ls", "--json", tool], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(f"Cannot inspect mise tool {tool}: {result.stderr.strip()}")
    entries = json.loads(result.stdout)
    entry = next((item for item in entries if item.get("active")), None)
    if entry is None and len(entries) == 1 and entries[0].get("source"):
        entry = entries[0]
    if entry is None:
        return None
    current = shutil.which(executable)
    if current and Path(current).resolve().name != "mise":
        owned = any(Path(current).resolve().is_relative_to(Path(item["install_path"]).resolve()) for item in entries)
        if not owned:
            return None
    source = entry.get("source", {})
    if source.get("type") != "mise.toml":
        raise RuntimeError(f"Cannot preserve mise configuration for {tool}: {source}")
    config = Path(source["path"])
    command = [mise, "use", "--path", str(config)]
    if executable == "code-review-graph":
        definition = tomllib.loads(config.read_text()).get("tools", {}).get(tool, {})
        extras = definition.get("extras", []) if isinstance(definition, dict) else []
        if isinstance(extras, str):
            extras = extras.split(",")
        extras = sorted({*extras, "communities"})
        command += ["--force", "--tool-option", f"extras={json.dumps(extras)}"]
    command.append(f"{tool}@{entry['requested_version']}")
    return command


def refresh_mise_paths() -> None:
    """Mise may move a tool to a new directory when its extras change."""
    for tool, executable in (("npm:@colbymchenry/codegraph", "codegraph"), ("pipx:code-review-graph", "code-review-graph")):
        if not shutil.which(executable):
            continue
        command = mise_install_command(tool, executable)
        if command:
            result = subprocess.run([command[0], "which", executable], capture_output=True, text=True, timeout=15)
            if result.returncode == 0 and result.stdout.strip():
                directory = str(Path(result.stdout.strip()).parent)
                os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")


def install_tools(*, upgrade: bool = False, runner: Runner | None = None) -> int:
    runner = runner or run
    need_codegraph = upgrade or shutil.which("codegraph") is None
    need_crg = upgrade or shutil.which("code-review-graph") is None
    if not (need_codegraph or need_crg):
        print("Code intelligence tools are already available on PATH.")
        return 0
    cg_command = mise_install_command("npm:@colbymchenry/codegraph", "codegraph") if need_codegraph else None
    crg_command = mise_install_command("pipx:code-review-graph", "code-review-graph") if need_crg else None
    npm = shutil.which("npm") if need_codegraph and not cg_command else None
    current_crg = tool_executable("code-review-graph") if need_crg and not crg_command else None
    pipx_owned = current_crg and (Path(current_crg).resolve().parent.parent / "pipx_metadata.json").is_file()
    uv = shutil.which("uv") if need_crg and not (crg_command or pipx_owned) else None
    pipx = shutil.which("pipx") if need_crg and not (crg_command or uv) else None
    missing = []
    if need_codegraph and not (cg_command or npm):
        missing.append("npm")
    if need_crg and not (crg_command or uv or pipx):
        missing.append("uv or pipx")
    if missing:
        print(
            f"missing installer: {', '.join(missing)}; install it on PATH or provision "
            "codegraph and code-review-graph on PATH manually",
            file=sys.stderr,
        )
        return 127
    commands = []
    if need_codegraph:
        commands.append(cg_command or [npm, "install", "--global", "@colbymchenry/codegraph"])
    if need_crg:
        commands.append(
            crg_command or (
                [uv, "tool", "install", "--upgrade", "code-review-graph[communities]"]
                if uv else [pipx, "install", "--force", "code-review-graph[communities]"]
            )
        )
    return sum(1 for command in commands if runner(command) != 0)


def plugin_version() -> str:
    return load_json(Path(__file__).resolve().parents[1] / ".claude-plugin/plugin.json")["version"]


def toolchain_health() -> dict[str, str]:
    """Verify the actual PATH commands and igraph in CRG's own interpreter."""
    health = {}
    for name in ("codegraph", "code-review-graph"):
        executable = tool_executable(name)
        if not executable:
            raise RuntimeError(f"Missing {name} on PATH")
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError(f"{name} failed: {result.stderr.strip()}")
        health[name] = f"{Path(executable).resolve()}: {result.stdout.strip()}"
    executable = Path(tool_executable("code-review-graph")).resolve()
    python = executable.with_name("python")
    if not python.is_file():
        raise RuntimeError(f"Cannot locate CRG's Python next to {executable}")
    result = subprocess.run(
        [str(python), "-c", "import igraph; "
         "assert len(igraph.Graph.Famous('Zachary').community_leiden().membership) == 34; "
         "print(igraph.__version__)"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        raise RuntimeError(f"igraph unavailable in CRG environment: {result.stderr.strip()}")
    health["igraph"] = result.stdout.strip()
    return health


def run_provision(command: list[str]) -> int:
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode:
        print(f"Installer failed: {shlex.join(command)}\n{(result.stdout + result.stderr)[-2000:]}")
    return result.returncode


def ensure_tools() -> int:
    """Provision on first activation of each plugin release; verify before caching."""
    global CODEGRAPH, CRG
    # Both clients share this receipt and lock, avoiding simultaneous installers.
    receipt = HOME / ".cache/code-intel/toolchain.json"
    try:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        with receipt.with_suffix(".lock").open("a+b") as lock:
            if os.name == "nt":
                import msvcrt
                lock.write(b"\0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            version = plugin_version()
            refresh_mise_paths()
            try:
                previous = load_json(receipt)
            except (OSError, ValueError):
                previous = {}
            try:
                health = toolchain_health()
            except (OSError, RuntimeError, subprocess.SubprocessError):
                health = None
            if health is not None and previous == {"plugin_version": version, "tools": health}:
                return 0
            print("Code Intel: installing/updating tools and required communities extension.")
            if install_tools(upgrade=True, runner=run_provision):
                raise RuntimeError("Tool installation failed; activation remains incomplete")
            refresh_mise_paths()
            CODEGRAPH = tool_executable("codegraph") or "codegraph"
            CRG = tool_executable("code-review-graph") or "code-review-graph"
            health = toolchain_health()
            atomic_write(receipt, json.dumps({"plugin_version": version, "tools": health}) + "\n")
            print(f"Code Intel: toolchain verified; igraph {health['igraph']} available in CRG.")
            return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Code Intel: tool provisioning failed: {error}")
        return 1


def hook_start() -> int:
    # Include lock waits, both installers and health checks in one host budget.
    try:
        result = run_hook_command(
            [sys.executable, str(Path(__file__).resolve()), "hook-activate"],
            input=sys.stdin.read(), timeout=240,
        )
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode:
            print(f"Code Intel: startup activation failed: {(result.stderr or '')[-2000:]}")
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Code Intel: startup activation did not complete: {error}; retry ensure-tools.")
    return 0


def hook_activate() -> int:
    if ensure_tools() == 0:
        return hook_status()
    return 0


def upgrade(base: Path) -> int:
    errors = install_tools(upgrade=True)
    if errors:
        return errors
    return status(base)


def git_root(path: Path) -> Path | None:
    directory = path if path.is_dir() else path.parent
    while not directory.exists() and directory != directory.parent:
        directory = directory.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def index_request(payload: dict) -> dict:
    """Keep path hints only; never persist shell commands or tool output."""
    if not isinstance(payload, dict):
        return {}
    cwd = Path(payload.get("cwd") or os.getcwd()).expanduser().resolve()
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    response = payload.get("tool_response") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    if not isinstance(response, dict):
        response = {}
    paths: list[str] = []
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or response.get("filePath")
    if file_path:
        paths.append(str(file_path))

    if tool_name == "apply_patch":
        patch = str(tool_input.get("command") or tool_input.get("patch") or tool_input.get("input") or "")
        paths.extend(re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE))
        paths.extend(re.findall(r"^\*\*\* Move to: (.+)$", patch, re.MULTILINE))
    if not paths and tool_name != "Bash":
        return {}
    return {"cwd": str(cwd), "paths": sorted(set(paths)), "discover": tool_name == "Bash"}


def hook_repos(request: dict) -> set[Path]:
    """Discover in the worker, never in the foreground PostToolUse hook."""
    cwd = Path(request["cwd"])
    repos: set[Path] = set()
    for name in request["paths"]:
        path = Path(name).expanduser()
        root = git_root(path if path.is_absolute() else cwd / path)
        if root:
            repos.add(root)
    if request["discover"]:
        repos.update(discover_repos(cwd, max_depth=3))
    if request["discover"] or not request["paths"]:
        root = git_root(cwd)
        if root:
            repos.add(root)
    return repos


def indexing_cache() -> Path:
    return HOME / ".cache/code-intel/indexing"


def try_lock(stream, wait: float = 0) -> bool:
    deadline = time.monotonic() + wait
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def unlock(stream) -> None:
    if os.name == "nt":
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def queue_lock(cache: Path):
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "queue.lock").open("a+b") as lock:
        if os.name == "nt" and not lock.tell():
            lock.write(b"\0")
            lock.flush()
        if not try_lock(lock, wait=1):
            raise TimeoutError("Code Intel indexing queue is busy")
        try:
            yield
        finally:
            unlock(lock)


def enqueue_index_update(request: dict) -> None:
    cache = indexing_cache()
    content = json.dumps(request, sort_keys=True)
    key = hashlib.sha256(content.encode()).hexdigest()
    with queue_lock(cache):
        atomic_write(cache / "requests" / f"{key}.json", content)


def queue_index_update(request: dict) -> bool:
    """Detach all stdio so the host can finish the hook immediately."""
    try:
        enqueue_index_update(request)
        cache = indexing_cache()
        with (cache / "worker.log").open("a") as log:
            options = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            }
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "hook-worker"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                close_fds=True, **options,
            )
        return True
    except (OSError, ValueError) as error:
        print(f"Code Intel: cannot queue index update: {error}", file=sys.stderr)
        return False


def run_hook_command(command: list[str], *, timeout: float, input: str | None = None,
                     capture: bool = True) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True, start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Node/Python launchers can have children; terminate the command group.
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            process.terminate()
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        finally:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def repo_fingerprint(repo: Path) -> str:
    """Cheap source inventory: HEAD, Git-visible paths and file metadata.

    Unlike porcelain status this detects another edit to an already dirty file,
    clean checkouts, untracked files, deletions and edits reverted to HEAD.
    Index databases excluded by Git never participate.
    """
    paths = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True, capture_output=True, timeout=10,
    ).stdout.split(b"\0")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
        capture_output=True, timeout=5,
    )
    digest = hashlib.sha256(head.stdout)
    digest.update(plugin_version().encode())
    for raw in sorted(set(paths) - {b""}):
        digest.update(raw + b"\0")
        try:
            stat = (repo / os.fsdecode(raw)).lstat()
            digest.update(f"{stat.st_mode}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}".encode())
        except FileNotFoundError:
            digest.update(b"deleted")
    return digest.hexdigest()


def refresh_hook_repo(repo: Path, *, force: bool = False) -> None:
    repo = repo.resolve()
    cache = indexing_cache()
    receipt = cache / "repos" / (hashlib.sha256(os.fsencode(str(repo))).hexdigest() + ".json")
    state = {"repo": str(repo), "status": "running", "started": time.time()}
    try:
        fingerprint = repo_fingerprint(repo)
        try:
            previous = load_json(receipt)
        except (OSError, ValueError):
            previous = {}
        if not force and previous.get("status") == "success" and previous.get("fingerprint") == fingerprint and not project_index_gaps(repo, umbrella=False):
            return
        atomic_write(receipt, json.dumps(state))

        def runner(command):
            print("+", shlex.join(command), flush=True)
            return run_hook_command(command, timeout=INDEX_COMMAND_TIMEOUT, capture=False).returncode

        if project_index_gaps(repo, umbrella=False):
            result = setup_project(repo, runner=runner, codegraph=CODEGRAPH, crg=CRG)
        else:
            result = update_repo(repo, runner=runner, codegraph=CODEGRAPH, crg=CRG)
        if result:
            raise RuntimeError(f"Indexer exited unsuccessfully ({result}); see worker.log")
        state.update(status="success", fingerprint=fingerprint)
        # Never acknowledge edits which arrived during the update as indexed.
        if repo_fingerprint(repo) != fingerprint:
            enqueue_index_update({"cwd": str(repo), "paths": [], "discover": False})
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        state.update(status="error", error=str(error))
        print(f"Code Intel: index update failed for {repo}: {error}", flush=True)
    state["finished"] = time.time()
    atomic_write(receipt, json.dumps(state))


def hook_worker() -> int:
    cache = indexing_cache()
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "worker.lock").open("a+b") as worker:
        if os.name == "nt" and not worker.tell():
            worker.write(b"\0")
            worker.flush()
        if not try_lock(worker):
            return 0
        try:
            while True:
                with queue_lock(cache):
                    requests = []
                    for path in sorted((cache / "requests").glob("*.json")):
                        requests.append(load_json(path))
                        path.unlink()
                    if not requests:
                        # Release while holding the queue lock: a racing enqueue
                        # either gets drained here or starts a new worker.
                        unlock(worker)
                        return 0
                repos: set[Path] = set()
                forced: set[Path] = set()
                for request in requests:
                    targets = hook_repos(request)
                    repos.update(targets)
                    if request.get("force"):
                        forced.update(targets)
                for repo in sorted(repos):
                    refresh_hook_repo(repo, force=repo in forced)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"Code Intel: indexing worker failed: {error}", flush=True)
            return 1


def hook_update() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    request = index_request(payload)
    if request:
        queue_index_update(request)
    return 0


def _index_ready(path: Path) -> bool:
    """True when the index directory exists and contains at least one file."""
    return path.is_dir() and any(path.iterdir())


def project_index_gaps(project: Path, *, umbrella: bool) -> tuple[str, ...]:
    """Describe missing or empty indexes without invoking either indexing tool."""

    gaps: list[str] = []
    if umbrella:
        if not _index_ready(project / ".codegraph"):
            gaps.append("CodeGraph umbrella")
        for repo in discover_repos(project):
            if not _index_ready(repo / ".codegraph"):
                gaps.append(f"{repo}: CodeGraph")
            if not _index_ready(repo / ".code-review-graph"):
                gaps.append(f"{repo}: CRG")
    else:
        if not _index_ready(project / ".codegraph"):
            gaps.append("CodeGraph")
        if not _index_ready(project / ".code-review-graph"):
            gaps.append("CRG")
    return tuple(gaps)


def hook_umbrella(cwd: Path) -> Path | None:
    """Recognize the explicit current-directory umbrella contract."""

    if not cwd.is_dir() or not has_ai_marker(cwd):
        return None
    repos = discover_repos(cwd, max_depth=3)
    if subrepo_count_within(cwd, repos) < 2:
        return None
    return cwd


def hook_status() -> int:
    missing = [name for name in ("codegraph", "code-review-graph") if shutil.which(name) is None]
    if missing:
        print(
            f"Code intelligence: missing tools on PATH: {', '.join(missing)}. "
            "Invoke the code-intel skill and offer to install the missing tools. "
            "Ask the user for consent; only after approval run install-tools, then "
            "verify both commands on PATH and restart the host to reconnect MCP. "
            "Do not install without approval."
        )
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        payload = {}
    cwd = Path(payload.get("cwd") or os.getcwd()).expanduser().resolve()
    repo = git_root(cwd)
    umbrella = None if repo is not None else hook_umbrella(cwd)
    project = repo or umbrella
    if project is None:
        print("Code intelligence: not a Git repository")
        return 0
    gaps = project_index_gaps(project, umbrella=umbrella is not None)
    if not gaps:
        print(f"Code intelligence: initialized — {project}")
        receipt = indexing_cache() / "repos" / (hashlib.sha256(os.fsencode(str(project))).hexdigest() + ".json")
        try:
            state = load_json(receipt)
        except (OSError, ValueError):
            state = {}
        if state.get("status") in ("error", "running"):
            print(f"Code intelligence: last automatic update {'failed' if state['status'] == 'error' else 'is incomplete'}; "
                  f"log: {indexing_cache() / 'worker.log'}")
        return 0
    if repo is not None:
        queued = queue_index_update({"cwd": str(repo), "paths": [], "discover": False})
        print(
            f"Code intelligence: automatic initialization {'queued' if queued else 'could not be queued'} — {repo}; "
            f"missing: {', '.join(gaps)}; log: {indexing_cache() / 'worker.log'}"
        )
        return 0
    scope = (
        "umbrella and its eligible nested Git repositories"
        if umbrella is not None
        else "repository"
    )
    print(
        f"Code intelligence: initialization needed — {project} ({scope}); "
        f"missing: {', '.join(gaps)}. Ask the user whether to initialize "
        "the current project with the code-intel skill. If approved, "
        "invoke the skill and run setup-project, then verify with project-status. "
        "Do not ask the user to run commands and do not initialize without approval."
    )
    return 0


def status(base: Path) -> int:
    errors = 0
    for command in (
        [CODEGRAPH, "--version"],
        [CRG, "--version"],
    ):
        errors += run(command) != 0
    registry = HOME / ".code-review-graph" / "registry.json"
    try:
        registered = load_json(registry).get("repos", [])
        registry_valid = isinstance(registered, list)
    except (OSError, ValueError, json.JSONDecodeError):
        registered, registry_valid = [], False
    print(f"CRG registry: {len(registered)} repositories" if registry_valid else "CRG registry: invalid")
    errors += not registry_valid
    indexes = sorted(
        path.parent for path in base.expanduser().resolve().rglob(".codegraph") if path.is_dir()
    )
    print("=== CodeGraph indexes ===")
    for path in indexes:
        kind = "repo" if (path / ".git").exists() else "umbrella"
        print(f"  [{kind}] {path}")
    return int(errors)


def project_status(
    path: Path,
    runner: Runner = run,
    codegraph: str = CODEGRAPH,
    crg: str = CRG,
) -> int:
    project = path.expanduser().resolve()
    del runner, codegraph, crg
    if not project.is_dir():
        print(f"[missing] {project}")
        return 1
    if (project / ".git").exists():
        checks = (("CodeGraph", project / ".codegraph"), ("CRG", project / ".code-review-graph"))
        kind = "repo"
    else:
        checks = (("CodeGraph umbrella", project / ".codegraph"),)
        kind = "umbrella"
    errors = 0
    print(f"[{kind}] {project}")
    for label, index in checks:
        present = index.is_dir()
        print(f"  {label}: {'present' if present else 'missing'}")
        errors += not present
    if kind == "umbrella":
        for repo in discover_repos(project):
            for label, index in (("CodeGraph", repo / ".codegraph"), ("CRG", repo / ".code-review-graph")):
                present = index.is_dir()
                print(f"  {repo}: {label} {'present' if present else 'missing'}")
                errors += not present
    return int(errors)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subcommands = result.add_subparsers(dest="command", required=True)
    subcommands.add_parser("install-tools")
    subcommands.add_parser("ensure-tools")
    upgrade_parser = subcommands.add_parser("upgrade")
    upgrade_parser.add_argument("--base", type=Path, default=HOME / "Projects")

    project = subcommands.add_parser("setup-project")
    project.add_argument("path", type=Path)
    project.add_argument("--force", action="store_true")

    batch = subcommands.add_parser("setup-batch")
    batch.add_argument("base", type=Path)

    update_one = subcommands.add_parser("update-project")
    update_one.add_argument("path", type=Path)

    update_many = subcommands.add_parser("update-batch")
    update_many.add_argument("base", type=Path)

    state = subcommands.add_parser("status")
    state.add_argument("--base", type=Path, default=HOME / "Projects")

    project_state = subcommands.add_parser("project-status")
    project_state.add_argument("path", type=Path)

    server = subcommands.add_parser("serve")
    server.add_argument("engine", choices=("codegraph", "crg"))

    subcommands.add_parser("hook-update")
    subcommands.add_parser("hook-worker")
    subcommands.add_parser("hook-status")
    subcommands.add_parser("hook-start")
    subcommands.add_parser("hook-activate")
    subcommands.add_parser("hook-prompt")
    return result


def main() -> int:
    arguments = parser().parse_args()
    if arguments.command == "install-tools":
        return install_tools()
    if arguments.command == "ensure-tools":
        return ensure_tools()
    if arguments.command == "upgrade":
        return upgrade(arguments.base)
    if arguments.command == "setup-project":
        return setup_project(arguments.path, force=arguments.force)
    if arguments.command == "setup-batch":
        return setup_batch(arguments.base)
    if arguments.command == "update-project":
        return update_project(arguments.path)
    if arguments.command == "update-batch":
        return update_batch(arguments.base)
    if arguments.command == "status":
        return status(arguments.base)
    if arguments.command == "project-status":
        return project_status(arguments.path)
    if arguments.command == "serve":
        command = [CODEGRAPH, "serve", "--mcp"] if arguments.engine == "codegraph" else [CRG, "serve"]
        os.execvp(command[0], command)
    if arguments.command == "hook-update":
        return hook_update()
    if arguments.command == "hook-worker":
        return hook_worker()
    if arguments.command == "hook-status":
        return hook_status()
    if arguments.command == "hook-start":
        return hook_start()
    if arguments.command == "hook-activate":
        return hook_activate()
    if arguments.command == "hook-prompt":
        return hook_prompt()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
