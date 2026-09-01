"""Writable activation, explicit recovery, and shutdown capability."""

from __future__ import annotations

import threading

from lockstep.runtime._service_values import Any, LockstepError, Path


class _ServiceActivationLifecycle:
    def _finish_writable_core_activation(
        self, deferred_start_run_id: str | None = None
    ) -> None:
        """Recover old work and publish one fully active writable core."""

        self._initial_recovery_exclusion = deferred_start_run_id
        try:
            self._recover_engine_effects()
        finally:
            self._initial_recovery_exclusion = None
        self._pump_failure = None
        self._pump_thread = threading.Thread(
            target=self._completion_pump,
            name="lockstep-effect-completion",
            daemon=True,
        )
        self._pump_thread.start()
        self._writable_core_active = True

    def _activate_writable_core(self) -> None:
        """Privately create command resources at the first writable intent."""

        with self._activation_lock:
            if self._writable_core_active:
                return
        self._start_activation.activate()

    def scenario_recover(
        self, project: str, *, limit: int = 128
    ) -> dict[str, Any]:
        """Explicitly run one bounded recovery sweep; status/wait/history never do."""

        if type(limit) is not int or not 1 <= limit <= self._MAX_ACTIVE_EFFECT_RUNS:
            raise LockstepError("scenario recover limit must be an integer from 1 to 128")
        project_identity = str(Path(project).resolve())
        self._activate_writable_core()
        recovered: list[str] = []
        with self._activation_lock, self._admission_recovery_lock:
            self._install_recovered_runtime_execution(
                limit=limit
            )
            recovered.extend(
                self._recovery_driver._sweep_run_drive_watches(
                    project_identity=project_identity,
                    limit=limit,
                )
            )
        return {"recovered": recovered, "count": len(recovered), "limit": limit}

    def close(self) -> None:
        with self._activation_lock:
            if self._closed:
                return
            self._closed = True
            if not self._writable_core_active:
                return
            self._writable_core_active = False
            self._pump_stop.set()
            self._pump_wakeup.set()
            pump_thread = self._pump_thread
            runtime = self.runtime
            store = self.store
        if pump_thread is not None:
            pump_thread.join()
        try:
            runtime.close()
        finally:
            store.close()
