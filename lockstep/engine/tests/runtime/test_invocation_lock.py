from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from lockstep.runtime.invocation_lock import InvocationLockStore


def _child(code: str, *arguments: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), *(str(arg) for arg in arguments)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_second_process_cannot_enter_until_holder_releases(tmp_path: Path) -> None:
    owner_state = tmp_path / "owner"
    holder = InvocationLockStore(owner_state, timeout=1)
    contender = """
        import sys
        from pathlib import Path

        from lockstep.runtime.invocation_lock import (
            InvocationLockStore,
            InvocationLockTimeout,
        )

        store = InvocationLockStore(Path(sys.argv[1]), timeout=0.1)
        try:
            with store.hold("thread-1"):
                print("acquired")
        except InvocationLockTimeout:
            print("timed-out")
    """

    with holder.hold("thread-1"):
        blocked = _child(contender, owner_state)

    acquired = _child(contender, owner_state)

    assert blocked.returncode == 0, blocked.stderr
    assert blocked.stdout.strip() == "timed-out"
    assert acquired.returncode == 0, acquired.stderr
    assert acquired.stdout.strip() == "acquired"


def test_kernel_releases_lock_when_holder_process_crashes(tmp_path: Path) -> None:
    owner_state = tmp_path / "owner"
    ready = tmp_path / "holder-entered"
    crashing_holder = """
        import os
        import sys
        from pathlib import Path

        from lockstep.runtime.invocation_lock import InvocationLockStore

        store = InvocationLockStore(Path(sys.argv[1]), timeout=1)
        with store.hold("thread-1"):
            Path(sys.argv[2]).write_text("held")
            os._exit(23)
    """

    crashed = _child(crashing_holder, owner_state, ready)

    assert crashed.returncode == 23
    assert ready.read_text() == "held"
    with InvocationLockStore(owner_state, timeout=0.2).hold("thread-1"):
        pass
