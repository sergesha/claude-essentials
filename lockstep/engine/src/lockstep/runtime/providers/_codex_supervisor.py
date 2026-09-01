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
import re
import secrets
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
        "launch_record",
        "launch_record_digest",
        "launch_ref",
        "request_digest",
        "runner_binding_digest",
        "workspace_ref",
        "stdin",
        "stdout",
        "stderr",
        "supervisor_ready",
        "alive",
        "go",
        "cancel",
        "started",
        "terminal",
        "effect_id",
        "public_launch_ref",
        "start_ref",
        "spawn_fence",
        "public_start",
        "public_terminal",
        "deadline_epoch",
        "max_stdout_bytes",
        "max_stderr_bytes",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid supervisor launch body")
    if value["schema"] != "lockstep.codex-supervisor/v1":
        raise ValueError("unsupported supervisor launch body")
    refs = (value["public_launch_ref"], value["start_ref"])
    if (
        any(
            not isinstance(item, str)
            or re.fullmatch(r"[0-9a-f]{64}", item) is None
            for item in refs
        )
        or refs[0] == refs[1]
        or not isinstance(value["effect_id"], str)
        or not value["effect_id"]
    ):
        raise ValueError("invalid public launch references")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_document(value: object) -> bytes:
    return _canonical(value) + b"\n"


def _write_all(descriptor: int, encoded: bytes) -> None:
    position = 0
    while position < len(encoded):
        written = os.write(descriptor, encoded[position:])
        if written <= 0:
            raise OSError("short public receipt write")
        position += written


