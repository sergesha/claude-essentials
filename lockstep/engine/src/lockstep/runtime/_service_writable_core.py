"""Writable-core preparation, rollback, and runtime configuration capability."""

from __future__ import annotations

from contextlib import suppress

from lockstep.runtime._service_values import (
    LockstepError,
    RuntimeExecutionAdmission,
    RuntimeSchemaMigrator,
    _RecoveryDriver,
)


class _ServiceWritableCore:
    def _prepare_writable_core(self) -> None:
        """Open complete writable resources without recovery or background work."""

        self._open_writable_stores()
        self._open_graph_runtime()
        if self._runtime_execution_context is None:
            self._runtime_execution_context = (
                self._reconstruct_runtime_execution_context()
            )
        self._open_effect_coordinator()
        self._recovery_driver = _RecoveryDriver(
            catalog=self.catalog,
            runtime=self.runtime,
            effects=self.effects,
            blobs=self.blobs,
            migrator=RuntimeSchemaMigrator(self.store),
            coordinator=self.coordinator,
            snapshot_resolver=self.snapshot_resolver,
            exclude_run_drive=lambda run_id: (
                run_id == self._initial_recovery_exclusion
            ),
            drive_recovered_run=self._drive_recovered_run,
        )

    def _rollback_writable_core_activation(self) -> None:
        """Return a failed first activation to a clean, retryable state."""

        runtime = getattr(self, "runtime", None)
        store = getattr(self, "store", None)
        if runtime is not None:
            with suppress(Exception):
                runtime.close()
        if store is not None:
            with suppress(Exception):
                store.close()
        with self._active_effect_lock:
            self._active_effect_runs.clear()
            self._owned_effect_bindings.clear()
            self._queued_effect_runs.clear()
            self._active_effect_queue.clear()
        self._pump_thread = None
        self._pump_failure = None
        self._pump_stop.clear()
        self._pump_wakeup.clear()
        self._initial_recovery_exclusion = None
        self._runtime_execution_composition = None
        self._runtime_execution_context = None
        self._recovery_driver = None
        self._writable_core_active = False

    def _configure_runtime_execution(
        self, admission: RuntimeExecutionAdmission | None
    ) -> None:
        if admission is None:
            return
        context = admission.context
        current = self._runtime_execution_context
        if current is not None and current != context:
            raise LockstepError("command runtime execution snapshot changed")
        if current is not None:
            return
        if self._writable_core_active:
            self._install_runtime_execution(context)
        else:
            self._runtime_execution_context = context
