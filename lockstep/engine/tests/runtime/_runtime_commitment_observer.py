"""Observation-only probes for the real managed commitment lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from contextlib import contextmanager
from threading import Event, local

from pytest import MonkeyPatch

from lockstep.runtime.effects.authority import EffectGrant
from lockstep.runtime.effects.ledger import EffectRecord
from lockstep.runtime.effects.owner_policy import OwnerRuntimeSnapshot
from lockstep.runtime.effects.owner_snapshot_store import open_runtime_snapshot
from lockstep.runtime.providers.base import (
    EffectRequest,
    PreparedLaunch,
    RunnerObservation,
    launch_commitment_digest,
)
from lockstep.runtime.providers.codex import CodexRunnerAdapter
from lockstep.runtime.runtime_execution import OwnerRuntimeEffectAuthority
from lockstep.runtime.service import LockstepCommandService


@dataclass(frozen=True)
class BoundRequestCall:
    intent: EffectRequest
    grant: EffectGrant
    request: EffectRequest


@dataclass(frozen=True)
class PrepareCall:
    adapter: CodexRunnerAdapter
    request: EffectRequest
    launch: PreparedLaunch


@dataclass(frozen=True)
class CommitmentCall:
    launch: PreparedLaunch
    record: EffectRecord
    owner_digest: str
    owner_snapshot: OwnerRuntimeSnapshot
    correlated_prepares: tuple[PrepareCall, ...]


class RuntimeCommitmentObserver:
    """Capture and pause one real Codex commitment without replacing behavior."""

    def __init__(
        self,
        monkeypatch: MonkeyPatch,
        owner_state: Path,
    ) -> None:
        self.bound_requests: list[BoundRequestCall] = []
        self.prepares: list[PrepareCall] = []
        self.commitments: list[CommitmentCall] = []
        self.reached = Event()
        self._release = Event()
        self._owner_state = owner_state
        self._command: LockstepCommandService | None = None
        original_bind_grant = EffectRequest.bind_grant
        original_prepare = CodexRunnerAdapter.prepare
        original_ensure_started = CodexRunnerAdapter.ensure_started

        def capture_grant(
            intent: EffectRequest,
            grant: EffectGrant,
        ) -> EffectRequest:
            request = original_bind_grant(intent, grant)
            self.bound_requests.append(BoundRequestCall(intent, grant, request))
            return request

        def capture_prepare(
            adapter: CodexRunnerAdapter,
            request: EffectRequest,
        ) -> PreparedLaunch:
            launch = original_prepare(adapter, request)
            self.prepares.append(PrepareCall(adapter, request, launch))
            return launch

        def observe_commitment(
            adapter: CodexRunnerAdapter,
            launch: PreparedLaunch,
        ) -> RunnerObservation:
            command = self._command
            if command is None:
                raise AssertionError("commitment observer has no command service")
            record = command.effects.get(launch.effect_id)
            owner_digest, owner_snapshot = open_runtime_snapshot(self._owner_state)
            correlated = tuple(
                call
                for call in self.prepares
                if call.request.effect_id == launch.effect_id
                and call.request.request_digest == launch.request_digest
                and launch_commitment_digest(call.request, call.launch)
                == launch_commitment_digest(call.request, launch)
            )
            self.commitments.append(
                CommitmentCall(
                    launch,
                    record,
                    owner_digest,
                    owner_snapshot,
                    correlated,
                )
            )
            self.reached.set()
            self._release.wait()
            return original_ensure_started(adapter, launch)

        monkeypatch.setattr(EffectRequest, "bind_grant", capture_grant)
        monkeypatch.setattr(CodexRunnerAdapter, "prepare", capture_prepare)
        monkeypatch.setattr(
            CodexRunnerAdapter,
            "ensure_started",
            observe_commitment,
        )

    def attach(self, command: LockstepCommandService) -> None:
        self._command = command

    def release(self) -> None:
        self._release.set()


@dataclass(frozen=True)
class OwnerCommitmentBarrierCall:
    intent: EffectRequest
    grant: EffectGrant
    request: EffectRequest
    launch: PreparedLaunch
    record: EffectRecord
    owner_digest: str
    owner_snapshot: OwnerRuntimeSnapshot


class OwnerCommitmentBarrier:
    """Pause after the final real resolve and before owner commitment."""

    def __init__(self, monkeypatch: MonkeyPatch, owner_state: Path) -> None:
        self.calls: list[OwnerCommitmentBarrierCall] = []
        self.reached = Event()
        self._release = Event()
        self._owner_state = owner_state
        self._command: LockstepCommandService | None = None
        self._latest = local()
        original_resolve = OwnerRuntimeEffectAuthority.resolve
        original_commitment = OwnerRuntimeEffectAuthority.commitment

        def observe_resolve(
            authority: OwnerRuntimeEffectAuthority,
            intent: EffectRequest,
        ) -> EffectGrant:
            grant = original_resolve(authority, intent)
            self._latest.value = (authority, intent, grant)
            return grant

        @contextmanager
        def pause_before_commitment(
            authority: OwnerRuntimeEffectAuthority,
            grant: EffectGrant,
            request: EffectRequest,
            launch: PreparedLaunch,
        ):
            command = self._command
            if command is None:
                raise AssertionError("owner commitment barrier has no command service")
            latest = getattr(self._latest, "value", None)
            if latest is None:
                raise AssertionError("owner commitment has no preceding real resolve")
            resolved_authority, intent, resolved_grant = latest
            if (
                resolved_authority is not authority
                or resolved_grant != grant
                or intent.bind_grant(resolved_grant) != request
            ):
                raise AssertionError("owner commitment differs from final real resolve")
            record = command.effects.get(request.effect_id)
            owner_digest, owner_snapshot = open_runtime_snapshot(self._owner_state)
            self.calls.append(
                OwnerCommitmentBarrierCall(
                    intent,
                    grant,
                    request,
                    launch,
                    record,
                    owner_digest,
                    owner_snapshot,
                )
            )
            self.reached.set()
            if not self._release.wait(10.0):
                raise AssertionError("timed out releasing owner commitment barrier")
            with original_commitment(authority, grant, request, launch):
                yield

        monkeypatch.setattr(
            OwnerRuntimeEffectAuthority,
            "resolve",
            observe_resolve,
        )
        monkeypatch.setattr(
            OwnerRuntimeEffectAuthority,
            "commitment",
            pause_before_commitment,
        )

    def attach(self, command: LockstepCommandService) -> None:
        self._command = command

    def release(self) -> None:
        self._release.set()
