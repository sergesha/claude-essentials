"""Neutral rendezvous and syscall observations for cooperating writer tests."""

from __future__ import annotations

import fcntl
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

from tests._authoring_crash_gate import (
    _MUTATION_SYSCALLS,
    _open_can_mutate,
    _write_open_is_allowed,
)


TIMEOUT = 10.0


class SimulatedProcessDeath(BaseException):
    """Escape publisher rollback after one durable destination mutation."""


@dataclass(frozen=True, slots=True)
class LockEvent:
    writer: str
    action: str
    identity: tuple[int, int]
    mode: int
    uid: int
    ordinal: int


@dataclass(slots=True)
class ThreadResults:
    values: dict[str, object] = field(default_factory=dict)
    threads: list[threading.Thread] = field(default_factory=list)

    def start(self, writer: str, operation: Callable[[], object]) -> threading.Thread:
        def run() -> None:
            try:
                self.values[writer] = operation()
            except BaseException as exc:
                self.values[writer] = exc

        thread = threading.Thread(target=run, name=writer)
        self.threads.append(thread)
        thread.start()
        return thread

    def join_all(self, trace: list[LockEvent]) -> None:
        deadline = time.monotonic() + TIMEOUT
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        alive = [thread.name for thread in self.threads if thread.is_alive()]
        assert not alive, f"writers did not finish: {alive}; trace={trace}"


def wait(event: threading.Event, trace: list[LockEvent]) -> None:
    assert event.wait(TIMEOUT), f"writer rendezvous timed out; trace={trace}"


@dataclass(slots=True)
class PlanningGate:
    writers: tuple[str, ...]
    planned: dict[str, threading.Event] = field(init=False)
    releases: dict[str, threading.Event] = field(init=False)
    values: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.planned = {writer: threading.Event() for writer in self.writers}
        self.releases = {writer: threading.Event() for writer in self.writers}

    def wrap(self, original: Callable[..., object]) -> Callable[..., object]:
        def observe(*args, **kwargs):
            value = original(*args, **kwargs)
            writer = threading.current_thread().name
            if writer in self.planned:
                self.values[writer] = value
                self.planned[writer].set()
                assert self.releases[writer].wait(TIMEOUT), "planner release timed out"
            return value

        return observe

    def wait_for(self, writer: str, trace: list[LockEvent]) -> None:
        wait(self.planned[writer], trace)

    def release_writer(self, writer: str) -> None:
        self.releases[writer].set()

    def release_all(self) -> None:
        for event in self.releases.values():
            event.set()


@dataclass(slots=True)
class FlockTrace:
    pause_writer: str | None = None
    pause_ordinal: int = 0
    events: list[LockEvent] = field(default_factory=list)
    timeline: list[tuple[str, str, int]] = field(default_factory=list)
    before_kernel: threading.Event = field(default_factory=threading.Event)
    enter_kernel: threading.Event = field(default_factory=threading.Event)
    paused_acquired: threading.Event = field(default_factory=threading.Event)
    _attempts: dict[str, int] = field(default_factory=dict)
    _pending: dict[str, int] = field(default_factory=dict)
    _held: dict[str, tuple[int, int]] = field(default_factory=dict)
    attempted: dict[tuple[str, int], threading.Event] = field(default_factory=dict)
    acquired: dict[tuple[str, int], threading.Event] = field(default_factory=dict)
    on_acquired: Callable[[str, int], None] | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = fcntl.flock

        def flock(descriptor: int, operation: int) -> None:
            writer = threading.current_thread().name
            info = os.fstat(descriptor)
            identity = info.st_dev, info.st_ino
            if operation & fcntl.LOCK_EX:
                first_attempt = writer not in self._pending
                if first_attempt:
                    ordinal = self._attempts.get(writer, 0) + 1
                    self._attempts[writer] = ordinal
                    self._pending[writer] = ordinal
                    if writer == self.pause_writer and ordinal == self.pause_ordinal:
                        self.before_kernel.set()
                        assert self.enter_kernel.wait(TIMEOUT), "flock release timed out"
                    self.events.append(
                        LockEvent(
                            writer,
                            "attempt",
                            identity,
                            info.st_mode & 0o777,
                            info.st_uid,
                            ordinal,
                        )
                    )
                    self.timeline.append((writer, "attempt", ordinal))
                    self.attempted.setdefault(
                        (writer, ordinal), threading.Event()
                    ).set()
                else:
                    ordinal = self._pending[writer]
                original(descriptor, operation)
                self._pending.pop(writer, None)
                self._held[writer] = identity
                self.events.append(
                    LockEvent(
                        writer,
                        "acquired",
                        identity,
                        info.st_mode & 0o777,
                        info.st_uid,
                        ordinal,
                    )
                )
                self.timeline.append((writer, "acquired", ordinal))
                if self.on_acquired is not None:
                    self.on_acquired(writer, ordinal)
                self.acquired.setdefault((writer, ordinal), threading.Event()).set()
                if writer == self.pause_writer and ordinal == self.pause_ordinal:
                    self.paused_acquired.set()
                return
            original(descriptor, operation)
            if operation & fcntl.LOCK_UN:
                ordinal = self._attempts.get(writer, 0)
                self.events.append(
                    LockEvent(
                        writer,
                        "released",
                        identity,
                        info.st_mode & 0o777,
                        info.st_uid,
                        ordinal,
                    )
                )
                self.timeline.append((writer, "released", ordinal))
                self._held.pop(writer, None)

        monkeypatch.setattr(fcntl, "flock", flock)

    def holds(self, writer: str) -> bool:
        return writer in self._held

    def event(self, writer: str, action: str, ordinal: int) -> LockEvent:
        return next(
            item
            for item in self.events
            if (item.writer, item.action, item.ordinal)
            == (writer, action, ordinal)
        )

    def wait_attempt(self, writer: str, ordinal: int) -> None:
        event = self.attempted.setdefault((writer, ordinal), threading.Event())
        assert event.wait(TIMEOUT), f"missing flock attempt: {writer}/{ordinal}"

    def before(self, first: LockEvent, second: LockEvent) -> bool:
        return self.events.index(first) < self.events.index(second)


