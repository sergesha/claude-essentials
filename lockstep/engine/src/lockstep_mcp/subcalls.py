"""Subcall process lifecycle + the two graph hooks (spawn/poll).

Restart truth model
-------------------
The spawning server does NOT own the outcome. Every subcall is started
under a small supervisor (``_subcall_wrapper.py``, a sibling file in
this installed package) which owns the child handle, enforces the
deadline, and records the terminal verdict in ``<workdir>/exit.json`` —
first writer wins, atomically. ``probe`` forms its verdict ONLY from the
state-dir files (``proc.json``, ``exit.json``, stdout/stderr), never
from an in-process handle, so a restarted server reads the same truth
the original would; the retained Popen handles exist solely to reap
supervisor zombies, never to decide status. If BOTH the server and the
supervisor die, no verdict can appear — any probe past the recorded
deadline then claims a terminal ``timed_out`` verdict itself, so
"running" cannot outlive the deadline.

OS-AGNOSTIC: stdlib subprocess/os only; no fcntl, /proc, ps, os.kill,
signal numbers, or platform branches. Killing is done exclusively by the
supervisor via the Popen handle it owns; this side requests it by
touching ``<workdir>/cancel``.

Security boundary (stated honestly)
-----------------------------------
argv[0] is re-verified with ``runners.verified_path`` immediately before
the spawn, and the supervisor re-checks it once more adjacent to its own
exec. That NARROWS the time-of-check->time-of-use window; it cannot
close it for a binary path the same OS user can rewrite (see
``runners.verified_path``). argv/env must come from
``runners.build_argv``/``runners.child_env`` — the hooks fail closed on
``_subcall_env=None`` precisely so the allowlist cannot be bypassed by
omission. ``validate_resume_session``/``safe_argv`` gate the ONE
worker-influenced token that sits before build_argv's ``--`` terminator.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from lockstep_mcp.runners import RunnerError, RunnerSpec, build_argv, verified_path
from lockstep_mcp.runs import RunIndex  # no cycle: runs imports only locking

_PROC = "proc.json"
_OUT = "stdout.txt"
_ERR = "stderr.txt"
_EXIT = "exit.json"
_CANCEL = "cancel"
_CLAIM = "claim"
_WRAPPER = Path(__file__).with_name("_subcall_wrapper.py")

# Popen handles of supervisors WE spawned — kept only so finished
# supervisors get reaped (no zombies); every status verdict comes from
# the files, so a restarted server (empty registry) loses nothing.
_HANDLES: dict[str, subprocess.Popen] = {}


def _sweep_handles() -> None:
    for key, proc in list(_HANDLES.items()):
        if proc.poll() is not None:
            _HANDLES.pop(key, None)


# --- resume_session shape gate ----------------------------------------------

_RESUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_resume_session(resume_session: Any) -> str:
    """resume_session is the ONE worker-influenced token build_argv places
    BEFORE the ``--`` terminator (in ``--resume``'s value slot); a
    ``-``-prefixed value could parse as a flag there in a commander-style
    CLI. Shape-gate it before it ever reaches build_argv."""
    if not isinstance(resume_session, str) or not _RESUME_RE.fullmatch(resume_session):
        raise RunnerError(
            f"resume_session fails the shape gate ^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$: {resume_session!r}"
        )
    return resume_session


def safe_argv(spec: RunnerSpec, prompt: str, model: str | None = None,
              resume_session: str | None = None) -> list[str]:
    """The only sanctioned way to build a subcall argv: shape-gates
    resume_session, then delegates to the frozen ``runners.build_argv``
    (model allowlist, ``--`` terminator, prompt last)."""
    if resume_session is not None:
        resume_session = validate_resume_session(resume_session)
    return build_argv(spec, prompt, model, resume_session)


# --- process layer -----------------------------------------------------------

def start_process(argv: list[str], cwd: str, env: dict | None, workdir: Path,
                  timeout_minutes: int) -> dict:
    """Spawn ``argv`` under the supervisor; write ``proc.json``. Raises
    RunnerError on an unverified argv[0] or a second start of the same
    workdir (one workdir == at most one session, ever)."""
    if not argv:
        raise RunnerError("subcall argv is empty")
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    _sweep_handles()
    # Single-start claim: O_CREAT|O_EXCL is the atomic test-and-set; a
    # concurrent/repeated start must never forge a second agent session.
    claim = workdir / _CLAIM
    try:
        os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        raise RunnerError(f"subcall already started in {workdir}") from None
    try:
        # Obligation: re-verify IMMEDIATELY before the spawn and exec
        # exactly the returned path (the supervisor re-checks once more,
        # adjacent to the true exec). Narrows the TOCTOU window; cannot
        # close it — see runners.verified_path.
        exe = verified_path(RunnerSpec(
            name=Path(str(argv[0])).name or str(argv[0]), path=str(argv[0]),
            models=["unused-by-verified-path"], timeout_minutes=int(timeout_minutes),
            max_subcalls_per_run=0, max_fractal_depth=0))
        rest = [str(a) for a in argv[1:]]
        full = [sys.executable, str(_WRAPPER), str(workdir),
                str(int(timeout_minutes) * 60), "--", exe, *rest]
        # stdout/stderr files are opened here and inherited down to the
        # runner child; our copies close right after Popen — no live fds
        # to lose across a server restart.
        with open(workdir / _OUT, "wb") as out_fh, open(workdir / _ERR, "wb") as err_fh:
            proc = subprocess.Popen(full, cwd=cwd, env=env, stdout=out_fh,
                                    stderr=err_fh, stdin=subprocess.DEVNULL)
    except Exception:
        try:
            os.unlink(claim)  # nothing spawned — leave the workdir reusable
        except OSError:
            pass
        raise
    meta = {"pid": proc.pid, "started": time.time(), "argv": [exe, *rest],
            "timeout_minutes": int(timeout_minutes)}
    tmp = workdir / (_PROC + ".tmp")
    tmp.write_text(json.dumps(meta))
    os.replace(tmp, workdir / _PROC)  # atomic: probe never sees a torn record
    _HANDLES[str(workdir)] = proc
    return meta


def _read_exit(workdir: Path) -> dict | None:
    try:
        marker = json.loads((workdir / _EXIT).read_text())
    except (OSError, ValueError):
        return None  # absent — or torn (hardlink-less fs fallback): retry next probe
    return marker if isinstance(marker, dict) else None


def _write_exit_excl(workdir: Path, payload: dict) -> dict | None:
    """Claim the terminal verdict iff none exists (same first-writer-wins
    protocol as the supervisor); return whatever verdict now stands."""
    final = workdir / _EXIT
    tmp = workdir / f"{_EXIT}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        tmp.write_text(json.dumps(payload))
    except OSError:
        return _read_exit(workdir)
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
                fh.write(json.dumps(payload))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return _read_exit(workdir)


def _verdict(marker: dict, started: float, out: str, err: str) -> dict:
    rc = marker.get("exit_code")
    base = {"exit_code": rc, "output": out, "stderr": err, "started": started}
    if marker.get("timed_out"):
        return {"status": "timeout", **base, "reasons": ["subcall exceeded its runner timeout"]}
    if marker.get("cancelled"):
        return {"status": "error", **base, "reasons": ["subcall cancelled by terminate()"]}
    if marker.get("error"):
        return {"status": "error", **base, "reasons": [str(marker["error"])]}
    if rc == 0:
        return {"status": "done", **base, "reasons": []}
    return {"status": "error", **base, "reasons": [f"runner exited {rc}: {err[-500:]}"]}


def probe(workdir: Path) -> dict:
    workdir = Path(workdir)
    _sweep_handles()
    proc_file = workdir / _PROC
    if not proc_file.exists():
        return {"status": "error", "reasons": ["no subcall process record"], "exit_code": None,
                "output": "", "stderr": "", "started": None}
    meta = json.loads(proc_file.read_text())
    started = meta["started"]
    # Verdict BEFORE output: a marker can only exist after the child
    # exited, so output read after the marker is complete; the reverse
    # order could pair a "done" verdict with truncated output.
    marker = _read_exit(workdir)
    if marker is None:
        deadline = started + meta["timeout_minutes"] * 60
        if time.time() <= deadline:
            out = (workdir / _OUT).read_text(errors="replace") if (workdir / _OUT).exists() else ""
            err = (workdir / _ERR).read_text(errors="replace") if (workdir / _ERR).exists() else ""
            return {"status": "running", "exit_code": None, "output": out, "stderr": err,
                    "started": started, "reasons": []}
        # Past the deadline with no verdict: the supervisor should have
        # recorded one and may be dead (machine restart). Claim the
        # terminal timeout verdict ourselves — first writer wins, so a
        # supervisor verdict that beat us is honored instead — then ask
        # any surviving supervisor to kill the child via the cancel file.
        marker = _write_exit_excl(workdir, {"exit_code": None, "timed_out": True, "cancelled": False})
        try:
            (workdir / _CANCEL).touch()
        except OSError:
            pass
        if marker is None:  # torn concurrent write on a hardlink-less fs; next probe settles
            return {"status": "timeout", "exit_code": None, "output": "", "stderr": "",
                    "started": started, "reasons": ["subcall exceeded its runner timeout"]}
    out = (workdir / _OUT).read_text(errors="replace") if (workdir / _OUT).exists() else ""
    err = (workdir / _ERR).read_text(errors="replace") if (workdir / _ERR).exists() else ""
    return _verdict(marker, started, out, err)


def terminate(workdir: Path) -> None:
    """Cancel a subcall: claim a terminal 'cancelled' verdict (a verdict
    already recorded stands) and signal the supervisor — which owns the
    handle, wherever it lives — to kill the child. Portable by
    construction: this side never needs a handle or a pid."""
    workdir = Path(workdir)
    if not workdir.exists():
        return
    _write_exit_excl(workdir, {"exit_code": None, "timed_out": False, "cancelled": True})
    try:
        (workdir / _CANCEL).touch()
    except OSError:
        pass
    _sweep_handles()


# --- graph hooks -------------------------------------------------------------
#
# Resume payloads land ONLY inside the `evidence` channel (yamlgraph's
# `interrupt_fn` returns `{resume_key: response}`; undeclared `_subcall_*`
# top-level channels are dropped by LangGraph), so both hooks read the
# engine-provided ctx from `state["evidence"]` — never from top-level state.


def _envelope(src: dict, **extra: Any) -> dict:
    env = {"node": src.get("_subcall_node", ""), "runner": src.get("_subcall_runner", ""),
           "output": "", "exit_code": None, "session_id": None,
           "child_run": src.get("_subcall_child_run"), "child_status": None,
           "artifact_hashes": {}, "reasons": []}
    env.update(extra)
    return env


def _session_id(output: str) -> str | None:
    try:
        payload = json.loads(output.strip().splitlines()[-1])
        sid = payload.get("session_id")
        return str(sid) if sid else None
    except (ValueError, IndexError, AttributeError):
        return None


def _workdir(src: dict) -> Path | None:
    wd = src.get("_subcall_workdir")
    return Path(wd) if wd else None


def _ctx(src: dict) -> tuple[dict, list[str]]:
    """Validate the engine-provided ctx (read from the `evidence` channel —
    NO top-level fallback: a second read path is a second thing to audit).
    Every field is REQUIRED — no silent fallbacks: a defaulted timeout hides
    a budget, a defaulted cwd leaks the engine's own cwd, and env=None would
    hand the child the engine's full environment past the child_env
    allowlist."""
    problems: list[str] = []
    wd = src.get("_subcall_workdir")
    argv = src.get("_subcall_argv")
    cwd = src.get("_subcall_cwd")
    env = src.get("_subcall_env")
    tm = src.get("_subcall_timeout_minutes")
    if not wd:
        problems.append("subcall ctx missing: _subcall_workdir")
    if not argv or not isinstance(argv, (list, tuple)):
        problems.append("subcall ctx missing: _subcall_argv")
    if not cwd:
        problems.append("subcall ctx missing: _subcall_cwd")
    if not isinstance(env, dict):
        problems.append("subcall ctx invalid: _subcall_env must be the child_env() allowlist dict "
                        "(None would inherit the engine's full environment)")
    if not isinstance(tm, int) or isinstance(tm, bool) or tm < 0:
        problems.append("subcall ctx missing: _subcall_timeout_minutes (non-negative int, no silent default)")
    ctx = {"workdir": Path(wd) if wd else None,
           "argv": list(argv) if isinstance(argv, (list, tuple)) else None,
           "cwd": cwd, "env": env, "timeout_minutes": tm}
    return ctx, problems


def spawn(state: dict) -> dict:
    src = state.get("evidence") or {}
    ctx, problems = _ctx(src)
    if problems:
        return {"_subcall_status": "error", "_subcall_envelope": _envelope(src, reasons=problems)}
    if (ctx["workdir"] / _PROC).exists():
        # Already spawned (e.g. server restarted between spawn and poll):
        # reattach to the recorded session instead of forging a second one.
        return {"_subcall_status": "running", "_subcall_envelope": _envelope(src)}
    try:
        start_process(ctx["argv"], cwd=ctx["cwd"], env=ctx["env"],
                      workdir=ctx["workdir"], timeout_minutes=ctx["timeout_minutes"])
    except (OSError, RunnerError) as exc:
        return {"_subcall_status": "error",
                "_subcall_envelope": _envelope(src, reasons=[f"spawn failed: {exc}"])}
    return {"_subcall_status": "running", "_subcall_envelope": _envelope(src)}


def _collect_artifacts(state_dir: Path, child_run: str, artifacts: dict) -> tuple[dict, list[str]]:
    """Resolve the marker's `artifacts:` map against the child run's LAST
    baseline snapshot (`runs/<child>.baseline.<n>.json`, `n` from
    `runs/<child>.baseline_index` — written by the child's OWN engine at
    its last PASS, inside the denied state dir). A mapped path absent from
    the manifest is a problem naming the artifact (fail closed)."""
    runs_dir = Path(state_dir) / "runs"
    try:
        n = int((runs_dir / f"{child_run}.baseline_index").read_text().strip())
        manifest = json.loads((runs_dir / f"{child_run}.baseline.{n}.json").read_text())
    except (OSError, ValueError) as exc:
        return {}, [f"child run {child_run}: cannot read its final baseline snapshot ({exc})"]
    if not isinstance(manifest, dict):
        return {}, [f"child run {child_run}: final baseline snapshot is not a manifest"]
    hashes: dict = {}
    problems: list[str] = []
    for name, path in (artifacts or {}).items():
        digest = manifest.get(path)
        if digest is None:
            problems.append(f"artifact '{name}' ({path}) is absent from child run "
                            f"{child_run}'s final baseline — fail closed")
        else:
            hashes[name] = digest
    return hashes, problems


def _poll_fractal(src: dict, workdir: Path) -> dict:
    """C7.2 — completion is the CHILD RUN's terminal status, never the OS
    process (a `claude -p` child can exit while its run is still awaiting,
    and vice versa). The status comes from a plain lock-free
    `RunIndex(state_dir).get(child_run)` — safe because every writer
    publishes via atomic os.replace; this reads the file the lock protects,
    never the child's checkpoint db. Child-terminal decides BEFORE any
    probe, so repeated polls are stable: terminate()'s cancelled verdict
    can never flip a done envelope."""
    child_run = str(src["_subcall_child_run"])
    state_dir = Path(src.get("_subcall_state_dir") or "")
    try:
        rec = RunIndex(state_dir).get(child_run)
    except (KeyError, OSError, ValueError):
        return {"_subcall_status": "error",
                "_subcall_envelope": _envelope(
                    src, reasons=[f"child run {child_run} not found in the run index"])}
    if rec.status == "done":
        hashes, problems = _collect_artifacts(state_dir, child_run,
                                              src.get("_subcall_artifacts") or {})
        try:
            terminate(workdir)   # best-effort: a session whose run is terminal has no purpose
        except Exception:  # noqa: BLE001
            pass
        if problems:
            return {"_subcall_status": "error",
                    "_subcall_envelope": _envelope(src, child_status="done", reasons=problems)}
        return {"_subcall_status": "done",
                "_subcall_envelope": _envelope(src, child_status="done", artifact_hashes=hashes)}
    if rec.status in ("escalated", "aborted"):
        try:
            terminate(workdir)
        except Exception:  # noqa: BLE001
            pass
        return {"_subcall_status": "error",
                "_subcall_envelope": _envelope(
                    src, child_status=rec.status,
                    reasons=[f"child run {child_run} is {rec.status}"])}
    # child still awaiting: only now does the OS session matter
    res = probe(workdir)
    if res["status"] == "running":
        return {"_subcall_status": "running",
                "_subcall_envelope": _envelope(src, child_status=rec.status)}
    return {"_subcall_status": "error",
            "_subcall_envelope": _envelope(
                src, child_status=rec.status, exit_code=res.get("exit_code"),
                reasons=[f"runner session ended ({res['status']}) while child run "
                         f"{child_run} is still awaiting"])}


def poll(state: dict) -> dict:
    # Keeps the FULL top-level `state` too: `_subcall_envelope` is a declared
    # top-level channel threading across ticks — the PRIOR envelope (e.g. the
    # one spawn() wrote when start_process raised) is only visible there,
    # never in `evidence`. When probe() finds no proc.json (a spawn that
    # failed before writing it), its generic reason must not overwrite the
    # real "spawn failed: ..." cause on the first poll tick.
    src = state.get("evidence") or {}
    wd = _workdir(src)
    if wd is None:
        return {"_subcall_status": "error", "_subcall_envelope": _envelope(src, reasons=["subcall ctx missing"])}
    if src.get("_subcall_child_run"):
        return _poll_fractal(src, wd)
    res = probe(wd)
    status = {"running": "running", "done": "done", "error": "error", "timeout": "error"}[res["status"]]
    reasons = list(res.get("reasons") or [])
    if reasons == ["no subcall process record"]:
        prior = (state.get("_subcall_envelope") or {}).get("reasons") or []
        if prior and list(prior) != reasons:
            reasons = list(prior)      # preserve the spawn-time reason over the generic probe miss
    env = _envelope(src, output=res.get("output", ""), exit_code=res.get("exit_code"),
                    session_id=_session_id(res.get("output", "")), reasons=reasons)
    return {"_subcall_status": status, "_subcall_envelope": env}
