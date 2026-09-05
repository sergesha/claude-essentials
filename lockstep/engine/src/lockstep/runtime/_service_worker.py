"""Capability owner extracted from the command-service facade."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from lockstep.runtime import config, sessions
from lockstep.runtime._service_payloads import (
    validate_evidence_payload,
    validate_reason_payload,
)
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.models import AcceptDescriptor
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.providers.manual import ManualSubmission
from lockstep.runtime.status import ScenarioStatus, project_status
from lockstep.runtime.worker_submission_service import WorkerSubmissionService


class _ServiceWorker:
    def _snapshot_status(
        self, run_id: str, project: str
    ) -> tuple[RunBinding, ScenarioStatus]:
        self._check_completion_pump()
        binding = self.runtime.binding(run_id)
        snapshot = self.runtime.snapshot(run_id, subgraphs=True)
        status = project_status(binding, snapshot, self.leases, self.effects)
        if status.status == "awaiting" and status.owner == "worker":
            session_binding = sessions.read_binding(self.state_dir, run_id)
            if not sessions.is_live(session_binding, config.session_stale_minutes()):
                status = replace(
                    status,
                    annotations=status.annotations
                    + (("binding_integrity", "missing_or_stale"),),
                )
        return binding, status

    def _worker_interrupt(self, run_id: str, step: str | None, project: str):
        binding, status = self._snapshot_status(run_id, project)
        if status.status != "awaiting" or status.owner != "worker":
            raise LockstepError(f"run {run_id} is not awaiting worker input")
        snapshot = self.runtime.snapshot(run_id, subgraphs=True)
        matches = []
        for interrupt in snapshot.pending:
            value = interrupt.value
            observed_step = value.get("step") if isinstance(value, dict) else None
            descriptor = self._protected_interrupt_descriptor(interrupt)
            protected = descriptor is not None
            selected_step = (
                observed_step
                if observed_step is not None
                else descriptor.logical_id
                if descriptor is not None
                else None
            )
            if (
                step is None
                or selected_step == step
                or (observed_step is None and not protected)
            ):
                matches.append(interrupt)
        if len(matches) != 1:
            raise LockstepError(
                "worker step does not identify exactly one pending interrupt"
            )
        matched = matches[0]
        matched_descriptor = self._protected_descriptor(matched)
        matched_step = (
            matched.value.get("step") if isinstance(matched.value, dict) else None
        ) or (matched_descriptor.logical_id if matched_descriptor is not None else None)
        if step is not None and matched_step is not None and matched_step != step:
            raise LockstepError(f"run {run_id} is parked on another step")
        return binding, matched

    def _resume_worker(
        self,
        run_id: str,
        step: str | None,
        result: Mapping[str, Any],
        *,
        manual_submission: ManualSubmission | None = None,
        session_id: str | None,
        project: str,
    ) -> dict[str, Any]:
        self._activate_writable_core()
        return WorkerSubmissionService(
            state_dir=self.state_dir,
            runtime=self.runtime,
            manual_effect_resources=lambda: (self.leases, self.coordinator),
            admission_lock=self._admission_recovery_lock,
            validate_existing=self._existing_run,
            bind_existing=self._bind_existing,
            select_interrupt=self._worker_interrupt,
            protected_descriptor=self._protected_descriptor,
            drive_engine_owned=self._drive_engine_owned,
        ).resume(
            run_id,
            step,
            result,
            manual_submission=manual_submission,
            session_id=session_id,
            project=project,
        )

    def scenario_done(
        self,
        run_id: str,
        step: str,
        evidence: dict,
        *,
        session_id: str | None,
        project: str,
    ) -> dict[str, Any]:
        checked_evidence = validate_evidence_payload(evidence)
        return self._resume_worker(
            run_id,
            step,
            {
                "schema": "lockstep.worker-result/v1",
                "outcome": "PASS",
                "evidence": checked_evidence,
            },
            manual_submission=ManualSubmission.build("PASS", evidence=checked_evidence),
            session_id=session_id,
            project=project,
        )

    def _pending_acceptance(
        self,
        run_id: str,
        step: str,
        *,
        project: str,
    ):
        if not isinstance(step, str) or not step:
            raise LockstepError("acceptance step must be non-empty text")
        binding = self.runtime.binding(run_id)
        snapshot = self.runtime.snapshot(run_id, subgraphs=True)
        matches = []
        for interrupt in snapshot.pending:
            descriptor = self._protected_interrupt_descriptor(interrupt)
            observed_step = (
                interrupt.value.get("step")
                if isinstance(interrupt.value, dict)
                else None
            )
            if isinstance(descriptor, AcceptDescriptor) and (
                descriptor.logical_id == step or observed_step == step
            ):
                matches.append(interrupt)
        if len(matches) != 1:
            raise LockstepError(
                "acceptance step does not identify exactly one pending interrupt"
            )
        return binding, matches[0]

    def scenario_escalate(
        self,
        run_id: str,
        reason: str,
        *,
        step: str | None = None,
        session_id: str | None,
        project: str,
    ) -> dict[str, Any]:
        checked_reason = validate_reason_payload(reason)
        return self._resume_worker(
            run_id,
            step,
            {
                "schema": "lockstep.worker-result/v1",
                "outcome": "FAIL",
                "reason": checked_reason,
            },
            manual_submission=ManualSubmission.build("FAIL", reason=checked_reason),
            session_id=session_id,
            project=project,
        )

    def scenario_abort(
        self,
        run_id: str,
        *,
        step: str | None = None,
        session_id: str | None,
        project: str,
    ) -> dict[str, Any]:
        return self._resume_worker(
            run_id,
            step,
            {"schema": "lockstep.worker-result/v1", "outcome": "ABORTED"},
            manual_submission=ManualSubmission.build("ABORTED"),
            session_id=session_id,
            project=project,
        )

    def done(
        self,
        run_id: str,
        step: str,
        evidence: dict,
        *,
        session_id: str | None = None,
        project: str,
    ):
        return self.scenario_done(
            run_id, step, evidence, session_id=session_id, project=project
        )

    def escalate(
        self,
        run_id: str,
        reason: str,
        *,
        step: str | None = None,
        session_id: str | None = None,
        project: str,
    ):
        return self.scenario_escalate(
            run_id, reason, step=step, session_id=session_id, project=project
        )

    def abort(
        self,
        run_id: str,
        *,
        step: str | None = None,
        session_id: str | None = None,
        project: str,
    ):
        return self.scenario_abort(
            run_id, step=step, session_id=session_id, project=project
        )