def _verify_public_final(
    directory_descriptor: int, basename: str, encoded: bytes
) -> None:
    descriptor = os.open(
        basename,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_descriptor,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("unsafe public receipt ownership or mode")
        observed = bytearray()
        while len(observed) <= len(encoded):
            chunk = os.read(descriptor, min(64 * 1024, len(encoded) + 1 - len(observed)))
            if not chunk:
                break
            observed.extend(chunk)
        if bytes(observed) != encoded:
            raise ValueError("public receipt bytes do not match staged bytes")
    finally:
        os.close(descriptor)


def _publish_public_record(path: Path, value: object) -> None:
    """Publish one owner-only canonical record without a replacement path."""

    encoded = _canonical_document(value)
    directory_descriptor = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    stage = f".{path.name}.{secrets.token_hex(16)}.stage"
    stage_created = False
    try:
        descriptor = os.open(
            stage,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        stage_created = True
        try:
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.link(
            stage,
            path.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        _verify_public_final(directory_descriptor, path.name, encoded)
        os.unlink(stage, dir_fd=directory_descriptor)
        stage_created = False
        os.fsync(directory_descriptor)
    finally:
        if stage_created:
            try:
                os.unlink(stage, dir_fd=directory_descriptor)
            except OSError:
                pass
        os.close(directory_descriptor)


def _public_record_matches(path: Path, value: object) -> bool:
    try:
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            _verify_public_final(
                directory_descriptor, path.name, _canonical_document(value)
            )
        finally:
            os.close(directory_descriptor)
        return True
    except (OSError, ValueError):
        return False


def _public_start_value(spec: dict[str, object]) -> dict[str, object]:
    return {
        "schema": "lockstep.codex-public-start/v1",
        "effect_id": spec["effect_id"],
        "public_launch_ref": spec["public_launch_ref"],
        "start_ref": spec["start_ref"],
    }


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


def _verify_private_joins(spec: dict[str, object]) -> None:
    path = Path(str(spec["launch_record"]))
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        encoded = os.read(descriptor, _MAX_SPEC_BYTES + 1)
        if os.read(descriptor, 1):
            raise ValueError("private launch record exceeds supervisor bound")
    finally:
        os.close(descriptor)
    if hashlib.sha256(encoded).hexdigest() != spec["launch_record_digest"]:
        raise ValueError("private launch record digest changed before spawn")
    try:
        raw = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("private launch record is malformed") from exc
    expected = {
        "effect_id": spec["effect_id"],
        "launch_ref": spec["launch_ref"],
        "request_digest": spec["request_digest"],
        "runner_binding_digest": spec["runner_binding_digest"],
        "workspace_ref": spec["workspace_ref"],
    }
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != "lockstep.codex-launch/v1"
        or {key: raw.get(key) for key in expected} != expected
    ):
        raise ValueError("private launch joins changed before spawn")


def _capture_chunks(stream, descriptor: int, limit: int, overflow: Event) -> None:
    written = 0
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            return
        remaining = limit - written
        if remaining > 0:
            retained = chunk[:remaining]
            _write_all(descriptor, retained)
            written += len(retained)
        if len(chunk) > remaining:
            overflow.set()


def _close_descriptor(descriptor: int, failures: list[str]) -> None:
    try:
        os.close(descriptor)
    except OSError:
        failures.append("capture_close_failed")


def _close_stream(stream, failures: list[str]) -> None:
    try:
        stream.close()
    except (OSError, ValueError):
        failures.append("capture_stream_close_failed")


def _capture(
    stream,
    path: Path,
    limit: int,
    overflow: Event,
    failures: list[str] | None = None,
    complete: Event | None = None,
) -> None:
    recorded = failures if failures is not None else []
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        _capture_chunks(stream, descriptor, limit, overflow)
        os.fsync(descriptor)
    except (OSError, ValueError):
        recorded.append("capture_failed")
    finally:
        if descriptor is not None:
            _close_descriptor(descriptor, recorded)
        _close_stream(stream, recorded)
        if complete is not None:
            complete.set()


def _kill_group(process_group: int) -> None:
    try:
        os.killpg(process_group, signal.SIGKILL)
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


def _finish_capture(
    process_group: int,
    readers: tuple[Thread, ...],
) -> bool:
    _wait_group_dead(process_group)
    for reader in readers:
        reader.join()
    return _group_is_dead(process_group) and all(not reader.is_alive() for reader in readers)


def _publish_terminal(
    spec: dict[str, object],
    *,
    returncode: int,
    overflow: bool,
    timed_out: bool,
    quiescent: bool,
    termination_reason: str,
    public: bool = False,
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
    if public:
        safe = {
            "effect_id": spec["effect_id"],
            "overflow": overflow,
            "public_launch_ref": spec["public_launch_ref"],
            "quiescent": quiescent,
            "returncode": returncode,
            "start_ref": spec["start_ref"],
            "termination_reason": termination_reason,
            "timed_out": timed_out,
        }
        safe["terminal_ref"] = hashlib.sha256(
            b"lockstep-public-terminal-v1\0" + _canonical(safe)
        ).hexdigest()
        _publish_public_record(Path(str(spec["public_terminal"])), safe)


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


def _publish_prelaunch_terminal(
    spec: dict[str, object], reason: str, *, public: bool = False
) -> None:
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
        public=public,
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
        _verify_private_joins(spec)
        if time.time() >= float(spec["deadline_epoch"]):
            return None, b"", "deadline"
        if cancel.is_file():
            return None, b"", "cancelled"
        _publish_public_record(
            Path(str(spec["spawn_fence"])), _public_start_value(spec)
        )
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


def _send_stdin(process: subprocess.Popen[bytes], stdin_bytes: bytes) -> bool:
    assert (
        process.stdin is not None
        and process.stdout is not None
        and process.stderr is not None
    )
    stdin_failed = False
    try:
        if not process.stdin.closed:
            process.stdin.write(stdin_bytes)
    except (OSError, ValueError):
        stdin_failed = True
    finally:
        try:
            process.stdin.close()
        except OSError:
            stdin_failed = True
    return stdin_failed


def _capture_threads(
    spec: dict[str, object],
    process: subprocess.Popen[bytes],
    overflow: Event,
    failures: list[str],
) -> tuple[Thread, Thread]:
    assert process.stdout is not None and process.stderr is not None
    streams = (process.stdout, process.stderr)
    paths = (Path(str(spec["stdout"])), Path(str(spec["stderr"])))
    limits = (int(spec["max_stdout_bytes"]), int(spec["max_stderr_bytes"]))
    return tuple(
        Thread(target=_capture, args=(stream, path, limit, overflow, failures))
        for stream, path, limit in zip(streams, paths, limits, strict=True)
    )


def _start_readers(
    readers: tuple[Thread, Thread],
    streams: tuple[object, object],
    failures: list[str],
) -> tuple[Thread, ...]:
    started: list[Thread] = []
    for reader, stream in zip(readers, streams, strict=True):
        try:
            reader.start()
            started.append(reader)
        except RuntimeError:
            failures.append("capture_start_failed")
            _close_stream(stream, failures)
    return tuple(started)


def _start_capture(
    spec: dict[str, object],
    process: subprocess.Popen[bytes],
    stdin_bytes: bytes,
) -> tuple[bool, Event, tuple[Thread, ...], list[str]]:
    stdin_failed = _send_stdin(process, stdin_bytes)
    overflow = Event()
    failures: list[str] = []
    readers = _capture_threads(spec, process, overflow, failures)
    assert process.stdout is not None and process.stderr is not None
    started = _start_readers(readers, (process.stdout, process.stderr), failures)
    return stdin_failed, overflow, started, failures


def _close_stdin(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is None:
        return
    try:
        process.stdin.close()
    except OSError:
        pass


def _terminate_group(process_group: int, signal_number: int) -> None:
    try:
        os.killpg(process_group, signal_number)
    except ProcessLookupError:
        pass


def _containment_capture(
    spec: dict[str, object],
    process: subprocess.Popen[bytes],
    capture: tuple[bool, Event, tuple[Thread, ...], list[str]] | None,
) -> tuple[bool, Event, tuple[Thread, ...], list[str]]:
    if capture is not None:
        return capture
    try:
        return _start_capture(spec, process, b"")
    except BaseException:  # noqa: BLE001 - irreversible child ownership
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                _close_stream(stream, [])
        return True, Event(), (), ["capture_start_failed"]


def _termination_grace(process: subprocess.Popen[bytes], failures: list[str]) -> None:
    try:
        _terminate_group(process.pid, signal.SIGTERM)
    except BaseException:  # noqa: BLE001 - KILL phase remains mandatory
        failures.append("term_failed")
    deadline = time.monotonic() + 2.0
    try:
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if process.poll() is None:
            _kill_group(process.pid)
    except BaseException:  # noqa: BLE001 - exact child remains owned
        failures.append("termination_monitor_failed")
        try:
            _kill_group(process.pid)
        except BaseException:  # noqa: BLE001 - recorded for fail-stuck phase
            failures.append("kill_failed")


def _containment_finish(
    process: subprocess.Popen[bytes],
    readers: tuple[Thread, ...],
) -> tuple[int, bool]:
    returncode = process.wait()
    _kill_group(process.pid)
    return returncode, _finish_capture(process.pid, readers)


def _publish_containment_terminal(
    spec: dict[str, object],
    *,
    stdin_failed: bool,
    overflow: Event,
    returncode: int,
    quiescent: bool,
) -> None:
    try:
        _publish_terminal(
            spec,
            returncode=127 if stdin_failed else returncode,
            overflow=overflow.is_set(),
            timed_out=False,
            quiescent=quiescent,
            termination_reason=(
                "stdin_failed" if stdin_failed else "receipt_publication_failed"
            ),
            public=True,
        )
    except (OSError, ValueError):
        pass


def _contain_receipt_publication_failure(
    spec: dict[str, object],
    process: subprocess.Popen[bytes],
    capture: tuple[bool, Event, tuple[Thread, ...], list[str]] | None = None,
) -> None:
    """Keep ownership after Popen and contain without a replacement attempt."""

    failures: list[str] = []
    try:
        _close_stdin(process)
    except BaseException:  # noqa: BLE001 - irreversible child ownership must not unwind
        failures.append("stdin_close_failed")
    stdin_failed, overflow, readers, capture_failures = _containment_capture(
        spec, process, capture
    )
    failures.extend(capture_failures)
    _termination_grace(process, failures)
    returncode, quiescent = _containment_finish(process, readers)
    _publish_containment_terminal(
        spec,
        stdin_failed=stdin_failed,
        overflow=overflow,
        returncode=returncode,
        quiescent=quiescent,
    )


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


def _execute_spawned(
    spec: dict[str, object],
    process: subprocess.Popen[bytes],
    stdin_bytes: bytes,
    cancel: Path,
) -> None:
    capture: tuple[bool, Event, tuple[Thread, ...], list[str]] | None = None
    try:
        _publish_public_record(
            Path(str(spec["public_start"])), _public_start_value(spec)
        )
        _atomic_json(
            Path(str(spec["started"])),
            {
                "schema": "lockstep.codex-started/v1",
                "pid": process.pid,
                "pgid": process.pid,
            },
        )
        capture = _start_capture(spec, process, stdin_bytes)
        stdin_failed, overflow, readers, failures = capture
        if stdin_failed or failures:
            raise OSError("post-spawn capture admission failed")
        timed_out = _monitor_process(spec, process, overflow, cancel)
        returncode = process.wait()
        _kill_group(process.pid)
        quiescent = _finish_capture(process.pid, readers)
        if not quiescent:
            raise OSError("post-spawn capture did not become quiescent")
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
            public=True,
        )
    except BaseException:  # noqa: BLE001 - irreversible child ownership must not unwind
        _contain_receipt_publication_failure(spec, process, capture)


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
                fenced = _public_record_matches(
                    Path(str(spec["spawn_fence"])), _public_start_value(spec)
                )
                _publish_prelaunch_terminal(
                    spec, prelaunch_reason, public=fenced
                )
                return 0
            assert process is not None
            _execute_spawned(spec, process, stdin_bytes, cancel)
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
