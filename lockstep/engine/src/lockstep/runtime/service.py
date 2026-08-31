"""Public scenario command facade over one shared mutable state owner."""

from __future__ import annotations

import sys
import threading
from collections import deque
from pathlib import Path

from lockstep.recipe.authority import (
    RecipeAuthorityPolicy,
)
from lockstep.runtime import _service_composition as _composition_module
from lockstep.runtime import _service_interrupt_descriptors as _interrupt_module
from lockstep.runtime import _service_start as _start_module
from lockstep.runtime._service_activation_lifecycle import (
    _ServiceActivationLifecycle,
)
from lockstep.runtime._service_composition import _ServiceComposition
from lockstep.runtime._service_effect_drive import _ServiceEffectDrive
from lockstep.runtime._service_interrupt_descriptors import _ServiceInterruptDescriptors
from lockstep.runtime._service_payloads import (
    validate_evidence_payload,
    validate_evidence_shape,
    validate_reason_payload,
)
from lockstep.runtime._service_preflight import (
    _resolve_preflight_recipe,
    _ServiceRecipeLookup,
    preflight_recipe,
)
from lockstep.runtime._service_publication_consent import _ServicePublicationConsent
from lockstep.runtime._service_recovery_pump import _ServiceRecoveryPump
from lockstep.runtime._service_session import _ServiceSession
from lockstep.runtime._service_start import _ServiceStart
from lockstep.runtime._service_worker import _ServiceWorker
from lockstep.runtime._service_writable_core import _ServiceWritableCore
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.recovery_driver import RecoveryDriver as _RecoveryDriver
from lockstep.runtime.runtime_execution import (
    RuntimeExecutionContext,
    build_runtime_execution_composition,
)
from lockstep.runtime.start_input import validate_start_input
from lockstep.runtime.start_service import (
    _WritableCoreActivation,
    plan_authorized_start,
)
from lockstep.runtime.storage import SQLiteStore

__all__ = (
    "LockstepCommandService",
    "LockstepError",
    "SQLiteStore",
    "_resolve_preflight_recipe",
    "build_runtime_execution_composition",
    "plan_authorized_start",
    "preflight_recipe",
    "validate_evidence_payload",
    "validate_evidence_shape",
    "validate_reason_payload",
    "validate_start_input",
)


class LockstepCommandService(
    _ServiceInterruptDescriptors,
    _ServiceEffectDrive,
    _ServiceComposition,
    _ServiceWritableCore,
    _ServiceRecoveryPump,
    _ServiceActivationLifecycle,
    _ServiceRecipeLookup,
    _ServiceStart,
    _ServiceSession,
    _ServiceWorker,
    _ServicePublicationConsent,
):
    _MAX_ENGINE_PROGRESS_DECISIONS = 32
    _MAX_ACTIVE_EFFECT_RUNS = 128

    def __init__(
        self,
        state_dir: Path,
        recipes_dir: Path,
        *,
        authority_policy: RecipeAuthorityPolicy | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.recipes_dir = Path(recipes_dir).resolve()
        self.authority_policy = authority_policy or RecipeAuthorityPolicy()
        self._runtime_execution_context: RuntimeExecutionContext | None = None
        self._runtime_execution_composition = None
        self._recovery_driver: _RecoveryDriver | None = None
        self._activation_lock = threading.RLock()
        self._writable_core_active = False
        self._closed = False
        self._pump_stop = threading.Event()
        self._pump_wakeup = threading.Event()
        self._active_effect_runs: set[str] = set()
        self._owned_effect_bindings: set[str] = set()
        self._initial_recovery_exclusion: str | None = None
        self._queued_effect_runs: set[str] = set()
        self._active_effect_queue: deque[str] = deque()
        self._active_effect_lock = threading.Lock()
        # A newly durable dispatch watch must be adopted by exactly one drive.
        # Serialize foreground admission with recovery enumeration so the pump
        # cannot finish and unbind a run between two foreground app uses.
        self._admission_recovery_lock = threading.RLock()
        self._pump_thread: threading.Thread | None = None
        self._pump_failure: BaseException | None = None
        self._start_activation = _WritableCoreActivation(
            lock=self._activation_lock,
            admission_lock=self._admission_recovery_lock,
            is_active=lambda: self._writable_core_active,
            is_closed=lambda: self._closed,
            prepare=self._prepare_writable_core,
            finish=self._finish_writable_core_activation,
            rollback=self._rollback_writable_core_activation,
            record_degraded=lambda exc: setattr(self, "_pump_failure", exc),
        )


_composition_module._SERVICE_FACADE = sys.modules[__name__]
_interrupt_module.LockstepCommandService = LockstepCommandService
_start_module._SERVICE_FACADE = sys.modules[__name__]
