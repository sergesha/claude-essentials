#!/usr/bin/env python3
"""Credentialless local executable for production-adapter integration tests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path, PurePosixPath


def _workspace(argv: list[str]) -> Path:
    try:
        supplied = Path(argv[argv.index("-C") + 1])
    except (ValueError, IndexError) as exc:
        raise SystemExit("controlled executable requires -C WORKSPACE") from exc
    return supplied.resolve(strict=True)


def _artifact_path(prompt: str) -> PurePosixPath | None:
    match = re.search(r"^Artifact path: (.+)$", prompt, flags=re.MULTILINE)
    if match is None:
        return None
    path = PurePosixPath(match.group(1))
    if path.is_absolute() or ".." in path.parts or path.as_posix() in {"", "."}:
        raise SystemExit("unsafe controlled artifact path")
    return path


def _snapshot_digest(workspace: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        item for item in workspace.rglob("*") if item.is_file() and ".git" not in item.parts
    ):
        relative = path.relative_to(workspace).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _await_controlled_release() -> Path | None:
    barrier = Path(os.environ["TMPDIR"]) / "lockstep-controlled-two-process-barrier"
    if not (barrier / "hold").is_dir():
        return None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (barrier / "release").is_file():
            return barrier
        time.sleep(0.01)
    raise SystemExit("controlled release gate timed out")


def _await_parallel_peer() -> None:
    barrier = Path(os.environ["TMPDIR"]) / "lockstep-controlled-two-process-barrier"
    if not barrier.is_dir():
        time.sleep(0.2)
        return
    barrier = barrier.resolve(strict=True)
    marker = barrier / f"{os.getpid()}.ready"
    marker.write_bytes(b"")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        peers = tuple(barrier.glob("*.ready"))
        if len(peers) == 2:
            return
        if len(peers) > 2:
            raise SystemExit("controlled barrier admitted more than two processes")
        time.sleep(0.01)
    raise SystemExit("controlled barrier timed out waiting for two processes")


def _exec_pinned(argv: list[str]) -> None:
    if not argv or argv[0] != "sandbox":
        return
    try:
        separator = argv.index("--")
        cwd = Path(argv[argv.index("--cd") + 1]).resolve(strict=True)
    except (ValueError, IndexError, OSError) as exc:
        raise SystemExit("invalid controlled pinned invocation") from exc
    command = argv[separator + 1 :]
    if not command:
        raise SystemExit("controlled pinned invocation has no command")
    os.chdir(cwd)
    os.execvpe(command[0], command, dict(os.environ))


def main() -> int:
    _exec_pinned(sys.argv[1:])
    prompt = sys.stdin.read()
    workspace = _workspace(sys.argv[1:])
    start_ns = time.monotonic_ns()
    snapshot_digest = _snapshot_digest(workspace)
    _await_parallel_peer()
    release_gate = _await_controlled_release()
    end_ns = time.monotonic_ns()
    artifact = _artifact_path(prompt)
    if artifact is not None:
        destination = workspace.joinpath(*artifact.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "# Findings\n"
            "Controlled evidence-backed review.\n"
            f"snapshot_sha256: {snapshot_digest}\n"
            f"started_ns: {start_ns}\n"
            f"ended_ns: {end_ns}\n\n"
            "# Verdict\n"
            "PASS\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "controlled effect completed",
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    if release_gate is not None:
        (release_gate / f"{os.getpid()}.completed").write_bytes(b"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
