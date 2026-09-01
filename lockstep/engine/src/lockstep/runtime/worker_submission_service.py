"""Worker-result submission use case over explicit command dependencies."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from lockstep.runtime import config, sessions
from lockstep.runtime.effects.descriptors import derive_effect_id
from lockstep.runtime.effects.models import EffectDescriptor
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.graph_runtime import (
    NativeCoordinateRejected,
    NativeHistoryLimitExceeded,
)
from lockstep.runtime.providers.manual import ManualSubmission
from lockstep.runtime.status import project_status


class WorkerSubmissionService:
    """Own session fencing and one exact worker interrupt submission."""

    def __init__(
        self,
        *,
        state_dir: object,
        runtime: object,
        manual_effect_resources: Callable[[], tuple[object, object]],
        admission_lock: object,
        validate_existing: Callable[[str, str], object],
        bind_existing: Callable[[str, str], object],
        select_interrupt: Callable[[str, str | None, str], tuple[object, object]],
        protected_descriptor: Callable[[object], EffectDescriptor | None],
        drive_engine_owned: Callable[..., object],
    ) -> None:
        self._state_dir = state_dir
        self._runtime = runtime
        self._manual_effect_resources = manual_effect_resources
        self._admission_lock = admission_lock
        self._validate_existing = validate_existing
        self._bind_existing = bind_existing
        self._select_interrupt = select_interrupt
        self._protected_descriptor = protected_descriptor
        self._drive_engine_owned = drive_engine_owned

    def _submit_manual(
        self,
        run_id: str,
        binding: object,
        interrupt: object,
        descriptor: EffectDescriptor,
        submission: ManualSubmission | None,
        session_id: str | None,
    ) -> dict[str, Any]:
        if descriptor.kind != "manual" or submission is None:
            raise LockstepError(
                "worker submission cannot target an engine-owned effect"
            )
        effect_id = derive_effect_id(interrupt.coordinate, descriptor.digest)
        assert session_id is not None
        leases, coordinator = self._manual_effect_resources()
        session_lease = leases.acquire(
            "session",
            effect_id,
            session_id,
            config.session_stale_minutes() * 60,
        )
        try:
            coordinator.submit_manual(
                run_id, interrupt.coordinate, submission
            )
        finally:
            leases.release(session_lease)
        return self._drive_engine_owned(run_id, binding=binding).to_dict()

    def resume(
        self,
        run_id: str,
        step: str | None,
        result: Mapping[str, Any],
        *,
        manual_submission: ManualSubmission | None,
        session_id: str | None,
        project: str,
    ) -> dict[str, Any]:
        with self._admission_lock:
            self._validate_existing(run_id, project)
            try:
                with sessions.locked_owner(
                    self._state_dir,
                    run_id,
                    session_id,
                    config.session_stale_minutes(),
                ), self._bind_existing(run_id, project):
                    binding, interrupt = self._select_interrupt(run_id, step, project)
                    descriptor = self._protected_descriptor(interrupt)
                    if descriptor is not None:
                        return self._submit_manual(
                            run_id,
                            binding,
                            interrupt,
                            descriptor,
                            manual_submission,
                            session_id,
                        )
                    snapshot = self._runtime.resume(
                        run_id,
                        interrupt.coordinate,
                        {interrupt.coordinate.interrupt_id: dict(result)},
                    )
            except PermissionError as exc:
                raise LockstepError(str(exc)) from exc
            except (NativeCoordinateRejected, NativeHistoryLimitExceeded) as exc:
                raise LockstepError(str(exc)) from exc
            return project_status(binding, snapshot, (), ()).to_dict()
