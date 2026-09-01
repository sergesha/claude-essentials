"""Recovery sweep and completion-pump capability."""

from __future__ import annotations

from lockstep.runtime._service_values import LockstepError


class _ServiceRecoveryPump:
    def _recover_engine_effects(self) -> None:
        """Adopt durable protected work without a scheduler or status side effect."""

        self._install_recovered_runtime_execution()
        with self._admission_recovery_lock:
            self._recovery_driver._sweep_run_drive_watches(
                project_identity=None,
                limit=self._MAX_ACTIVE_EFFECT_RUNS,
            )

    def _completion_pump(self) -> None:
        """Adopt terminal runner observations through the same coordinator."""

        while not self._pump_stop.is_set():
            explicitly_woken = self._pump_wakeup.wait(0.25)
            self._pump_wakeup.clear()
            if self._pump_stop.is_set():
                return
            try:
                active_run_ids = self._drain_completion_runs()
                if active_run_ids or not explicitly_woken:
                    self._recover_engine_effects()
            except Exception as exc:  # noqa: BLE001 - retain cross-provider failure
                self._pump_failure = exc
                return

    def _check_completion_pump(self) -> None:
        if self._pump_failure is not None:
            raise LockstepError(
                "engine-owned completion pump failed"
            ) from self._pump_failure
