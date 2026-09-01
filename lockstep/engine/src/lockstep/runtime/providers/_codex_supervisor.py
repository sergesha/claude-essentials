"""Small detached process supervisor for one prepared Codex launch.

The parent commits an owner-only immutable launch body before starting this
module.  This process performs exactly one argv-array spawn, bounds output while
it is produced, and publishes a terminal receipt only after the process group is
quiescent.  It has no workflow, ledger, or checkpoint access.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from threading import Event, Thread

_MAX_SPEC_BYTES = 2 * 1024 * 1024


def _atomic_json(path: Path, value: object) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_spec(path: Path, expected_digest: str) -> dict[str, object]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if info.st_size > _MAX_SPEC_BYTES:
            raise ValueError("launch body exceeds supervisor admission limit")
        encoded = os.read(descriptor, _MAX_SPEC_BYTES + 1)
    finally:
        os.close(descriptor)
    if hashlib.sha256(encoded).hexdigest() != expected_digest:
        raise ValueError("launch body digest mismatch")
    value = json.loads(encoded)
    required = {
        "schema",
        "argv",
        "cwd",
        "environment",
        "executable_identity",
        "credential_identity_digest",
        "stdin",
        "stdout",
        "stderr",
        "supervisor_ready",
        "alive",
        "go",
        "cancel",
        "started",
        "terminal",
        "deadline_epoch",
        "max_stdout_bytes",
        "max_stderr_bytes",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid supervisor launch body")
    if value["schema"] != "lockstep.codex-supervisor/v1":
        raise ValueError("unsupported supervisor launch body")
    return value


def _read_identity(path: Path) -> tuple[os.stat_result, str]:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("bound launch file is not regular")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
        )
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("bound launch file changed while hashing")
        return before, digest.hexdigest()
    finally:
        os.close(descriptor)


def _verify_bound_files(spec: dict[str, object], argv: list[str]) -> None:
    expected = spec["executable_identity"]
    if not isinstance(expected, dict) or set(expected) != {
        "device",
        "inode",
        "size",
        "mtime_ns",
        "sha256",
    }:
        raise ValueError("invalid executable identity commitment")
    executable = Path(argv[0])
    info, sha256 = _read_identity(executable)
    observed = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "sha256": sha256,
    }
    if observed != expected:
        raise ValueError("Codex executable identity changed at inner spawn")

    credential = Path(str(spec["environment"]["CODEX_HOME"])) / "auth.json"
    expected_credential = spec["credential_identity_digest"]
    if not credential.exists() and not credential.is_symlink():
        observed_credential = None
    else:
        info, sha256 = _read_identity(credential)
        values = {
            "schema": "lockstep.codex-credential/v1",
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode": info.st_mode,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "sha256": sha256,
            "audience": "openai-codex",
        }
        observed_credential = hashlib.sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    if observed_credential != expected_credential:
        raise ValueError("Codex credential identity changed at inner spawn")


def _capture(stream, path: Path, limit: int, overflow: Event) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    written = 0
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            remaining = limit - written
            if remaining > 0:
                os.write(descriptor, chunk[:remaining])
                written += min(len(chunk), remaining)
            if len(chunk) > remaining:
                overflow.set()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
        stream.close()


def _kill_group(process_group: int) -> None:
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _terminate_group(process_group: int) -> None:
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _group_is_dead(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_group_dead(process_group: int) -> None:
    delay = 0.02
    while not _group_is_dead(process_group):
        _kill_group(process_group)
        time.sleep(delay)
        delay = min(delay * 2, 10.0)


def _finish_capture(process_group: int, readers: tuple[Thread, ...]) -> None:
    _wait_group_dead(process_group)
    for reader in readers:
        reader.join()


def _publish_terminal(
    spec: dict[str, object],
    *,
    returncode: int,
    overflow: bool,
    timed_out: bool,
    quiescent: bool,
    termination_reason: str,
) -> None:
    paths = (Path(str(spec["stdout"])), Path(str(spec["stderr"])))
    for output in paths:
        if not output.exists():
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
    stdout, stderr = (path.read_bytes() for path in paths)
    _atomic_json(
        Path(str(spec["terminal"])),
        {
            "schema": "lockstep.codex-terminal/v1",
            "returncode": returncode,
            "overflow": overflow,
            "timed_out": timed_out,
            "quiescent": quiescent,
            "termination_reason": termination_reason,
            "stdout_size": len(stdout),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_size": len(stderr),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        },
    )


def _launch_inputs(
    spec: dict[str, object],
) -> tuple[list[str], dict[str, str]]:
    argv = spec["argv"]
    environment = spec["environment"]
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
        or not isinstance(environment, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in environment.items()
        )
    ):
        raise ValueError("invalid supervisor argv or environment")
    return argv, environment


def _publish_prelaunch_terminal(spec: dict[str, object], reason: str) -> None:
    values = {
        "cancelled": (130, False),
        "deadline": (124, True),
        "spawn_failed": (127, False),
    }
    returncode, timed_out = values[reason]
    _publish_terminal(
        spec,
        returncode=returncode,
        overflow=False,
        timed_out=timed_out,
        quiescent=True,
        termination_reason=reason,
    )


def _await_launch_permission(
    spec: dict[str, object],
    go: Path,
    cancel: Path,
) -> str | None:
    while not go.is_file():
        if cancel.is_file():
            return "cancelled"
        if time.time() >= float(spec["deadline_epoch"]):
            return "deadline"
        time.sleep(0.02)
    return None


def _spawn_inner_process(
    spec: dict[str, object],
    argv: list[str],
    environment: dict[str, str],
    cancel: Path,
) -> tuple[subprocess.Popen[bytes] | None, bytes, str | None]:
    try:
        stdin_bytes = Path(str(spec["stdin"])).read_bytes()
        _verify_bound_files(spec, argv)
        if time.time() >= float(spec["deadline_epoch"]):
            return None, b"", "deadline"
        if cancel.is_file():
            return None, b"", "cancelled"
        process = subprocess.Popen(
            argv,
            cwd=str(spec["cwd"]),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
        return process, stdin_bytes, None
    except (OSError, ValueError, KeyError, TypeError):
        return None, b"", "spawn_failed"


def _start_capture(
    spec: dict[str, object],
    process: subprocess.Popen[bytes],
    stdin_bytes: bytes,
) -> tuple[bool, Event, tuple[Thread, Thread]]:
    assert (
        process.stdin is not None
        and process.stdout is not None
        and process.stderr is not None
    )
    stdin_failed = False
    try:
        process.stdin.write(stdin_bytes)
    except OSError:
        stdin_failed = True
    finally:
        try:
            process.stdin.close()
        except OSError:
            stdin_failed = True
    overflow = Event()
    readers = (
        Thread(
            target=_capture,
            args=(
                process.stdout,
                Path(str(spec["stdout"])),
                int(spec["max_stdout_bytes"]),
                overflow,
            ),
        ),
        Thread(
            target=_capture,
            args=(
                process.stderr,
                Path(str(spec["stderr"])),
                int(spec["max_stderr_bytes"]),
                overflow,
            ),
        ),
    )
    for reader in readers:
        reader.start()
    return stdin_failed, overflow, readers


def _monitor_process(
    spec: dict[str, object],
    process: subprocess.Popen[bytes],
    overflow: Event,
    cancel: Path,
) -> bool:
    while process.poll() is None:
        if (
            overflow.is_set()
            or cancel.is_file()
            or time.time() >= float(spec["deadline_epoch"])
        ):
            timed_out = not overflow.is_set() and not cancel.is_file()
            _kill_group(process.pid)
            return timed_out
        time.sleep(0.02)
    return False


def _terminal_reason(
    *,
    stdin_failed: bool,
    overflow: bool,
    cancelled: bool,
    timed_out: bool,
) -> str:
    if stdin_failed:
        return "stdin_failed"
    if overflow:
        return "output_overflow"
    if cancelled:
        return "cancelled"
    if timed_out:
        return "deadline"
    return "exited"


def _contain_spawned_process(
    spec: dict[str, object],
    process: subprocess.Popen[bytes],
    capture: tuple[bool, Event, tuple[Thread, Thread]] | None,
) -> None:
    """Retain ownership after Popen and prevent a replacement attempt."""

    if capture is None:
        try:
            capture = _start_capture(spec, process, b"")
        except BaseException:  # noqa: BLE001 - the spawned child remains owned
            capture = None
    _terminate_group(process.pid)
    _kill_group(process.pid)
    returncode = process.wait()
    if capture is not None:
        _stdin_failed, overflow, readers = capture
        _finish_capture(process.pid, readers)
    else:
        overflow = Event()
        _wait_group_dead(process.pid)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None or stream.closed:
            continue
        try:
            stream.close()
        except OSError:
            pass
    try:
        _publish_terminal(
            spec,
            returncode=127 if returncode == 0 else returncode,
            overflow=overflow.is_set(),
            timed_out=False,
            quiescent=True,
            termination_reason="stdin_failed",
        )
    except (OSError, ValueError):
        pass


class _CodexSupervisorTransaction:
    def __init__(
        self,
        spec: dict[str, object],
        argv: list[str],
        environment: dict[str, str],
    ) -> None:
        self._spec = spec
        self._argv = argv
        self._environment = environment

    def execute(self) -> int:
        spec = self._spec
        argv = self._argv
        environment = self._environment
        alive_descriptor = os.open(
            Path(str(spec["alive"])), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            fcntl.flock(alive_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _atomic_json(
                Path(str(spec["supervisor_ready"])),
                {"schema": "lockstep.codex-supervisor-ready/v1", "pid": os.getpid()},
            )
            go = Path(str(spec["go"]))
            cancel = Path(str(spec["cancel"]))
            prelaunch_reason = _await_launch_permission(spec, go, cancel)
            if prelaunch_reason is not None:
                _publish_prelaunch_terminal(spec, prelaunch_reason)
                return 0
            process, stdin_bytes, prelaunch_reason = _spawn_inner_process(
                spec, argv, environment, cancel
            )
            if prelaunch_reason is not None:
                _publish_prelaunch_terminal(spec, prelaunch_reason)
                return 0
            assert process is not None
            capture: tuple[bool, Event, tuple[Thread, Thread]] | None = None
            try:
                _atomic_json(
                    Path(str(spec["started"])),
                    {
                        "schema": "lockstep.codex-started/v1",
                        "pid": process.pid,
                        "pgid": process.pid,
                    },
                )
                capture = _start_capture(spec, process, stdin_bytes)
                stdin_failed, overflow, readers = capture
                if stdin_failed:
                    _kill_group(process.pid)
                timed_out = _monitor_process(spec, process, overflow, cancel)
                returncode = process.wait()
                if stdin_failed:
                    returncode = 127
                _kill_group(process.pid)
                _finish_capture(process.pid, readers)
                _publish_terminal(
                    spec,
                    returncode=returncode,
                    overflow=overflow.is_set(),
                    timed_out=timed_out,
                    quiescent=True,
                    termination_reason=_terminal_reason(
                        stdin_failed=stdin_failed,
                        overflow=overflow.is_set(),
                        cancelled=cancel.is_file(),
                        timed_out=timed_out,
                    ),
                )
            except BaseException:  # noqa: BLE001 - the spawned child remains owned
                _contain_spawned_process(spec, process, capture)
            return 0
        finally:
            os.close(alive_descriptor)


def run(path: Path, expected_digest: str) -> int:
    spec = _read_spec(path, expected_digest)
    argv, environment = _launch_inputs(spec)
    transaction = _CodexSupervisorTransaction(spec, argv, environment)
    return transaction.execute()


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        return run(Path(sys.argv[1]), sys.argv[2])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # The parent treats a missing terminal receipt after possible supervisor
        # creation as indeterminate.  Keep diagnostics bounded and local.
        message = str(exc).replace("\n", " ")[:512]
        os.write(2, f"lockstep Codex supervisor: {message}\n".encode())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
