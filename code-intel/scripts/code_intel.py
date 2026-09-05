#!/usr/bin/env python3
"""Shared code intelligence controller (Python 3.11+, macOS and Linux)."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterator, Literal, Mapping, Sequence

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


class UnusableDataLocation(UserError):
    """The selected host data directory cannot be used; never fall back."""


class CorruptState(UserError):
    """Existing state cannot be trusted or repaired implicitly."""


@dataclass(frozen=True)
class DataLocation:
    path: Path
    source: Literal["PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"]


@dataclass(frozen=True)
class FreshnessMarker:
    root: str
    head: str
    versions: Mapping[str, str]
    checkout_fingerprint: str
    index_fingerprints: Mapping[str, str]
    status: Literal["pending", "success", "failed"]
    schema_version: int = 2
    crg_candidates: list[str] | None = None


@dataclass(frozen=True)
class ReadinessResult:
    root: Path
    tools: Mapping[str, Path]
    marker: FreshnessMarker


def select_data_location(env: Mapping[str, str], *, read_only: bool) -> DataLocation:
    source = next(
        (name for name in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA") if env.get(name)),
        None,
    )
    if source is None:
        raise UnusableDataLocation(
            "Set PLUGIN_DATA or CLAUDE_PLUGIN_DATA to host plugin storage."
        )
    try:
        path = Path(env[source]).expanduser().resolve()
        ancestor = path
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not ancestor.is_dir() or not os.access(ancestor, os.R_OK | os.X_OK):
            raise OSError("storage is not an accessible directory")
        if not read_only:
            if not os.access(ancestor, os.W_OK) or not ancestor.stat().st_mode & 0o222:
                raise OSError("storage is not writable")
            path.mkdir(parents=True, exist_ok=True)
        return DataLocation(path, source)
    except (OSError, ValueError, RuntimeError) as exc:
        raise UnusableDataLocation(f"Cannot use {source}={env[source]!r}: {exc}") from exc


def state_path(root: Path, data: DataLocation) -> Path:
    key = hashlib.sha256(os.fsencode(str(root.resolve()))).hexdigest()
    return data.path / (key + ".json")


def _valid_crg_path(path: object) -> bool:
    if not isinstance(path, str) or not path or "\0" in path or path.startswith("/"):
        return False
    if any(part in ("", ".", "..") for part in path.split("/")):
        return False
    try:
        os.fsencode(path)
    except UnicodeError:
        return False
    return True


def _validate_marker(root: Path, value: object) -> FreshnessMarker:
    fields = {
        "root", "head", "versions", "checkout_fingerprint", "index_fingerprints", "status"
    }
    if not isinstance(value, dict):
        raise CorruptState("Invalid marker schema.")
    if set(value) == fields:
        value = {**value, "schema_version": 1, "crg_candidates": None}
    elif (set(value) != fields | {"schema_version", "crg_candidates"}
          or type(value["schema_version"]) is not int or value["schema_version"] != 2):
        raise CorruptState("Invalid marker schema.")
    candidates = value["crg_candidates"]
    if candidates is not None and (
        not isinstance(candidates, list)
        or any(not _valid_crg_path(path) for path in candidates)
        or candidates != sorted(set(candidates), key=os.fsencode)
    ):
        raise CorruptState("Invalid CRG candidate history.")
    if any(
        not isinstance(value[name], str)
        for name in ("root", "head", "checkout_fingerprint", "status")
    ):
        raise CorruptState("Invalid marker values.")
    if value["root"] != str(root.resolve()) or value["status"] not in (
        "pending", "success", "failed"
    ):
        raise CorruptState("Marker root or status mismatch.")
    for name in ("versions", "index_fingerprints"):
        mapping = value[name]
        if not isinstance(mapping, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in mapping.items()
        ):
            raise CorruptState("Invalid marker mappings.")
    if value["status"] == "success":
        versions, indexes = value["versions"], value["index_fingerprints"]
        if (
            not versions or set(versions) != set(indexes)
            or not set(versions) <= set(TOOLS)
            or any(not version for version in versions.values())
            or not re.fullmatch(r"[0-9a-f]{64}", value["checkout_fingerprint"])
            or any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in indexes.values())
            or (value["head"] and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["head"]))
            or (value["schema_version"] == 2 and "crg" in versions
                and (candidates is None or not value["head"]))
        ):
            raise CorruptState("Incomplete successful freshness marker.")
    return FreshnessMarker(**value)


def read_marker(root: Path, data: DataLocation) -> FreshnessMarker | None:
    path = state_path(root, data)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CorruptState(f"Cannot read state at {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CorruptState(f"State is not a regular file: {path}")
            return _validate_marker(root, json.load(stream))
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise CorruptState(f"Cannot read state at {path}: {exc}") from exc


def write_marker(root: Path, data: DataLocation, marker: FreshnessMarker) -> None:
    value = asdict(marker)
    _validate_marker(root, value)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".marker-", suffix=".tmp", dir=data.path)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_path(root, data))
    except OSError as exc:
        raise UserError(f"Cannot publish state: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


@contextmanager
def root_lock(root: Path, data: DataLocation, deadline: float) -> Iterator[None]:
    """Serialize one canonical checkout; the OS releases ownership on exit.

    Never unlink a lock file: waiters must all keep using the same inode.
    """
    remaining(deadline)
    path = state_path(root, data).with_suffix(".lock")
    fd = None
    try:
        fd = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UserError(f"Lock is not a regular file: {path}")
        while True:
            remaining(deadline)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(.02, remaining(deadline)))
        remaining(deadline)
        yield
    except OSError as exc:
        raise UserError(f"Cannot use root lock {path}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)


def remaining(deadline: float) -> float:
    timeout = deadline - time.monotonic()
    if not math.isfinite(timeout) or timeout <= 0:
        raise UserError("Operation deadline has expired.")
    return timeout


INDEX_DIRS = {"codegraph": ".codegraph", "crg": ".code-review-graph"}
CHECKOUT_EXCLUDES = {b".git", b".codegraph", b".code-review-graph"}
# Actual CodeGraph 1.6.0 runtime names, not suffix patterns. CRG 2.3.8's
# daemon identity lives in its global home, outside the project index.
INDEX_TRANSIENTS = {
    "codegraph": {b"codegraph.lock", b"daemon.pid", b"daemon.sock"},
    "crg": set(),
}


@contextmanager
def _parent_fd(root_fd: int, path: bytes) -> Iterator[tuple[int, bytes]]:
    parts = path.split(b"/")
    if any(part in (b"", b".", b"..") for part in parts):
        raise UserError("Invalid fingerprint input path.")
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        yield current, parts[-1]
    finally:
        os.close(current)


def _metadata(value: os.stat_result) -> tuple[int, ...]:
    identity = (value.st_dev, value.st_ino, value.st_mode)
    # Directory entry sets are compared explicitly. Transient engine files may
    # change their parent directory's timestamps without changing its inputs.
    if stat.S_ISDIR(value.st_mode):
        return identity
    return (*identity, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _input_metadata(
    root_fd: int, paths: Sequence[bytes], deadline: float, *, missing: bool
) -> dict[bytes, tuple[int, ...] | None]:
    result = {}
    for path in paths:
        remaining(deadline)
        try:
            with _parent_fd(root_fd, path) as (parent, name):
                result[path] = _metadata(os.stat(name, dir_fd=parent, follow_symlinks=False))
        except FileNotFoundError:
            if not missing:
                raise
            result[path] = None  # A stable tracked deletion is a distinct input.
    return result


def _tree_paths(
    root_fd: int, deadline: float, *, excluded: set[bytes], transient: set[bytes]
) -> list[bytes]:
    paths = []

    def visit(directory: int, prefix: bytes) -> None:
        remaining(deadline)
        with os.scandir(directory) as entries:
            names = []
            for entry in entries:
                remaining(deadline)
                names.append(os.fsencode(entry.name))
            names.sort()
        for name in names:
            remaining(deadline)
            if name in excluded or (not prefix and name in transient):
                continue
            path = prefix + name
            paths.append(path)
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    if _metadata(os.fstat(child)) != _metadata(metadata):
                        raise UserError("Directory changed during fingerprint capture.")
                    visit(child, path + b"/")
                finally:
                    os.close(child)
    visit(root_fd, b"")
    return sorted(paths)


def _git_paths(root: Path, deadline: float) -> list[bytes]:
    output = run_child(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, timeout=remaining(deadline),
    ).stdout
    return sorted({
        path for path in output.encode("utf-8", errors="surrogateescape").split(b"\0")
        if path and not CHECKOUT_EXCLUDES.intersection(path.split(b"/"))
    })


def _content_fingerprint(
    root: Path, deadline: float, enumerate_paths: Callable[[int], list[bytes]],
    *, missing: bool, directories: bool,
) -> str:
    remaining(deadline)
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            identity = _metadata(os.fstat(root_fd))
            paths = enumerate_paths(root_fd)
            before = _input_metadata(root_fd, paths, deadline, missing=missing)
            digest = hashlib.sha256()
            for path in paths:
                remaining(deadline)
                digest.update(len(path).to_bytes(8, "big") + path)
                metadata = before[path]
                if metadata is None:
                    digest.update(b"missing\0")
                    continue
                mode = metadata[2]
                digest.update(
                    str(stat.S_IFMT(mode)).encode() + b":"
                    + str(mode & 0o111).encode() + b"\0"
                )
                with _parent_fd(root_fd, path) as (parent, name):
                    if _metadata(os.stat(name, dir_fd=parent, follow_symlinks=False)) != metadata:
                        raise UserError("Input changed during fingerprint capture.")
                    if stat.S_ISLNK(mode):
                        target = os.readlink(name, dir_fd=parent)
                        digest.update(len(target).to_bytes(8, "big") + target)
                    elif stat.S_ISREG(mode):
                        if not mode & 0o444:
                            raise UserError("Unreadable fingerprint input.")
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                        try:
                            if _metadata(os.fstat(fd)) != metadata:
                                raise UserError("Input changed before fingerprint read.")
                            content = hashlib.sha256()
                            while True:
                                remaining(deadline)
                                chunk = os.read(fd, 1024 * 1024)
                                if not chunk:
                                    break
                                content.update(chunk)
                            if _metadata(os.fstat(fd)) != metadata:
                                raise UserError("Input changed during fingerprint read.")
                            digest.update(content.digest())
                        finally:
                            os.close(fd)
                    elif not (directories and stat.S_ISDIR(mode)):
                        raise UserError(f"Unsupported fingerprint input: {os.fsdecode(path)!r}")
            if paths != enumerate_paths(root_fd) or before != _input_metadata(
                root_fd, paths, deadline, missing=missing
            ):
                raise UserError("Inputs changed during fingerprint capture.")
            if identity != _metadata(os.stat(root, follow_symlinks=False)):
                raise UserError("Fingerprint root changed during capture.")
            remaining(deadline)
            return digest.hexdigest()
        finally:
            os.close(root_fd)
    except OSError as exc:
        raise UserError(f"Cannot fingerprint {root}: {exc}") from exc


def checkout_fingerprint(root: Path, deadline: float) -> str:
    root = root.resolve()
    if (root / ".git").exists():
        return _content_fingerprint(
            root, deadline, lambda _fd: _git_paths(root, deadline),
            missing=True, directories=False,
        )
    return _content_fingerprint(
        root, deadline,
        lambda fd: _tree_paths(fd, deadline, excluded=CHECKOUT_EXCLUDES, transient=set()),
        missing=False, directories=True,
    )


def index_fingerprint(root: Path, index_name: str, deadline: float) -> str:
    if index_name not in INDEX_DIRS:
        raise UserError(f"Unknown index: {index_name}")
    return _content_fingerprint(
        root.resolve() / INDEX_DIRS[index_name], deadline,
        lambda fd: _tree_paths(fd, deadline, excluded=set(), transient=INDEX_TRANSIENTS[index_name]),
        missing=False, directories=True,
    )


def _head(root: Path, deadline: float) -> str:
    if not (root / ".git").exists():
        return ""
    return run_child(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=root, timeout=remaining(deadline)
    ).stdout.strip()


def capture_checkout(root: Path, deadline: float) -> tuple[str, str]:
    head = _head(root, deadline)
    fingerprint = checkout_fingerprint(root, deadline)
    if head != _head(root, deadline):
        raise UserError("HEAD changed during checkout capture.")
    return head, fingerprint


def crg_candidate_paths(root: Path, base: str, deadline: float) -> list[str]:
    """Read native CRG diff candidates, preserving both sides of renames/copies."""
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base):
        raise UserError("Invalid Git base for CRG candidate discovery.")
    output = run_child(
        ["git", "--no-optional-locks", "diff", "--name-status", "-z", base, "--"],
        cwd=root, timeout=remaining(deadline),
    ).stdout.encode("utf-8", "surrogateescape")
    if not output:
        return []
    if not output.endswith(b"\0"):
        raise UserError("Truncated Git candidate output.")
    records = output[:-1].split(b"\0")
    paths = set()
    position = 0
    while position < len(records):
        status = records[position]
        position += 1
        scored = re.fullmatch(rb"[RCM]([0-9]{1,3})", status)
        if status not in (b"A", b"D", b"M", b"T", b"U", b"X", b"B") and not (
            scored and int(scored[1]) <= 100
        ):
            raise UserError("Invalid Git candidate status.")
        count = 2 if status.startswith((b"R", b"C")) else 1
        if position + count > len(records):
            raise UserError("Truncated Git candidate record.")
        for raw in records[position:position + count]:
            path = os.fsdecode(raw)
            if not _valid_crg_path(path):
                raise UserError("Invalid Git candidate path.")
            paths.add(path)
        position += count
    return sorted(paths, key=os.fsencode)


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
        directories[:] = []
    return tuple(sorted(repositories))


def discover_scope(path: Path, *, deadline: float) -> RepoScope:
    root = Path(path).expanduser().resolve()
    remaining(deadline)
    if not root.is_dir():
        raise UserError(f"Not a directory: {root}")
    if _has_git_marker(root):
        return _repository_scope(root, deadline=deadline)

    repositories = _nested_repositories(root, deadline=deadline)
    if repositories:
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


def child_environment(*, allow_install: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    if not allow_install:
        env.update(
            CODEGRAPH_NO_DOWNLOAD="1",
            NODE_DISABLE_COMPILE_CACHE="1",
            # 1.6.0 tries cached bundles and prunes siblings BEFORE checking
            # NO_DOWNLOAD. A device cannot contain its bundles/ directory.
            CODEGRAPH_INSTALL_DIR=os.devnull,
        )
    return env


def run_child(
    argv: Sequence[str], *, cwd: Path | None, timeout: float,
    input_text: str | None = None, allow_install: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run an argv-only child; finish all group members before returning.

    MCP servers must use execve instead: this runner captures command output.
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
            stdin=subprocess.PIPE if input_text is not None else None,
            text=False, shell=False,
            start_new_session=True, env=child_environment(allow_install=allow_install),
        )
    except OSError as exc:
        raise UserError(f"Cannot start {args[0]}: {exc}") from exc
    timed_out = False
    try:
        stdout, stderr = process.communicate(
            input=None if input_text is None else input_text.encode("utf-8"),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(process)
        stdout, stderr = process.communicate()
    finally:
        # A successful parent may have left a background index writer behind.
        _kill_group(process)
        process.wait()
        _wait_group_exit(process)
    # Preserve raw Git path bytes, including carriage returns and invalid UTF-8;
    # text-mode universal newline conversion would alias distinct filenames.
    stdout = stdout.decode("utf-8", errors="surrogateescape")
    stderr = stderr.decode("utf-8", errors="surrogateescape")
    if timed_out:
        raise UserError(f"{args[0]} timed out after {timeout:g}s.")
    if process.returncode:
        detail = " ".join((stderr or stdout).split())[:500]
        raise UserError(
            f"{args[0]} exited with status {process.returncode}"
            + (f": {detail}" if detail else ".")
        )
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def resolve_verified_tool(
    spec: ToolSpec, *, deadline: float, cwd: Path | None = None,
    allow_install: bool = False,
) -> Path:
    binary = shutil.which(spec.executable)
    if not binary:
        shim_dir = Path.home() / ".local/share/mise/shims"
        binary = shutil.which(spec.executable, path=str(shim_dir))
    remedy = "Run install-tools explicitly, then start a new host session."
    if not binary:
        raise UserError(f"Missing {spec.executable} {spec.version}. {remedy}")
    # Keep the shim name: resolving its symlink would change argv[0] to mise.
    path = Path(binary).absolute()
    verified_version(spec, path, deadline=deadline, cwd=cwd, allow_install=allow_install)
    return path


def verified_version(
    spec: ToolSpec, path: Path, *, deadline: float, cwd: Path | None = None,
    allow_install: bool = False,
) -> str:
    """Return the observed, validated version in this target's tool environment."""
    remedy = "Run install-tools explicitly, then start a new host session."
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise UserError(f"Deadline expired checking {spec.executable}. {remedy}")
    try:
        result = run_child(
            [str(path), "--version"], cwd=cwd, timeout=remaining, allow_install=allow_install
        )
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
    return match[1]


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
        existing = exclude.read_bytes() if exclude.exists() else b""
        lines = existing.splitlines()
        missing = [
            entry
            for entry in (b".codegraph/", b".code-review-graph/")
            if entry not in lines
        ]
        if not missing:
            return
        separator = (
            b"" if not existing or existing.endswith((b"\n", b"\r")) else b"\n"
        )
        updated = existing + separator + b"".join(entry + b"\n" for entry in missing)
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_bytes(updated)
    except OSError as exc:
        raise UserError(f"Cannot update Git exclude file at {exclude}: {exc}") from exc