def _destination_index(
    destinations: tuple[Path, ...], value: object, directory_fd: int | None
) -> int | None:
    if directory_fd is None:
        return None
    parent = os.fstat(directory_fd)
    leaf = os.fsdecode(value)
    for index, path in enumerate(destinations):
        candidate = path.parent.stat()
        if path.name == leaf and (candidate.st_dev, candidate.st_ino) == (
            parent.st_dev,
            parent.st_ino,
        ):
            return index
    return None


@dataclass(slots=True)
class DurableMutationGate:
    holder: str
    destinations: tuple[Path, ...]
    pause: bool = True
    crash: bool = False
    on_crash: Callable[[], None] | None = None
    on_mutation: Callable[[str, str, int], None] | None = None
    inside: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    events: list[tuple[str, str, int]] = field(default_factory=list)
    _pending_parent: tuple[int, int] | None = None
    _injected: bool = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original_replace = os.replace
        original_link = os.link
        original_fsync = os.fsync

        def mutate(name, original, source, destination, *args, **kwargs):
            writer = threading.current_thread().name
            ordinal = _destination_index(
                self.destinations, destination, kwargs.get("dst_dir_fd")
            )
            if ordinal is None:
                return original(source, destination, *args, **kwargs)
            self.events.append((writer, name, ordinal))
            if self.on_mutation is not None:
                self.on_mutation(writer, name, ordinal)
            if writer == self.holder and not self.inside.is_set():
                self.inside.set()
                if self.pause:
                    assert self.release.wait(TIMEOUT), "mutation release timed out"
            result = original(source, destination, *args, **kwargs)
            if writer == self.holder and self.crash and not self._injected:
                parent = self.destinations[ordinal].parent.stat()
                self._pending_parent = parent.st_dev, parent.st_ino
            return result

        def replace(source, destination, *args, **kwargs):
            return mutate("replace", original_replace, source, destination, *args, **kwargs)

        def link(source, destination, *args, **kwargs):
            return mutate("link", original_link, source, destination, *args, **kwargs)

        def fsync(descriptor: int) -> None:
            original_fsync(descriptor)
            observed = os.fstat(descriptor)
            if (
                threading.current_thread().name == self.holder
                and self._pending_parent == (observed.st_dev, observed.st_ino)
                and not self._injected
            ):
                self._injected = True
                if self.on_crash is not None:
                    self.on_crash()
                raise SimulatedProcessDeath("after durable destination parent fsync")

        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(os, "link", link)
        if self.crash:
            monkeypatch.setattr(os, "fsync", fsync)

    @property
    def injected(self) -> bool:
        return self._injected


@dataclass(slots=True)
class ThreadMutationTrace:
    """Attribute journal, stage and destination mutation syscalls by thread."""

    allowed_write_open_identities: frozenset[tuple[int, int]] = frozenset()
    events: list[tuple[str, str]] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (*_MUTATION_SYSCALLS, "fsync"):
            if not hasattr(os, name):
                continue
            original = getattr(os, name)

            def record(*args, _name=name, _original=original, **kwargs):
                self.events.append((threading.current_thread().name, _name))
                return _original(*args, **kwargs)

            monkeypatch.setattr(os, name, record)
        original_open = os.open

        def record_open(path, flags, mode=0o777, *, dir_fd=None):
            if _open_can_mutate(flags) and not _write_open_is_allowed(
                path, flags, dir_fd, self.allowed_write_open_identities
            ):
                self.events.append((threading.current_thread().name, "open"))
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", record_open)
