"""Capability owner extracted from the command-service facade."""

from __future__ import annotations

from collections.abc import Callable

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.engine_drive_service import (
    EngineDriveService,
)
from lockstep.runtime.status import ScenarioStatus


class _ServiceEffectDrive:
    def _reserve_effect_run(self, run_id: str) -> bool:
        available, _owned = self._reserve_effect_run_owned(run_id)
        return available

    def _reserve_effect_run_owned(self, run_id: str) -> tuple[bool, bool]:
        with self._active_effect_lock:
            if run_id in self._active_effect_runs:
                return True, False
            if len(self._active_effect_runs) >= self._MAX_ACTIVE_EFFECT_RUNS:
                return False, False
            self._active_effect_runs.add(run_id)
            return True, True

    def _activate_effect_run(self, run_id: str) -> None:
        if not self._reserve_effect_run(run_id):
            return
        with self._active_effect_lock:
            if run_id not in self._queued_effect_runs:
                self._queued_effect_runs.add(run_id)
                self._active_effect_queue.append(run_id)
        self._pump_wakeup.set()

    def _deactivate_effect_run(self, run_id: str) -> None:
        with self._active_effect_lock:
            self._active_effect_runs.discard(run_id)
            self._queued_effect_runs.discard(run_id)

    def _release_failed_start_reservation(self, run_id: str) -> None:
        with self._active_effect_lock:
            if run_id in self._queued_effect_runs:
                return
            self._active_effect_runs.discard(run_id)

    def _finish_owned_effect_binding(self, run_id: str, owned: bool) -> None:
        if not owned:
            return
        release = False
        with self._active_effect_lock:
            if run_id in self._active_effect_runs:
                self._owned_effect_bindings.add(run_id)
            else:
                release = True
        if release:
            self.runtime.unbind(run_id)

    def _release_inactive_effect_binding(self, run_id: str) -> None:
        release = False
        with self._active_effect_lock:
            if (
                run_id not in self._active_effect_runs
                and run_id in self._owned_effect_bindings
            ):
                self._owned_effect_bindings.discard(run_id)
                release = True
        if release:
            self.runtime.unbind(run_id)

    def _take_active_effect_runs(self, limit: int = 128) -> tuple[str, ...]:
        selected = []
        with self._active_effect_lock:
            while self._active_effect_queue and len(selected) < limit:
                run_id = self._active_effect_queue.popleft()
                if run_id not in self._queued_effect_runs:
                    continue
                self._queued_effect_runs.discard(run_id)
                selected.append(run_id)
        return tuple(selected)

    def _drive_engine_owned(
        self,
        run_id: str,
        *,
        binding: RunBinding | None = None,
        snapshot=None,
    ) -> ScenarioStatus:
        """Advance only coordinator-owned effects through monotonic decisions."""
        try:
            return self._engine_drive_service().drive(
                run_id, binding=binding, snapshot=snapshot
            )
        finally:
            self._release_inactive_effect_binding(run_id)

    def _drive_recovered_run(self, run_id: str) -> bool:
        """Try one recovered run through the normal authoritative drive owner."""

        binding = self.catalog.get(run_id)
        owns_binding = self.runtime.bind(binding)
        owns_reservation = False

        def reserve_effect_run(recovered_run_id: str) -> bool:
            nonlocal owns_reservation
            available, owned = self._reserve_effect_run_owned(recovered_run_id)
            owns_reservation = owns_reservation or owned
            return available

        try:
            try:
                return self._engine_drive_service(
                    reserve_effect_run=reserve_effect_run
                ).drive_recovered(run_id)
            except BaseException:
                if owns_reservation:
                    self._deactivate_effect_run(run_id)
                raise
        finally:
            self._release_inactive_effect_binding(run_id)
            self._finish_owned_effect_binding(run_id, owns_binding)

    def _engine_drive_service(
        self,
        *,
        reserve_effect_run: Callable[[str], bool] | None = None,
    ) -> EngineDriveService:
        return EngineDriveService(
            runtime=self.runtime,
            catalog=getattr(self, "catalog", None),
            leases=self.leases,
            effects=self.effects,
            coordinator=self.coordinator,
            max_decisions=self._MAX_ENGINE_PROGRESS_DECISIONS,
            protected_descriptor=self._protected_interrupt_descriptor,
            reserve_effect_run=reserve_effect_run or self._reserve_effect_run,
            activate_effect_run=self._activate_effect_run,
            deactivate_effect_run=self._deactivate_effect_run,
        )

    def _drain_completion_runs(self) -> tuple[str, ...]:
        with self._admission_recovery_lock:
            active_run_ids = self._take_active_effect_runs()
            for run_id in active_run_ids:
                binding = self.catalog.get(run_id)
                self.runtime.bind(binding)
                self._drive_engine_owned(run_id, binding=binding)
        return active_run_ids