def initialize_indexes_locked(
    root: Path,
    tools: Mapping[str, Path],
    *,
    force: bool,
    deadline: float,
) -> set[str]:
    root = Path(root).resolve()
    rebuilt = set()
    if "codegraph" in tools and (force or not (root / ".codegraph").is_dir()):
        command = "index" if (root / ".codegraph/codegraph.db").is_file() else "init"
        run_child(
            [str(tools["codegraph"]), command, str(root)],
            cwd=root,
            timeout=remaining(deadline),
        )
        rebuilt.add("codegraph")
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
        rebuilt.add("crg")
    return rebuilt


def needs_crg_repair(
    previous: FreshnessMarker | None,
    candidates_from_previous_head: list[str] | None,
    *, crg_rebuilt: bool,
) -> bool:
    if crg_rebuilt:
        return False
    if (previous is None or previous.schema_version != 2
            or previous.status != "success"
            or "crg" not in previous.versions
            or previous.crg_candidates is None):
        return True
    return bool(set(previous.crg_candidates) - set(candidates_from_previous_head))


def select_crg_base(
    root: Path, previous: FreshnessMarker | None, *, crg_rebuilt: bool, deadline: float
) -> str | None:
    if crg_rebuilt:
        return None
    candidates = None
    if (previous is not None and previous.schema_version == 2
            and previous.status == "success" and "crg" in previous.versions
            and previous.crg_candidates is not None):
        candidates = crg_candidate_paths(root, previous.head, deadline)
    if not needs_crg_repair(previous, candidates, crg_rebuilt=False):
        return previous.head
    head = _head(root, deadline)
    empty_tree = run_child(
        ["git", "hash-object", "-t", "tree", "-w", "--stdin"],
        cwd=root, timeout=remaining(deadline), input_text="",
    ).stdout.strip()
    if (not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", empty_tree)
            or len(empty_tree) != len(head)):
        raise UserError("Invalid empty-tree object ID from Git.")
    return empty_tree


