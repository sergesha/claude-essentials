"""Worker-result submission use case over explicit command dependencies."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from lockstep.runtime import config, sessions
from lockstep.runtime.effects.descriptors import derive_effect_id
from lockstep.runtime.effects.models import EffectDescriptor
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.evidence import project_path_errors, validate_evidence
from lockstep.runtime.graph_runtime import (
    NativeCoordinateRejected,
    NativeHistoryLimitExceeded,
)
from lockstep.runtime.providers.manual import ManualSubmission
from lockstep.runtime.status import project_status
from lockstep.runtime.validator_execution import validate_manual_checks


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

    @staticmethod
    def _validate_manual_pass(
        binding: object,
        interrupt: object,
        result: Mapping[str, Any],
        submission: ManualSubmission | None,
    ) -> None:
        if submission is None or submission.kind != "done":
            return
        value = getattr(interrupt, "value", None)
        if not isinstance(value, dict):
            raise LockstepError("protected manual interrupt has no evidence contract")
        try:
            evidence = json.loads(submission.payload)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LockstepError("protected manual PASS has invalid evidence") from exc
        if not isinstance(evidence, dict):
            raise LockstepError("protected manual PASS requires evidence")
        try:
            canonical_submission = ManualSubmission.build("PASS", evidence=evidence)
        except (TypeError, ValueError) as exc:
            raise LockstepError("protected manual PASS has invalid evidence") from exc
        if submission != canonical_submission:
            raise LockstepError("protected manual PASS evidence is not canonical")
        if result.get("evidence") != evidence:
            raise LockstepError("protected manual evidence does not match submission")
        project = getattr(binding, "project_identity", None)
        if not isinstance(project, str) or not project:
            raise LockstepError("protected manual run has no project identity")
        schema = value.get("evidence_schema")
        try:
            errors = validate_evidence(schema, evidence)
            errors.extend(project_path_errors(schema, evidence, project))
        except Exception as exc:
            raise LockstepError("invalid protected manual evidence contract") from exc
        if not errors:
            errors.extend(
                validate_manual_checks(value.get("checks"), evidence, project)
            )
        if errors:
            raise LockstepError("manual evidence rejected: " + "; ".join(errors))

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
                        if descriptor.kind == "manual":
                            self._validate_manual_pass(
                                binding, interrupt, result, manual_submission
                            )
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
