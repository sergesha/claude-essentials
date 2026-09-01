"""Runtime-owned environment and state path parsing."""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    return Path(os.environ.get("LOCKSTEP_STATE_DIR") or str(Path.home() / ".lockstep"))


def recipes_dir() -> Path:
    return Path(os.environ.get("LOCKSTEP_RECIPES") or str(Path.cwd() / ".lockstep" / "recipes"))


def policy_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "policy.d"


def session_stale_minutes() -> float:
    try:
        return float(os.environ.get("LOCKSTEP_SESSION_STALE_MINUTES", "30"))
    except ValueError:
        return 30.0


def project_matches(run_project: str, cwd: str) -> bool:
    try:
        rp = Path(run_project).resolve()
        cp = Path(cwd).resolve()
    except Exception:  # noqa: BLE001
        return False
    return cp == rp or rp in cp.parents