def update_indexes_locked(
    root: Path, tools: Mapping[str, Path], *, deadline: float, crg_base: str | None = None
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
                *([] if crg_base is None else ["--base", crg_base]),
                "--skip-flows",
                "--repo",
                str(root),
            ],
            cwd=root,
            timeout=remaining(deadline),
        )


def capture(root: Path, tools: Mapping[str, Path], deadline: float) -> FreshnessMarker:
    """Observe a stable checkout and every selected index, without writing state."""
    root = root.resolve()
    if not tools or any(name not in TOOLS for name in tools):
        raise UserError("No valid index selection for capture.")
    versions = {
        name: verified_version(TOOLS[name], path, deadline=deadline, cwd=root)
        for name, path in tools.items()
    }
    checkout = capture_checkout(root, deadline)
    candidates = crg_candidate_paths(root, checkout[0], deadline) if "crg" in tools else None
    fingerprints = {name: index_fingerprint(root, name, deadline) for name in sorted(tools)}
    if checkout != capture_checkout(root, deadline):
        raise UserError("Checkout changed during index capture.")
    if candidates != (crg_candidate_paths(root, checkout[0], deadline) if "crg" in tools else None):
        raise UserError("Git candidates changed during index capture.")
    if fingerprints != {name: index_fingerprint(root, name, deadline) for name in sorted(tools)}:
        raise UserError("Indexes changed during capture.")
    if checkout != capture_checkout(root, deadline):
        raise UserError("Checkout changed during index capture.")
    return FreshnessMarker(
        str(root), checkout[0], versions,
        checkout[1], fingerprints, "success",
        crg_candidates=candidates,
    )


