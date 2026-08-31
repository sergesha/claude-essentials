"""Stop-hook run selection and response projection."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.status import ScenarioStatus


def _matching_stop_runs(
    active: Iterable[tuple[RunBinding, ScenarioStatus]],
    *,
    state_dir: Path,
    cwd: str,
    session_id: object,
    stale_minutes: float,
    project_matches: Callable[[str, str], bool],
    owned_by_another: Callable[[Path, str, str, float], bool],
) -> tuple[tuple[RunBinding, ScenarioStatus], ...]:
    matches = tuple(
        (binding, status)
        for binding, status in active
        if project_matches(binding.project_identity, cwd)
    )
    if not isinstance(session_id, str) or not session_id:
        return matches
    return tuple(
        (binding, status)
        for binding, status in matches
        if not owned_by_another(
            state_dir,
            binding.public_run_id,
            session_id,
            stale_minutes,
        )
    )


def _render_stop_decision(
    matches: Iterable[tuple[RunBinding, ScenarioStatus]],
) -> tuple[int, str]:
    lines = []
    for binding, status in matches:
        if status.status == "awaiting":
            lines.append(
                f"lockstep: active run(s) awaiting a report — "
                f"{binding.public_run_id} (step: {status.step}). Report the step "
                "via scenario_done with evidence, scenario_escalate if blocked, "
                "or scenario_abort to cancel the run."
            )
        else:
            lines.append(
                f"lockstep: run {binding.public_run_id} is {status.status} under "
                "engine ownership — check scenario_status before stopping."
            )
    if not lines:
        return 0, ""
    return 0, json.dumps({"decision": "block", "reason": " ".join(lines)})
