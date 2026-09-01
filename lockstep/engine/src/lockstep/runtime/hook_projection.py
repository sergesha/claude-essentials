"""Verified read-only native status projection for hooks and doctor."""

from __future__ import annotations

from pathlib import Path

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.read_resources import RuntimeReadResources
from lockstep.runtime.status import ScenarioStatus, project_status


class HookProjectionError(RuntimeError):
    """Trusted native state could not be projected safely."""


def read_only_statuses(
    state_dir: Path,
) -> tuple[tuple[RunBinding, ScenarioStatus], ...]:
    """Never creates storage, checkpoints, materializations, or transitions."""

    try:
        resources = RuntimeReadResources(state_dir)
        bindings = resources.bindings()
        if not bindings:
            return ()
        projected = []
        for binding in bindings:
            effects = resources.effects_for_thread(binding.thread_id)
            with resources.native_app(binding) as app:
                snapshot = app.snapshot(thread_id=binding.thread_id, subgraphs=True)
            projected.append((binding, project_status(binding, snapshot, (), effects)))
        return tuple(projected)
    except Exception as exc:
        raise HookProjectionError(
            "trusted native state failed read-only verification"
        ) from exc