def mutate_project(
    path: Path, *, operation: Literal["setup", "update"], force: bool, deadline: float
) -> None:
    if operation not in ("setup", "update"):
        raise UserError(f"Unknown project operation: {operation}")
    scope = discover_scope(path, deadline=deadline)
    roots = setup_roots(scope)
    data = select_data_location(os.environ, read_only=False)
    for root, engines in roots:
        with root_lock(root, data, deadline):
            previous = read_marker(root, data)  # Corrupt state is never repaired implicitly.
            pending = FreshnessMarker(str(root), "", {}, "", {}, "pending")
            write_marker(root, data, pending)
            try:
                tools = {
                    name: resolve_verified_tool(TOOLS[name], deadline=deadline, cwd=root)
                    for name in engines
                }
                before = capture_checkout(root, deadline)
                before_candidates = crg_candidate_paths(root, before[0], deadline) if "crg" in tools else None
                rebuilt = set()
                if operation == "setup":
                    rebuilt = initialize_indexes_locked(root, tools, force=force, deadline=deadline)
                elif any(not (root / INDEX_DIRS[name]).is_dir() for name in tools):
                    raise UserError("Missing index; authorize setup-project first.")
                crg_base = select_crg_base(root, previous, crg_rebuilt="crg" in rebuilt, deadline=deadline) if "crg" in tools else None
                update_indexes_locked(root, tools, crg_base=crg_base, deadline=deadline)
                observed = capture(root, tools, deadline)
                if before != capture_checkout(root, deadline) or before_candidates != observed.crg_candidates:
                    raise UserError("Checkout or Git candidates changed during indexing.")
                write_marker(root, data, observed)
            except Exception as exc:
                try:
                    write_marker(root, data, replace(pending, status="failed"))
                except Exception as state_error:
                    raise UserError(f"{exc}; additionally, {state_error}") from exc
                raise


