from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import RLock

from lockstep.runtime.effects.authority import (
    EffectAuthorityDenied,
    EffectGrant,
)
from lockstep.runtime.providers.base import (
    EffectRequest,
    PreparedLaunch,
    RunnerObservation,
    TerminalSafetyObservation,
)


def _legacy_command_service(
    state_dir,
    recipes_dir,
    *,
    runners: Mapping[str, object],
    effect_authority: object,
):
    """Test-only harness for pre-owner-policy execution behavior."""

    from lockstep.runtime.service import LockstepCommandService

    service = LockstepCommandService(state_dir, recipes_dir)
    service._require_owner_runtime_policy = lambda _requirements: None  # noqa: SLF001
    service._reconstruct_runtime_execution_context = (  # noqa: SLF001
        lambda **_kwargs: None
    )

    def open_test_coordinator() -> None:
        authority, coordinator = service._effect_coordinator_for(  # noqa: SLF001
            dict(runners), effect_authority
        )
        service.authority, service.coordinator = authority, coordinator

    service._open_effect_coordinator = open_test_coordinator  # noqa: SLF001
    service._activate_writable_core()  # noqa: SLF001 - explicit legacy harness
    return service


class FakeRunner:
    """Deterministic durable-attempt fake; method calls and actual spawns differ."""

    reconciliation_boundary = "local_durable_handle"

    def __init__(
        self,
        *,
        binding_digest: str = "b" * 64,
        required_authorities: tuple[str, ...] = ("os_user_execution",),
    ) -> None:
        self.binding_digest = binding_digest
        self.required_authorities = required_authorities
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
            (
                item
                for item in reversed(self.ensure_started_calls)
                if item.effect_id == effect_id
            ),
            None,
        )
        if launch is None:
            request = next(
                item
                for item in reversed(self.prepare_calls)
                if item.effect_id == effect_id
            )
            return RunnerObservation(
                effect_id=effect_id,
                request_digest=request.request_digest,
                runner_binding_digest=request.runner_binding_digest,
                state="absent",
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


class FakeEffectAuthority:
    """Explicit deterministic authority; never installed as a production default."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._grants: dict[str, EffectGrant] = {}
        self._revoked: set[str] = set()
        self.resolve_calls: list[str] = []
        self.resolve_intents: list[EffectRequest] = []
        self.commit_calls: list[str] = []

    def authorize(self, intent: EffectRequest) -> EffectGrant:
        with self._lock:
            grant = EffectGrant.build(
                intent,
                actor_binding_digest="d" * 64,
                required_authorities=("os_user_execution",),
                workspace_ref=f"workspace:{intent.effect_id}",
                parent_capability_generation=1,
                grant_generation=1,
                policy_epoch=1,
                config_epoch=1,
                approval_generation=None,
                expires_at=self._clock() + timedelta(hours=1),
            )
            self._grants[intent.intent_digest] = grant
            self._revoked.discard(intent.intent_digest)
            return grant

    def resolve(self, intent: EffectRequest) -> EffectGrant:
        with self._lock:
            self.resolve_calls.append(intent.intent_digest)
            self.resolve_intents.append(intent)
            if intent.intent_digest in self._revoked:
                raise EffectAuthorityDenied("effect grant is revoked")
            grant = self._grants.get(intent.intent_digest)
            if grant is None:
                raise EffectAuthorityDenied("no exact effect grant is installed")
            if grant.expires_at <= self._clock():
                raise EffectAuthorityDenied("effect grant is expired")
            return grant

    def revoke(self, intent_digest: str) -> None:
        with self._lock:
            self._grants.pop(intent_digest, None)
            self._revoked.add(intent_digest)

    @contextmanager
    def commitment(self, grant, request, launch):
        with self._lock:
            current = self._grants.get(request.intent_digest)
            if (
                request.intent_digest in self._revoked
                or current is None
                or current.digest != grant.digest
            ):
                raise EffectAuthorityDenied("effect grant is revoked or superseded")
            if (
                request.grant_digest != grant.digest
                or getattr(launch, "workspace_ref", None) != grant.workspace_ref
                or grant.expires_at <= self._clock()
            ):
                raise EffectAuthorityDenied("effect grant commitment is stale")
            self.commit_calls.append(grant.digest)
            yield
