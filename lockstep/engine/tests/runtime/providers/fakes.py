from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import replace

from lockstep.runtime.providers.base import (
    EffectRequest,
    PreparedLaunch,
    RunnerObservation,
    TerminalSafetyObservation,
)


class FakeRunner:
    """Deterministic durable-attempt fake; method calls and actual spawns differ."""

    def __init__(self, *, binding_digest: str = "b" * 64) -> None:
        self.binding_digest = binding_digest
        self.prepare_calls: list[EffectRequest] = []
        self.ensure_started_calls: list[PreparedLaunch] = []
        self.inspect_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.quiesce_calls: list[str] = []
        self.spawn_count = 0
        self._started: set[str] = set()
        self.start_observations: deque[RunnerObservation] = deque()
        self.inspect_observations: deque[RunnerObservation] = deque()
        self.cancel_observations: deque[RunnerObservation] = deque()
        self.safety_observations: deque[TerminalSafetyObservation] = deque()
        self.workspace_refs: deque[str | None] = deque()
        self.prepare_callbacks: deque[Callable[[], object]] = deque()

    def prepare(self, request: EffectRequest) -> PreparedLaunch:
        self.prepare_calls.append(request)
        if self.prepare_callbacks:
            self.prepare_callbacks.popleft()()
        return PreparedLaunch(
            effect_id=request.effect_id,
            request_digest=request.request_digest,
            runner_binding_digest=request.runner_binding_digest,
            launch_ref=f"launch:{request.effect_id}",
            workspace_ref=(
                self.workspace_refs.popleft()
                if self.workspace_refs
                else f"workspace:{request.effect_id}"
            ),
        )

    def ensure_started(self, launch: PreparedLaunch) -> RunnerObservation:
        self.ensure_started_calls.append(launch)
        if launch.effect_id not in self._started:
            self._started.add(launch.effect_id)
            self.spawn_count += 1
        if self.start_observations:
            return self.start_observations.popleft()
        return RunnerObservation.running_for(launch)

    def inspect(self, effect_id: str) -> RunnerObservation:
        self.inspect_calls.append(effect_id)
        if self.inspect_observations:
            return self.inspect_observations.popleft()
        launch = next(
            item
            for item in reversed(self.ensure_started_calls)
            if item.effect_id == effect_id
        )
        return RunnerObservation.running_for(launch)

    def cancel(self, effect_id: str) -> RunnerObservation:
        self.cancel_calls.append(effect_id)
        if self.cancel_observations:
            return self.cancel_observations.popleft()
        launch = next(
            item
            for item in reversed(self.ensure_started_calls)
            if item.effect_id == effect_id
        )
        return RunnerObservation.running_for(launch)

    def quiesce(self, effect_id: str) -> TerminalSafetyObservation:
        self.quiesce_calls.append(effect_id)
        if self.safety_observations:
            return self.safety_observations.popleft()
        launch = next(
            item
            for item in reversed(self.ensure_started_calls)
            if item.effect_id == effect_id
        )
        return TerminalSafetyObservation.pending_for(launch)

    def terminal(self, launch: PreparedLaunch, result) -> RunnerObservation:
        return RunnerObservation(
            effect_id=launch.effect_id,
            request_digest=launch.request_digest,
            runner_binding_digest=launch.runner_binding_digest,
            state="terminal",
            result=result,
        )

    def mismatch(self, observation: RunnerObservation) -> RunnerObservation:
        return replace(observation, request_digest="f" * 64)