@contextmanager
def ensure_ready(
    path: Path, *, force_sync: bool, deadline: float
) -> Iterator[ReadinessResult]:
    """Hold readiness ownership through caller work and final revalidation."""
    scope = discover_scope(path, deadline=deadline)
    if scope.kind == "umbrella":
        raise UserError(
            "Umbrella scope is not initialized automatically; request authorization for setup-project."
        )
    if scope.kind not in ("repository", "worktree"):
        raise UserError("No Git checkout available.")
    root = scope.root
    data = select_data_location(os.environ, read_only=False)
    with root_lock(root, data, deadline):
        previous = read_marker(root, data)  # Preserve corruption and another lock owner's state.
        pending = FreshnessMarker(str(root), "", {}, "", {}, "pending")
        try:
            tools = {
                name: resolve_verified_tool(spec, deadline=deadline, cwd=root)
                for name, spec in TOOLS.items()
            }
            indexes_exist = all((root / name).is_dir() for name in INDEX_DIRS.values())
            observed = capture(root, tools, deadline) if indexes_exist else None
            reuse = (
                not force_sync and previous is not None
                and previous.status == "success" and previous == observed
            )
            if not reuse:
                write_marker(root, data, pending)
                before = capture_checkout(root, deadline)
                before_candidates = crg_candidate_paths(root, before[0], deadline) if "crg" in tools else None
                rebuilt = initialize_indexes_locked(root, tools, force=False, deadline=deadline)
                crg_base = select_crg_base(root, previous, crg_rebuilt="crg" in rebuilt, deadline=deadline) if "crg" in tools else None
                update_indexes_locked(root, tools, crg_base=crg_base, deadline=deadline)
                observed = capture(root, tools, deadline)
                if before != capture_checkout(root, deadline) or before_candidates != observed.crg_candidates:
                    raise UserError("Checkout or Git candidates changed during indexing.")
            yield ReadinessResult(root, tools, observed)
            if capture(root, tools, deadline) != observed:
                raise UserError("Checkout or indexes changed during hook completion.")
            write_marker(root, data, observed)
        except Exception as exc:
            try:
                write_marker(root, data, replace(pending, status="failed"))
            except Exception as state_error:
                raise UserError(f"{exc}; additionally, {state_error}") from exc
            raise


