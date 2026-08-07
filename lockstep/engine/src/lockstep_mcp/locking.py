"""Portable cross-process lock — correct by design.

OS-AGNOSTIC: stdlib ``os`` only. No fcntl/flock/msvcrt, no /proc, no
signals, no platform branching; this module is the only place a lock
primitive appears in the project.

Scheme
------
The lock is a sidecar ``<target>.lock`` (never ``target`` itself: callers
publish via ``os.replace``, which swaps inodes, so a lock on the target
would be void). Admission is a win on ``os.open(lock, O_CREAT|O_EXCL)``
— an atomic test-and-set on both POSIX and Windows.

VACATION of the lock name — a breaker removing a stale lock, or the
holder releasing — happens inside a *breaker session*: exclusive
ownership of a second sidecar ``<target>.lock.break``, won by the same
``O_CREAT|O_EXCL`` test-and-set and held across a handful of syscalls
only (no user code, no sleeps). Inside a session the verdict is formed
FRESH — the breaker re-checks staleness, the releaser re-checks
ownership — and cannot be invalidated before the unlink: any other
remover would need the session, and creators cannot touch an occupied
name. The main lock is never renamed aside or replaced; there is no
restore path for it, hence no absence window in which a third party can
slip in. The one exception is the release fallback (``_release``, after
0.25s of failing to enter a session): it unlinks the lock NAME with no
session held. See "Residual boundary" below for the contract that keeps
it safe.

Exclusivity invariant: every critical-section entry is an
``O_CREAT|O_EXCL`` win on ``<target>.lock``, and that name is vacated
only inside an exclusive breaker session whose staleness/ownership
verdict is formed inside the session — so no verdict outlives the state
it judged, and, absent a process death inside a session's ~5-syscall
window (with the release fallback additionally conditioned on a
``stale_after``-contract violation), no two holders can coexist.

Caller contract
---------------
Staleness is decided by the lock file's mtime age, never by content.
There is no heartbeat: ``stale_after`` MUST exceed the longest legitimate
hold (including scheduler pauses). A hold that outlives ``stale_after``
is indistinguishable from a crashed holder and WILL be broken — that is
the mechanism working as designed. Crash recovery is therefore
wall-clock-based by necessity; this is the irreducible cost of portable
(no-fcntl) crash recovery, not a gap in this implementation.

Residual boundary (stated exactly)
----------------------------------
Exclusivity is exact on every crash-free path under the caller contract.
Four branches depart from it, each gated by a process death, a
contract violation, or both:

- Release fallback (``_release``): after 0.25s failing to enter a
  session, release unlinks the lock name with no session held — an
  out-of-session vacation. It can delete a foreign live lock only if
  (i) the releaser's own hold already outlived ``stale_after`` (a
  timing-axiom violation — under the stated contract this alone rules
  out misfire), AND (ii) sessions were wedged for the full 0.25s, AND
  (iii) a break + re-acquire lands in the adjacent-syscall gap. This
  branch is named here because it genuinely sits outside a session, not
  because it is reachable under the contract.
- Crash inside a session (a microseconds-long window of ~5 syscalls):
  orphans the break-mutex; recovered by mtime staleness via
  rename-aside + re-verify in ``_recover_break_mutex``. That recovery's
  own check-then-act window requires a further compound interleaving to
  misfire.
- Zombie-restore (``_recover_break_mutex``): if the wrong-grab victim's
  session ends cleanly (via its own ``finally``) during the
  rename-aside's absence window, the restore recreates a mutex for a
  session that already finished. That mutex is ownerless — nobody will
  unlink it — and wedges every session for up to ``stale_after``, during
  which every release takes the fallback above. No second crash is
  needed; the wedge is not itself an exclusivity break, but it
  multiplies exposure to the fallback branch.
- Level-1 empty-window mis-unlink (``_breaker_session``'s ``finally``):
  if our own payload write failed (e.g. ENOSPC) and our mutex was
  separately vacated mid-session (downstream of a zombie-restore above),
  a third party's new session caught in its own empty window can read
  back as "ours" and get unlinked, enabling two concurrent breakers.
  Narrowed to the write-failed case only: a payload write that
  succeeded cannot leave the file empty, so an empty read after a
  successful write is treated as another instance's, never ours.

Each branch needs a process death inside microseconds, or a
contract-violating hold, or both — acceptable for the workload (a
handful of processes, millisecond holds, stale-breaking as rare crash
recovery).
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_POLL = 0.02  # acquire-loop poll interval (seconds)
_RELEASE_SESSION_WAIT = 0.25  # max wait for a session at release before fallback


class LockTimeout(RuntimeError):
    """Raised when the lock could not be acquired within the timeout."""


def _lock_path(target: Path) -> Path:
    return target.parent / (target.name + ".lock")


def _break_path(lock: Path) -> Path:
    return lock.parent / (lock.name + ".break")


def _is_stale(path: Path, stale_after: float) -> bool:
    # mtime age only, never parsed content: a just-created, still-empty
    # lock (the window between O_CREAT|O_EXCL and the payload write) is
    # FRESH and must not be stolen. A missing file is not stale either —
    # the acquire loop simply retries.
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) > stale_after


def _recover_break_mutex(brk: Path, stale_after: float) -> None:
    """Remove an ORPHANED break-mutex (its owner died mid-session).

    This is the one place check-then-act on a name survives, and it is
    only reachable after a process died inside a microseconds-long
    session. ``os.rename`` to a unique aside name is atomic on its
    source: of any racers, exactly one gets the file; the aside name
    never pre-exists, so POSIX's silent-destination-replace semantics
    never engage. Staleness is re-verified on the race-free aside copy;
    a wrong grab (the verdict went stale and we yanked a LIVE session's
    mutex) is restored via O_CREAT|O_EXCL with its original mtime.
    """
    side = brk.with_name(brk.name + f".crashed-{os.getpid()}-{time.time_ns()}")
    try:
        os.rename(brk, side)
    except OSError:
        return  # lost the race (or it's gone) — caller retries
    if _is_stale(side, stale_after):
        try:
            os.unlink(side)
        except OSError:
            pass
        return
    # Wrong grab: put the live mutex back under its name, preserving its
    # mtime (its staleness clock). Restore must fail loudly if the name
    # was re-taken meanwhile — hence O_EXCL, never rename (POSIX rename
    # silently replaces an existing destination).
    try:
        payload = side.read_bytes()
        st = side.stat()
    except OSError:
        return
    try:
        fd = os.open(brk, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        pass
    else:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        try:
            os.utime(brk, (st.st_atime, st.st_mtime))
        except OSError:
            pass
    try:
        os.unlink(side)
    except OSError:
        pass


@contextmanager
def _breaker_session(brk: Path, stale_after: float) -> Iterator[bool]:
    """Try to enter the exclusive breaker session; yield True iff entered.

    The session is the serializer for ALL removals of the main lock.
    It is held across a handful of syscalls only. On contention (fresh
    mutex held by another session) yields False; a stale mutex (owner
    died mid-session) is recovered first, then False — the caller's
    loop retries.
    """
    token = f"{os.getpid()}:{time.time_ns()}"
    try:
        fd = os.open(brk, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if _is_stale(brk, stale_after):
            _recover_break_mutex(brk, stale_after)
        yield False
        return
    except OSError:
        yield False
        return
    wrote = False
    try:
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump({"token": token, "pid": os.getpid(), "ts": time.time()}, fh)
            wrote = True
        except OSError:
            pass  # content is diagnostic; the created file IS the session
        yield True
    finally:
        # Verified release: skip the unlink if the file readably belongs to
        # ANOTHER session (possible only downstream of the orphan-recovery
        # residual). An empty/unreadable read is ours ONLY if our own
        # payload write never succeeded — a write that succeeded cannot
        # leave the file empty, so an empty read after a successful write
        # means our mutex was vacated (zombie-restore residual) and a
        # DIFFERENT instance now owns the name in its own empty window;
        # unlinking it would be race A's disease reimported. Only the
        # write-failed case still falls back to "ours".
        theirs = False
        try:
            data = json.loads(brk.read_text())
            theirs = isinstance(data, dict) and data.get("token") not in (None, token)
        except (OSError, ValueError, TypeError):
            theirs = wrote
        if not theirs:
            try:
                os.unlink(brk)
            except OSError:
                pass


def _unlink_if_owner(lock: Path, owner: str) -> None:
    try:
        payload = json.loads(lock.read_text())
    except (OSError, ValueError, TypeError):
        return  # missing/unreadable: the name is not provably ours — leave it
    if isinstance(payload, dict) and payload.get("owner") == owner:
        try:
            os.unlink(lock)
        except OSError:
            pass


def _release(lock: Path, brk: Path, owner: str, stale_after: float) -> None:
    # Release inside a session: the ownership verdict then cannot be
    # invalidated before the unlink, even by an overtaken (contract-
    # violating) holder's schedule. Bounded wait; if the break-mutex is
    # wedged (orphaned but not yet stale), fall back to the best-effort
    # verified unlink rather than hang the caller's ``finally``.
    deadline = time.monotonic() + _RELEASE_SESSION_WAIT
    while True:
        with _breaker_session(brk, stale_after) as in_session:
            if in_session:
                _unlink_if_owner(lock, owner)
                return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.005)
    _unlink_if_owner(lock, owner)


@contextmanager
def file_lock(target: Path, timeout: float = 10.0, stale_after: float = 60.0) -> Iterator[None]:
    lock = _lock_path(target)
    brk = _break_path(lock)
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    owner = f"{os.getpid()}:{time.time_ns()}"
    acquired = False
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            broke = False
            if _is_stale(lock, stale_after):
                with _breaker_session(brk, stale_after) as in_session:
                    if in_session and _is_stale(lock, stale_after):
                        # Verdict formed INSIDE the session: nobody else
                        # can remove or replace the file before this
                        # unlink, so it removes exactly the stale file.
                        try:
                            os.unlink(lock)
                        except OSError:
                            pass
                        broke = True
            if broke:
                continue  # name vacated by us — race O_EXCL immediately
            if time.monotonic() >= deadline:
                raise LockTimeout(f"could not acquire lock for {target} within {timeout}s")
            time.sleep(_POLL)
            continue
        with os.fdopen(fd, "w") as fh:
            json.dump({"pid": os.getpid(), "ts": time.time(), "owner": owner}, fh)
        acquired = True
        break
    try:
        yield
    finally:
        if acquired:
            _release(lock, brk, owner, stale_after)
