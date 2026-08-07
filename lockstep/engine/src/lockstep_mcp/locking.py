"""Portable cross-process lock.

OS-AGNOSTIC: acquisition is an atomic ``O_CREAT | O_EXCL`` create on a
sidecar file — the one primitive POSIX and Windows both give us through
stdlib ``os``. No fcntl/flock/msvcrt anywhere in this project; this module
is the only place a lock primitive appears.

The lock is ALWAYS a sidecar (``<target>.lock``), never ``target`` itself:
callers publish via ``os.replace``, which swaps inodes, so a lock held on
the replaced file would be void.

Caller contract: there is no heartbeat/renewal — a held lock's mtime is
set once, at acquire time, and never refreshed. ``stale_after`` MUST
therefore exceed the longest legitimate hold under this lock. A hold that
outlives ``stale_after`` is indistinguishable from a crashed holder and
WILL be broken by a waiter; that is the mechanism working as designed, not
a bug to be "fixed" by shortening a hold's actual work instead of widening
``stale_after``.

Concurrency boundary: breaking a stale lock is a crash-recovery path, not
routine traffic, and is verified exclusive for the case the recovery path
exists for — one active holder plus any number of waiters, at least two of
which race to break the same stale lock (see
``test_concurrent_stale_break_is_exclusive`` /
``test_stale_break_survives_original_holder_release``). Portable stdlib
has no atomic "delete/replace this file iff it still matches what I last
observed" primitive (that needs fcntl/flock, explicitly excluded here), so
the break path is check-then-act and carries an inherent, small TOCTOU
window. Under PATHOLOGICAL, sustained concurrent stale-breaking — many
callers continuously racing to break the same already-stale lock with no
real work between attempts, well beyond what ``stale_after`` being sized
correctly (see above) should ever produce — that window is not proven
closed. Keep ``stale_after`` comfortably above real hold durations so
stale-breaking stays a rare recovery event rather than routine contention.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockTimeout(RuntimeError):
    """Raised when the lock could not be acquired within the timeout."""


def _lock_path(target: Path) -> Path:
    return target.parent / (target.name + ".lock")


def _is_stale(path: Path, stale_after: float) -> bool:
    # Staleness is decided by the lock file's MODIFICATION TIME, never by
    # parsing its content. There is a window between the O_CREAT|O_EXCL
    # create and the json.dump that fills it where the lock file exists but
    # is still empty; a competing waiter reading it in that window must NOT
    # treat "fails to parse" as "stale" — that would delete a live lock out
    # from under its holder. A missing file (raced away by another waiter
    # who just broke/released it) is not stale either — the acquire loop
    # will simply retry.
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) > stale_after


def _break_stale(lock: Path, stale_after: float) -> None:
    """Break a stale lock without trusting the path — or the pre-check — alone.

    A plain ``os.unlink(lock)`` trusts the NAME, not the specific file: if
    another waiter already broke this same stale lock and re-acquired a
    fresh one under the same name, a naive unlink here would delete that
    LIVE lock instead — two waiters would then both pass their
    ``O_CREAT|O_EXCL`` and enter the critical section together.

    ``os.rename(lock, side)`` is atomic with respect to its source on both
    POSIX and Windows: of any number of racing breakers, exactly one
    observes success (it now owns the only reference to whatever file was
    named ``lock`` at that instant); every other racer's rename fails with
    ``OSError`` because the name is already gone. Losers do nothing and let
    the acquire loop retry.

    The caller's staleness check (``_is_stale`` on ``lock``, before this is
    called) is itself a TOCTOU window: the file it inspected can be
    released and replaced by a fresh, live lock between that check and this
    rename — so the winner of the rename race is not necessarily entitled
    to break anything. Once ``side`` is exclusively ours (nobody else can
    reach it by name), we re-verify staleness on that race-free copy — its
    mtime is untouched by the rename, so this verdict is authoritative. If
    it turns out fresh, we grabbed a live lock by mistake and must put it
    back under its original name rather than discard it.
    """
    side = lock.with_name(lock.name + f".stale-{os.getpid()}-{time.time_ns()}")
    try:
        os.rename(lock, side)
    except OSError:
        return  # lost the race to break it (or it's already gone) — just retry
    if _is_stale(side, stale_after):
        try:
            os.unlink(side)
        except OSError:
            pass
        return
    # Wrongly grabbed a live lock: restore it under its original name so
    # its real owner's eventual release still finds and can release it.
    #
    # This must NOT be `os.rename(side, lock)`: POSIX rename() SILENTLY
    # REPLACES an existing destination (no error) — Windows raises instead.
    # If a third party legitimately created a fresh lock at `lock` while we
    # were investigating, a plain rename-back would clobber THEIR live lock
    # on POSIX instead of failing loudly, reintroducing the very bug this
    # function exists to close. So the restore reuses the one atomic,
    # portable "fail if it already exists" primitive this module relies on
    # everywhere else — O_CREAT|O_EXCL — copying `side`'s exact bytes and
    # mtime into a freshly (and exclusively) created `lock`.
    try:
        payload = side.read_bytes()
        st = side.stat()
    except OSError:
        return  # side vanished under us; nothing left to restore
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        # a third party legitimately owns `lock` now — discard our
        # orphaned copy instead of leaking it.
        try:
            os.unlink(side)
        except OSError:
            pass
        return
    with os.fdopen(fd, "wb") as fh:
        fh.write(payload)
    try:
        os.utime(lock, (st.st_atime, st.st_mtime))  # preserve the original staleness clock
    except OSError:
        pass
    try:
        os.unlink(side)
    except OSError:
        pass


@contextmanager
def file_lock(target: Path, timeout: float = 10.0, stale_after: float = 60.0) -> Iterator[None]:
    lock = _lock_path(target)
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    acquired = False
    # Unique per-acquisition owner token: proves at release time that the
    # file currently named `lock` is still THIS acquisition's file, not one
    # a stale-breaker created after outliving `stale_after` (see the
    # release path below).
    owner = f"{os.getpid()}:{time.time_ns()}"
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as fh:
                json.dump({"pid": os.getpid(), "ts": time.time(), "owner": owner}, fh)
            acquired = True
            break
        except FileExistsError:
            if _is_stale(lock, stale_after):
                _break_stale(lock, stale_after)
                continue
            if time.monotonic() >= deadline:
                raise LockTimeout(f"could not acquire lock for {target} within {timeout}s")
            time.sleep(0.02)
    try:
        yield
    finally:
        if acquired:
            # Release only if `lock` still names THIS acquisition's file.
            # If a legitimate hold outlived `stale_after`, a waiter may
            # already have broken it and acquired its own — unlinking on
            # the bare path in that case would delete the new holder's
            # live lock. Verify the owner token first; on any mismatch,
            # missing file, or unreadable content, do nothing: the name no
            # longer belongs to us.
            try:
                payload = json.loads(lock.read_text())
            except (OSError, ValueError, TypeError):
                payload = None
            if payload is not None and payload.get("owner") == owner:
                try:
                    os.unlink(lock)
                except OSError:
                    pass