HOOK_EVENTS = {
    "hook-status": "SessionStart",
    "hook-prompt": "UserPromptSubmit",
    "hook-update": "PostToolUse",
}
WRITE_TOOLS = {"Bash", "Write", "Edit", "NotebookEdit", "apply_patch"}
ROUTING_CONTEXT = (
    "Use CodeGraph first for verbatim symbol source, callers, callees, call paths, "
    "and dynamic dispatch. Use code-review-graph first for review, blast radius, "
    "affected flows, architecture, communities, semantic search, and refactoring. "
    "Fall back to normal file/search tools when the selected graph lacks the answer. "
    "Do not use an index when readiness is missing, pending, failed, timed out, "
    "or stale; use normal file/search tools until readiness is established."
)


def hook_response(event: str, text: str) -> dict[str, object]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _hook_fallback(event: str, error: Exception) -> dict[str, object]:
    diagnostic = " ".join(str(error).split())[:600]
    return hook_response(
        event, f"Code intelligence unavailable: {diagnostic} "
        "Use normal file/search tools until readiness is established."
    )


def extract_prompt_context(stdout: str) -> str:
    # CodeGraph 1.6.0 emits raw context or empty stdout for a successful no-op.
    text = stdout.strip()
    if not text or re.fullmatch(r"<codegraph_context(?:\s[^>]*)?>[\s\S]*</codegraph_context>", text):
        return text
    raise UserError("CodeGraph prompt-hook returned malformed context.")


