"""Bounded coordinator-owned effect drive over explicit runtime dependencies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects._coordinator_values import ReconcileAction
from lockstep.runtime.effects.models import (
    AcceptDescriptor,
    DecisionDescriptor,
    EffectDescriptor,
    PublishDescriptor,
    ScopeDescriptor,
)
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.status import ScenarioStatus, project_status

ProtectedDescriptor = (
    EffectDescriptor
    | ScopeDescriptor
    | DecisionDescriptor
    | AcceptDescriptor
    | PublishDescriptor
)
Protected = tuple[tuple[object, ProtectedDescriptor], ...]
_CONTINUATION_ACTIONS = frozenset(
    {
        ReconcileAction.PREPARED,
        ReconcileAction.LAUNCH_CLAIMED,
        ReconcileAction.SEALED,
        ReconcileAction.DELIVERED,
        ReconcileAction.AWAITING_DELIVERY,
    }
)
_ACCEPTED_ATTEMPT_ACTIONS = _CONTINUATION_ACTIONS | frozenset(
    {
        ReconcileAction.PUBLICATION_CLAIMED,
        ReconcileAction.PUBLICATION_PROGRESS,
        ReconcileAction.RUNNING,
        ReconcileAction.QUIESCENCE_PENDING,
        ReconcileAction.INDETERMINATE,
    }
)


@dataclass(frozen=True, slots=True)
class _DriveReport:
    status: ScenarioStatus
    accepted: bool


class EngineDriveService:
    """Own one bounded monotonic drive loop and its run-ownership decisions."""

    def __init__(
        self,
        *,
        runtime: object,
        catalog: object,
        leases: object,
        effects: object,
        coordinator: object,
        max_decisions: int,
        protected_descriptor: Callable[[object], ProtectedDescriptor | None],
        reserve_effect_run: Callable[[str], bool],
        activate_effect_run: Callable[[str], None],
        deactivate_effect_run: Callable[[str], None],
    ) -> None:
        self._runtime = runtime
        self._catalog = catalog
        self._leases = leases
        self._effects = effects
        self._coordinator = coordinator
        self._max_decisions = max_decisions
        self._protected_descriptor = protected_descriptor
        self._reserve_effect_run = reserve_effect_run
        self._activate_effect_run = activate_effect_run
        self._deactivate_effect_run = deactivate_effect_run

    def _protected(self, snapshot: object) -> Protected:
        return tuple(
            (interrupt, descriptor)
            for interrupt in snapshot.pending
            if (descriptor := self._protected_descriptor(interrupt)) is not None
        )

    @staticmethod
    def _accepted_attempt(actions: set[ReconcileAction]) -> bool:
        return bool(actions & _ACCEPTED_ATTEMPT_ACTIONS)

    @staticmethod
    def _requires_followup(actions: set[ReconcileAction]) -> bool:
        return (bool(actions) and actions <= _CONTINUATION_ACTIONS) or bool(
            actions
            & {
                ReconcileAction.RUNNING,
                ReconcileAction.QUIESCENCE_PENDING,
                ReconcileAction.BUSY,
            }
        )

    def _settle(
        self,
        run_id: str,
        status: ScenarioStatus,
        *,
        keep_active: bool,
    ) -> ScenarioStatus:
        if keep_active:
            self._activate_effect_run(run_id)
        else:
            self._deactivate_effect_run(run_id)
        return status

    def _deliver(
        self,
        run_id: str,
        protected: Protected,
        actions: set[ReconcileAction],
    ) -> tuple[object, bool]:
        if ReconcileAction.AWAITING_DELIVERY not in actions:
            return self._runtime.snapshot(run_id, subgraphs=True), False
        self._coordinator.deliver_ready(run_id)
        delivered = self._runtime.snapshot(run_id, subgraphs=True)
        source_coordinates = {
            interrupt.coordinate for interrupt, _descriptor in protected
        }
        source_still_pending = any(
            interrupt.coordinate in source_coordinates
            for interrupt in delivered.pending
        )
        return delivered, source_still_pending

    def _decision(
        self,
        run_id: str,
        binding: RunBinding,
        snapshot: object,
    ) -> tuple[ScenarioStatus | None, object, bool]:
        status = project_status(
            binding,
            snapshot,  # type: ignore[arg-type]
            self._leases,
            self._effects,
        )
        protected = self._protected(snapshot)
        if not protected:
            cleanup = self._coordinator.reconcile_consumed(run_id)
            if any(
                ReconcileAction(report.action) is ReconcileAction.BUSY
                for report in cleanup
            ):
                self._activate_effect_run(run_id)
                return status, snapshot, False
            return self._settle(run_id, status, keep_active=False), snapshot, False
        if status.status == "awaiting" and status.owner == "worker":
            return self._settle(run_id, status, keep_active=False), snapshot, False
        has_runner = any(
            isinstance(descriptor, EffectDescriptor)
            and descriptor.runner is not None
            for _interrupt, descriptor in protected
        )
        if has_runner and not self._reserve_effect_run(run_id):
            return status, snapshot, False
        reports = self._coordinator.reconcile_pending(run_id)
        actions = {ReconcileAction(report.action) for report in reports}
        if self._requires_followup(actions):
            self._activate_effect_run(run_id)
        accepted = self._accepted_attempt(actions)
        snapshot, delivery_blocked = self._deliver(run_id, protected, actions)
        status = project_status(
            binding,
            snapshot,  # type: ignore[arg-type]
            self._leases,
            self._effects,
        )
        if delivery_blocked:
            return status, snapshot, accepted
        if status.status == "awaiting" and status.owner == "worker":
            return self._settle(
                run_id,
                status,
                keep_active=False,
            ), snapshot, accepted
        if not actions <= _CONTINUATION_ACTIONS:
            keep_active = bool(
                actions
                & {
                    ReconcileAction.RUNNING,
                    ReconcileAction.QUIESCENCE_PENDING,
                    ReconcileAction.BUSY,
                }
            )
            return self._settle(
                run_id,
                status,
                keep_active=keep_active,
            ), snapshot, accepted
        return None, snapshot, accepted

    def _drive_report(
        self,
        run_id: str,
        *,
        binding: RunBinding | None = None,
        snapshot: object | None = None,
    ) -> _DriveReport:
        del snapshot
        with self._runtime.decision_guard(run_id):
            return self._drive_report_guarded(run_id, binding=binding)

    def _drive_report_guarded(
        self,
        run_id: str,
        *,
        binding: RunBinding | None = None,
    ) -> _DriveReport:
        current_binding = binding or self._catalog.get(run_id)
        current_snapshot = self._runtime.snapshot(run_id, subgraphs=True)
        accepted = False
        for _decision in range(self._max_decisions):
            status, current_snapshot, step_accepted = self._decision(
                run_id, current_binding, current_snapshot
            )
            accepted = accepted or step_accepted
            if status is not None:
                return _DriveReport(status, accepted)
        raise LockstepError("engine-owned progress exceeded its bounded decision budget")

    def drive(
        self,
        run_id: str,
        *,
        binding: RunBinding | None = None,
        snapshot: object | None = None,
    ) -> ScenarioStatus:
        return self._drive_report(
            run_id, binding=binding, snapshot=snapshot
        ).status

    def drive_recovered(self, run_id: str) -> bool:
        return self._drive_report(run_id).accepted
