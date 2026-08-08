"""Subcall supervisor: the process that OWNS the runner child's handle.

Usage: python _subcall_wrapper.py <workdir> <timeout_seconds> -- <argv...>

``subcalls.start_process`` spawns THIS instead of the runner directly.
It Popens the runner, waits, and records the terminal verdict in
``<workdir>/exit.json`` — so it outlives the server that spawned it, and
a RESTARTED server (which no longer owns any handle) reads the true
outcome from the state dir. It also enforces the deadline and honours a
``<workdir>/cancel`` marker, killing via the Popen handle it owns.

OS-AGNOSTIC and deliberately import-free beyond the stdlib: it is
executed by file path under the allowlisted (minimal) child environment,
so it must not import the package or anything site-installed. No os.kill,
no signals, no /proc, no platform branches.

The runner's stdout/stderr are inherited from this process (the server
pointed them at ``stdout.txt``/``stderr.txt``); a crash of this
supervisor itself lands its traceback in ``stderr.txt``.
"""
import json
import os
import signal
import subprocess
import sys
import time

_EXIT = "exit.json"
_CANCEL = "cancel"


def _write_exit(workdir: str, payload: dict) -> None:
    # First terminal verdict wins, atomically: full content goes to a
    # unique tmp file, then os.link claims the final name iff absent
    # (EEXIST on both POSIX and NTFS) — a reader can never see a torn
    # exit.json. Where hardlinks are unsupported, fall back to O_EXCL
    # direct write (readers tolerate a transient parse failure).
    final = os.path.join(workdir, _EXIT)
    tmp = os.path.join(workdir, f"{_EXIT}.{os.getpid()}.{time.time_ns()}.tmp")
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    try:
        os.link(tmp, final)
    except FileExistsError:
        pass
    except OSError:
        try:
            fd = os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            pass
        else:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _signal_tree(proc, sig) -> None:
    """Signal the runner's whole group where the platform has groups, the
    leader alone where it does not. Capability-detected (`os.killpg` /
    `os.getpgid`), never branched on a platform name; a group kill that
    fails falls back to the ordinary terminate/kill on the handle."""
    killpg, getpgid = getattr(os, "killpg", None), getattr(os, "getpgid", None)
    if killpg and getpgid:
        try:
            killpg(getpgid(proc.pid), sig)
            return
        except OSError:
            pass
    (proc.kill if sig == getattr(signal, "SIGKILL", None) else proc.terminate)()


def main() -> int:
    args = sys.argv[1:]
    sep = args.index("--")
    workdir, timeout_s = args[0], float(args[1])
    argv = args[sep + 1:]
    exe = argv[0]
    # Re-verify ADJACENT to the true exec (the server verified the same
    # path just before spawning us; this is the narrowest point we can
    # check without closing the TOCTOU window — see runners.verified_path).
    if not (os.path.isabs(exe) and os.path.isfile(exe) and os.access(exe, os.X_OK)):
        _write_exit(workdir, {"exit_code": None, "timed_out": False, "cancelled": False,
                              "error": f"runner path failed verification at exec time: {exe!r}"})
        return 1
    cancel = os.path.join(workdir, _CANCEL)
    deadline = time.monotonic() + timeout_s
    try:
        # cwd/env/stdout/stderr inherited from this supervisor as the
        # server set them; never PATH-resolved (exe is absolute).
        # A real runner spawns children of its own (MCP servers, Bash), and
        # signalling the leader alone leaves them editing the project after
        # the subcall is recorded as timed out. Give it its own session so
        # the whole tree can be signalled — asked for by CAPABILITY, never
        # by platform name, and retried plainly where it is unsupported.
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        except (ValueError, AttributeError, NotImplementedError, OSError):
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
    except OSError as e:
        _write_exit(workdir, {"exit_code": None, "timed_out": False, "cancelled": False,
                              "error": f"exec failed: {e}"})
        return 1
    timed_out = cancelled = False
    rc = None
    while True:
        try:
            rc = proc.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            pass
        cancelled = os.path.exists(cancel)
        timed_out = not cancelled and time.monotonic() > deadline
        if cancelled or timed_out:
            _signal_tree(proc, signal.SIGTERM)
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _signal_tree(proc, signal.SIGKILL)
                try:
                    rc = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    rc = None
            break
    _write_exit(workdir, {"exit_code": rc, "timed_out": timed_out, "cancelled": cancelled})
    return 0


if __name__ == "__main__":
    sys.exit(main())
