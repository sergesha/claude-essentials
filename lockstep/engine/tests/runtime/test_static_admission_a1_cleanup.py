"""Failure-path contracts for the focused A1 concurrent-test cleanup owner."""

from __future__ import annotations

import threading

import pytest

from tests.runtime._static_admission_a1_harness import A1ConcurrentCleanup


class _RecordedRelease:
    def __init__(self, actions: list[str], name: str, error: BaseException | None = None):
        self._actions = actions
        self._name = name
        self._error = error

    def set(self) -> None:
        self._actions.append(f"release:{self._name}")
        if self._error is not None:
            raise self._error


class _RecordedCall:
    def __init__(
        self,
        actions: list[str],
        name: str,
        result: bool = True,
        error: BaseException | None = None,
    ):
        self.name = name
        self._actions = actions
        self._result = result
        self._error = error

    def stop(self) -> bool:
        self._actions.append(f"stop:{self.name}")
        if self._error is not None:
            raise self._error
        return self._result


def test_stop_false_raises_only_after_every_cleanup_action() -> None:
    actions: list[str] = []
    cleanup = A1ConcurrentCleanup(
        lambda: actions.append("close"),
        (_RecordedRelease(actions, "barrier"),),  # type: ignore[arg-type]
        calls=[
            _RecordedCall(actions, "stuck", result=False),  # type: ignore[list-item]
            _RecordedCall(actions, "later"),  # type: ignore[list-item]
        ],
    )

    with pytest.raises(BaseExceptionGroup, match="A1 concurrent cleanup failed") as caught:
        with cleanup:
            pass

    assert actions == ["release:barrier", "stop:stuck", "stop:later", "close"]
    assert any("stuck" in str(error) for error in caught.value.exceptions)
    assert cleanup.threads_stopped is False


def test_start_failure_after_registration_is_cleaned_without_invalid_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[str] = []

    def fail_start(_thread: threading.Thread) -> None:
        actions.append("start")
        raise RuntimeError("injected start failure")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    cleanup = A1ConcurrentCleanup(lambda: actions.append("close"), ())

    with pytest.raises(RuntimeError, match="injected start failure"):
        with cleanup:
            cleanup.launch("fails-to-start", lambda: None)

    assert len(cleanup.calls) == 1
    assert cleanup.threads_stopped is True
    assert actions == ["start", "close"]


def test_cleanup_failures_do_not_skip_later_actions_and_note_original() -> None:
    actions: list[str] = []

    def fail_close() -> None:
        actions.append("close")
        raise RuntimeError("close failed")

    cleanup = A1ConcurrentCleanup(
        fail_close,
        (
            _RecordedRelease(actions, "broken", RuntimeError("release failed")),
            _RecordedRelease(actions, "later"),
        ),  # type: ignore[arg-type]
        calls=[
            _RecordedCall(actions, "broken", error=RuntimeError("stop failed")),  # type: ignore[list-item]
            _RecordedCall(actions, "later"),  # type: ignore[list-item]
        ],
    )
    original = AssertionError("original test failure")

    with pytest.raises(AssertionError, match="original test failure") as caught:
        with cleanup:
            raise original

    assert caught.value is original
    assert actions == [
        "release:broken",
        "release:later",
        "stop:broken",
        "stop:later",
        "close",
    ]
    notes = getattr(original, "__notes__", ())
    assert len(notes) == 1
    expected_messages = ("release failed", "stop failed", "close failed")
    assert all(message in notes[0] for message in expected_messages)
