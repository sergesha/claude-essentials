"""Passive runtime observation boundary."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from lockstep.runtime import config, sessions
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.native_models import NativeHistoryLimitExceeded
from lockstep.runtime.observation import (
    project_events,
    project_history,
    project_trace,
    status_revision,
)
from lockstep.runtime.read_resources import RuntimeReadResources
from lockstep.runtime.status import ScenarioStatus, project_status


class RuntimeProjection:
    """Read-only view of durable runtime state."""

    _MAX_PUBLIC_EVENTS = 10_000

    def __init__(self, state_dir: Path, recipes_dir: Path) -> None:
        self._state_dir = Path(state_dir).absolute()
        del recipes_dir
        self._resources = RuntimeReadResources(self._state_dir)
        self._wait_clock = time.monotonic
        self._wait_sleep = time.sleep

    def _binding_for(self, run_id: str, project: str):
        project_identity = str(Path(project).resolve())
        try:
            binding = self._resources.binding_for(run_id, project_identity)
        except Exception as exc:
            raise LockstepError(
                "trusted native state failed read-only verification"
            ) from exc
        if binding is not None:
            return binding
        raise LockstepError(f"unknown run {run_id!r}")

    def _status_for(self, binding, effects=None) -> ScenarioStatus:
        try:
            observed_effects = (
                effects
                if effects is not None
                else self._resources.effects_for_thread(binding.thread_id)
            )
            with self._resources.native_app(binding) as app:
                snapshot = app.snapshot(thread_id=binding.thread_id, subgraphs=True)
            status = project_status(binding, snapshot, (), observed_effects)
        except Exception as exc:
            raise LockstepError(
                "trusted native state failed read-only verification"
            ) from exc
        if status.status == "awaiting" and status.owner == "worker":
            try:
                session_binding = self._resources.session_binding(
                    binding.public_run_id
                )
            except Exception as exc:
                raise LockstepError(
                    "trusted native state failed read-only verification"
                ) from exc
            if not sessions.is_live(session_binding, config.session_stale_minutes()):
                status = replace(
                    status,
                    annotations=status.annotations
                    + (("binding_integrity", "missing_or_stale"),),
                )
        return status

    def status(self, run_id: str, project: str) -> dict:
        return self._status_for(self._binding_for(run_id, project)).to_dict()

    def close(self) -> None:
        """Release the projection; it owns no active or writable resource."""

    def wait(self, run_id: str, timeout_seconds: int, project: str) -> dict:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise LockstepError("scenario wait timeout must be an integer from 1 to 60")
        initial = self.status(run_id, project)
        initial_revision = status_revision(initial)
        deadline = self._wait_clock() + timeout_seconds
        current = initial
        while True:
            remaining = deadline - self._wait_clock()
            if remaining <= 0:
                return {**current, "changed": False, "revision": initial_revision}
            self._wait_sleep(min(0.1, remaining))
            current = self.status(run_id, project)
            revision = status_revision(current)
            if revision != initial_revision:
                return {**current, "changed": True, "revision": revision}

    def history(self, run_id: str, project: str) -> list[dict]:
        binding = self._binding_for(run_id, project)
        try:
            return project_history(binding, self._resources.history(binding))
        except NativeHistoryLimitExceeded as exc:
            raise LockstepError(str(exc)) from exc
        except Exception as exc:
            raise LockstepError(
                "trusted native state failed read-only verification"
            ) from exc

    def events(self, run_id: str, project: str) -> list[dict]:
        binding = self._binding_for(run_id, project)
        try:
            native = self._resources.history(binding)
            effects = self._resources.effects_for_thread(
                binding.thread_id
            ).list_for_thread(binding.thread_id)
        except NativeHistoryLimitExceeded as exc:
            raise LockstepError(str(exc)) from exc
        except Exception as exc:
            raise LockstepError(
                "trusted native state failed read-only verification"
            ) from exc
        return project_events(
            native,
            effects,
            limit=self._MAX_PUBLIC_EVENTS,
        )

    def list_runs(self, project: str) -> list[dict]:
        project_identity = str(Path(project).resolve())
        try:
            bindings = self._resources.bindings_for_project(project_identity)
            return [
                self._status_for(binding).to_dict() for binding in bindings
            ]
        except LockstepError:
            raise
        except Exception as exc:
            raise LockstepError(
                "trusted native state failed read-only verification"
            ) from exc

    def run_trace(self, run_id: str, project: str) -> str:
        return project_trace(self.history(run_id, project))
