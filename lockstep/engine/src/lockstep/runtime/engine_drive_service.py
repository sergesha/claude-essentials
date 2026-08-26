"""Bounded coordinator-owned effect drive over explicit runtime dependencies."""

from __future__ import annotations

from collections.abc import Callable

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.models import EffectDescriptor, ScopeDescriptor
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.status import ScenarioStatus, project_status


Protected = tuple[tuple[object, EffectDescriptor | ScopeDescriptor], ...]


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
        protected_descriptor: Callable[[object], EffectDescriptor | ScopeDescriptor | None],
        reserve_effect_run: Callable[[str], bool],
        activate_effect_run: Callable[[str], None],
        deactivate_effect_run: Callable[[str], None],
        acknowledge_start: Callable[[RunBinding, object, Protected], None],
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
        self._acknowledge_start = acknowledge_start

    def _protected(self, snapshot: object) -> Protected:
        return tuple(
            (interrupt, descriptor)
            for interrupt in snapshot.pending
            if (descriptor := self._protected_descriptor(interrupt)) is not None
        )

    def _settle(
        self,
        run_id: str,
        binding: RunBinding,
        snapshot: object,
        protected: Protected,
        status: ScenarioStatus,
        *,
        keep_active: bool,
    ) -> ScenarioStatus:
        self._acknowledge_start(binding, snapshot, protected)
        if keep_active:
            self._activate_effect_run(run_id)
        else:
            self._deactivate_effect_run(run_id)
        return status

    def _deliver(
        self,
        run_id: str,
        protected: Protected,
        actions: set[str],
    ) -> tuple[object, bool]:
        if "awaiting_delivery" not in actions:
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
    ) -> tuple[ScenarioStatus | None, object]:
        status = project_status(binding, snapshot, self._leases, self._effects)
        protected = self._protected(snapshot)
        if not protected:
            cleanup = self._coordinator.reconcile_consumed(run_id)
            if any(report.action == "busy" for report in cleanup):
                self._activate_effect_run(run_id)
                return status, snapshot
            return self._settle(
                run_id, binding, snapshot, protected, status, keep_active=False
            ), snapshot
        if status.status == "awaiting" and status.owner == "worker":
            return self._settle(
                run_id, binding, snapshot, protected, status, keep_active=False
            ), snapshot
        has_runner = any(
            isinstance(descriptor, EffectDescriptor)
            and descriptor.runner is not None
            for _interrupt, descriptor in protected
        )
        if has_runner and not self._reserve_effect_run(run_id):
            return status, snapshot
        reports = self._coordinator.reconcile_pending(run_id)
        actions = {report.action for report in reports}
        snapshot, delivery_blocked = self._deliver(run_id, protected, actions)
        status = project_status(binding, snapshot, self._leases, self._effects)
        if delivery_blocked:
            self._activate_effect_run(run_id)
            return status, snapshot
        current_protected = self._protected(snapshot)
        if status.status == "awaiting" and status.owner == "worker":
            return self._settle(
                run_id,
                binding,
                snapshot,
                current_protected,
                status,
                keep_active=False,
            ), snapshot
        progressive = {
            "prepared",
            "launch_claimed",
            "sealed",
            "delivered",
            "awaiting_delivery",
        }
        if not actions <= progressive:
            keep_active = bool(actions & {"running", "quiescence_pending", "busy"})
            return self._settle(
                run_id,
                binding,
                snapshot,
                current_protected,
                status,
                keep_active=keep_active,
            ), snapshot
        return None, snapshot

    def drive(
        self,
        run_id: str,
        *,
        binding: RunBinding | None = None,
        snapshot: object | None = None,
    ) -> ScenarioStatus:
        current_binding = binding or self._catalog.get(run_id)
        current_snapshot = snapshot or self._runtime.snapshot(run_id, subgraphs=True)
        for _decision in range(self._max_decisions):
            status, current_snapshot = self._decision(
                run_id, current_binding, current_snapshot
            )
            if status is not None:
                return status
        raise LockstepError("engine-owned progress exceeded its bounded decision budget")