def handle_hook(
    command: str, payload: object, *, cwd: Path | None = None
) -> dict[str, object]:
    event = HOOK_EVENTS.get(command, "SessionStart")
    deadline = time.monotonic() + 45
    try:
        if command not in HOOK_EVENTS:
            raise UserError("Unknown lifecycle hook.")
        if not isinstance(payload, dict):
            raise UserError("Hook payload must be a JSON object.")
        if "cwd" in payload:
            value = payload["cwd"]
            if not isinstance(value, str) or not value or "\0" in value:
                raise UserError("Hook cwd must be a nonempty directory string.")
            path = Path(value)
        else:
            path = cwd if cwd is not None else Path.cwd()
        if command == "hook-update":
            tool = payload.get("tool_name", payload.get("toolName"))
            if not isinstance(tool, str):
                raise UserError("PostToolUse requires a string tool name.")
            if tool not in WRITE_TOOLS:
                return hook_response(event, "")
        with ensure_ready(path, force_sync=command == "hook-update", deadline=deadline) as ready:
            text = ROUTING_CONTEXT
            if command == "hook-prompt":
                try:
                    result = run_child(
                        [str(ready.tools["codegraph"]), "prompt-hook"],
                        cwd=ready.root, timeout=remaining(deadline),
                        input_text=json.dumps(payload),
                    )
                except UserError as exc:
                    # Child stdout/stderr may contain provisional graph context.
                    # Keep it out of fail-open output even on a nonzero exit.
                    reason = "timed out" if "timed out" in str(exc) else "failed"
                    raise UserError(f"CodeGraph prompt-hook {reason}.") from exc
                context = extract_prompt_context(result.stdout)
                text = context + "\n\n" + text if context else text
        return hook_response(event, text)
    except Exception as exc:
        return _hook_fallback(event, exc)


