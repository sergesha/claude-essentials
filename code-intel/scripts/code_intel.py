#!/usr/bin/env python3
"""Deterministic CodeGraph + code-review-graph setup for Claude and Codex."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Iterable


HOME = Path.home()
MISE_SHIMS = HOME / ".local" / "share" / "mise" / "shims"
CODEGRAPH = str(MISE_SHIMS / "codegraph")
CRG = str(MISE_SHIMS / "code-review-graph")

EXCLUDED_DIRS = {"node_modules", ".venv", "vendor", ".cache", "__pycache__"}
CODE_SUFFIXES = {".py", ".ts", ".js", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".cs"}
AI_MARKERS = ("CLAUDE.md", "AGENTS.md", ".claude", ".codex", ".cursorrules")

Runner = Callable[[list[str]], int]


def run(command: list[str]) -> int:
    print("+", shlex.join(command), flush=True)
    try:
        return subprocess.run(command, check=False).returncode
    except FileNotFoundError:
        print(f"missing executable: {command[0]}", file=sys.stderr)
        return 127


def run_quiet(command: list[str]) -> int:
    try:
        return subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
    except OSError:
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
        if repo is not None and project_index_gaps(repo, umbrella=False):
            if not ensure_repo_indexes(repo):
                return 0
    try:
        result = subprocess.run(
            [codegraph, "prompt-hook"],
            input=raw_payload,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
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


def install_tools() -> int:
    commands = [
        ["mise", "use", "-g", "npm:@colbymchenry/codegraph@latest"],
        ["mise", "use", "-g", "pipx:code-review-graph@latest"],
    ]
    return sum(1 for command in commands if run(command) != 0)


def upgrade(base: Path) -> int:
    errors = install_tools()
    if errors:
        return errors
    return status(base)


def git_root(path: Path) -> Path | None:
    directory = path if path.is_dir() else path.parent
    result = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def dirty(repo: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def hook_repos(payload: dict) -> set[Path]:
    if not isinstance(payload, dict):
        return set()
    cwd = Path(payload.get("cwd") or os.getcwd()).expanduser().resolve()
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    response = payload.get("tool_response") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    if not isinstance(response, dict):
        response = {}
    repos: set[Path] = set()

    file_path = tool_input.get("file_path") or response.get("filePath")
    if file_path:
        root = git_root(Path(file_path).expanduser())
        if root:
            repos.add(root)

    if tool_name == "apply_patch":
        patch = str(tool_input.get("command") or "")
        for match in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE):
            candidate = Path(match.group(1))
            if not candidate.is_absolute():
                candidate = cwd / candidate
            root = git_root(candidate)
            if root:
                repos.add(root)

    if tool_name == "Bash":
        repos.update(discover_repos(cwd, max_depth=3))
        root = git_root(cwd)
        if root:
            repos.add(root)
    return {repo for repo in repos if dirty(repo)}


def hook_update() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    for repo in sorted(hook_repos(payload)):
        if project_index_gaps(repo, umbrella=False):
            ensure_repo_indexes(repo)
            continue
        if not (repo / ".code-review-graph").is_dir():
            continue
        try:
            subprocess.run(
                [CRG, "update", "--skip-flows", "--repo", str(repo)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return 0
    return 0


def project_index_gaps(project: Path, *, umbrella: bool) -> tuple[str, ...]:
    """Describe missing indexes without invoking either indexing tool."""

    gaps: list[str] = []
    if umbrella:
        if not (project / ".codegraph").is_dir():
            gaps.append("CodeGraph umbrella")
        for repo in discover_repos(project):
            if not (repo / ".codegraph").is_dir():
                gaps.append(f"{repo}: CodeGraph")
            if not (repo / ".code-review-graph").is_dir():
                gaps.append(f"{repo}: CRG")
    else:
        if not (project / ".codegraph").is_dir():
            gaps.append("CodeGraph")
        if not (project / ".code-review-graph").is_dir():
            gaps.append("CRG")
    return tuple(gaps)


def ensure_repo_indexes(repo: Path) -> bool:
    """Initialize missing repository indexes without polluting hook output."""

    if not project_index_gaps(repo, umbrella=False):
        return True
    try:
        return setup_project(repo.resolve(), runner=run_quiet) == 0
    except (OSError, RuntimeError, ValueError):
        return False


def hook_umbrella(cwd: Path) -> Path | None:
    """Recognize the explicit current-directory umbrella contract."""

    if not cwd.is_dir() or not has_ai_marker(cwd):
        return None
    repos = discover_repos(cwd, max_depth=3)
    if subrepo_count_within(cwd, repos) < 2:
        return None
    return cwd


def hook_status() -> int:
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
        return 0
    if repo is not None:
        if ensure_repo_indexes(repo):
            gaps = project_index_gaps(repo, umbrella=False)
            if not gaps:
                print(f"Code intelligence: initialized — {repo}")
                return 0
        print(
            f"Code intelligence: automatic initialization failed — {repo}; "
            f"missing: {', '.join(project_index_gaps(repo, umbrella=False))}"
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
    subcommands.add_parser("hook-status")
    subcommands.add_parser("hook-prompt")
    return result


def main() -> int:
    arguments = parser().parse_args()
    if arguments.command == "install-tools":
        return install_tools()
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
        os.execv(command[0], command)
    if arguments.command == "hook-update":
        return hook_update()
    if arguments.command == "hook-status":
        return hook_status()
    if arguments.command == "hook-prompt":
        return hook_prompt()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