def observe_project(path: Path, *, deadline: float) -> dict[str, object]:
    """Read-only diagnostic: no locking, storage creation, probes, or repair."""
    report: dict[str, object] = {
        "healthy": False,
        "python": {"version": sys.version.split()[0], "executable": sys.executable},
        "mise": shutil.which("mise"),
        "plugin_root": str(PLUGIN_ROOT),
        "data": None, "scope": None, "indexes": {}, "tools": {},
        "current_head": None, "stored_head": None,
    }
    errors = []
    scope = None
    data = None
    try:
        scope = discover_scope(path, deadline=deadline)
        report["scope"] = {"kind": scope.kind, "root": str(scope.root)}
        if scope.kind == "none":
            errors.append("No Git repository or umbrella scope.")
        report["current_head"] = _head(scope.root, deadline)
    except (UserError, OSError) as exc:
        errors.append(str(exc))
    # Report the selected variable even when its path is unusable.
    source = next(
        (name for name in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA") if os.environ.get(name)),
        None,
    )
    if source:
        report["data"] = {
            "source": source, "path": os.environ[source], "writable_best_effort": False
        }
    try:
        data = select_data_location(os.environ, read_only=True)
        exists = data.path.exists()
        writable = (
            exists and os.access(data.path, os.W_OK | os.X_OK)
            and bool(data.path.stat().st_mode & 0o222)
        )
        report["data"] = {
            "source": data.source, "path": str(data.path), "exists": exists,
            "readable_best_effort": os.access(data.path, os.R_OK | os.X_OK),
            "writable_best_effort": writable,
        }
        if not exists or not writable:
            errors.append("Selected plugin storage is missing or not writable (best-effort access check).")
    except (UserError, OSError) as exc:
        errors.append(str(exc))
    engines = ("codegraph",) if scope and scope.kind == "umbrella" else ("codegraph", "crg")
    tools = {}
    for name in engines:
        spec = TOOLS[name]
        detail = {
            "required_version": spec.version,
            "executable": shutil.which(spec.executable), "version": None,
        }
        try:
            target = scope.root if scope else path.resolve()
            tools[name] = resolve_verified_tool(spec, deadline=deadline, cwd=target)
            version = verified_version(spec, tools[name], deadline=deadline, cwd=target)
            detail.update(executable=str(tools[name]), version=version)
        except (UserError, OSError) as exc:
            detail["error"] = str(exc)
            errors.append(str(exc))
        report["tools"][name] = detail
        if scope:
            index = scope.root / INDEX_DIRS[name]
            report["indexes"][name] = index.is_dir() and not index.is_symlink()
    if scope and scope.kind != "none" and data:
        try:
            before = read_marker(scope.root, data)
            if before:
                report["stored_head"] = before.head
            if len(tools) == len(engines):
                observed = capture(scope.root, tools, deadline)
                report["current_head"] = observed.head
                after = read_marker(scope.root, data)
                if before != after:
                    errors.append("State changed during observation.")
                elif before is None:
                    errors.append("No freshness state; run setup-project.")
                elif before.status != "success":
                    errors.append(f"Stored index operation is {before.status}.")
                elif before != observed:
                    errors.append("Stored checkout, HEAD, versions, or index content is stale.")
            else:
                errors.append("Cannot establish freshness without all verified tools.")
        except (UserError, OSError) as exc:
            errors.append(str(exc))
    report["healthy"] = not errors
    report["trust_reason"] = (
        "; ".join(errors) if errors
        else "Successful state matches the current checkout and indexes."
    )
    return report


def install_tools() -> int:
    mise = shutil.which("mise")
    if not mise:
        raise UserError("Install mise, then invoke install-tools explicitly.")
    for spec in TOOLS.values():
        run_child([mise, "use", "-g", spec.mise_package], cwd=None, timeout=300, allow_install=True)
    for spec in TOOLS.values():
        resolve_verified_tool(spec, deadline=time.monotonic() + 10, allow_install=True)
    return 0


def serve(engine: str) -> int:
    binary = str(resolve_verified_tool(TOOLS[engine], deadline=time.monotonic() + 10))
    args = [binary, "serve"] + (["--mcp"] if engine == "codegraph" else [])
    os.execve(binary, args, child_environment())
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
    commands.add_parser("doctor", help="Read-only diagnostics for the current directory")
    for name in HOOK_EVENTS:
        commands.add_parser(name, help="Fail-open lifecycle hook")
    status = commands.add_parser("project-status", help="Read-only project diagnostics")
    status.add_argument("path", type=Path)
    for name in ("setup-project", "setup-batch", "update-project", "update-batch"):
        operation = commands.add_parser(name)
        operation.add_argument("path", type=Path)
        if name == "setup-project":
            operation.add_argument("--force", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.command == "install-tools":
            return install_tools()
        if args.command == "serve":
            return serve(args.engine)
        if args.command in HOOK_EVENTS:
            try:
                response = handle_hook(args.command, json.load(sys.stdin))
            except Exception as exc:
                response = _hook_fallback(HOOK_EVENTS[args.command], exc)
            print(json.dumps(response))
            return 0
        if args.command in ("doctor", "project-status"):
            report = observe_project(
                Path.cwd() if args.command == "doctor" else args.path,
                deadline=time.monotonic() + 30,
            )
            print(json.dumps(report, sort_keys=True))
            return 0 if report["healthy"] else 1
        mutate_project(
            args.path,
            operation="setup" if args.command.startswith("setup-") else "update",
            force=getattr(args, "force", False), deadline=time.monotonic() + 300,
        )
        return 0
    except (UserError, OSError) as exc:
        print("code-intel: " + " ".join(str(exc).split()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
