"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects._coordinator_values import (
    CoordinatorLineageError,
    ProviderContractViolation,
    ReconcileReport,
    _Context,
)
from lockstep.runtime.effects.authority import (
    EffectAuthorityDenied,
    EffectAuthorityUnavailable,
)
from lockstep.runtime.effects.descriptors import (
    parse_effect_descriptor,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.effects.models import (
    EffectDescriptor,
    EffectResult,
    ScopeDescriptor,
)
from lockstep.runtime.leases import Lease
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot
from lockstep.runtime.project_snapshots import ProjectSnapshotRef
from lockstep.runtime.providers.base import (
    DefinitiveProviderFailure,
    PreparedLaunch,
    RunnerObservation,
    launch_commitment_digest,
)


class _EffectCoordinatorRunner:
    def _reconcile_prepared_effect(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        context: _Context,
        record: EffectRecord,
        lease: Lease,
    ) -> ReconcileReport:
        if isinstance(context.descriptor, ScopeDescriptor):
            assert context.scope_result is not None
            sealed = self._ledger.seal(
                record.effect_id,
                context.scope_result,
                expected_revision=record.revision,
                lease=lease,
                scope_descriptor=context.descriptor,
            )
            return self._report(run_id, sealed, "sealed")
        if record.deadline_at is not None and record.deadline_at <= self._now():
            sealed = self._ledger.seal(
                record.effect_id,
                self._timeout_result(record.effect_id),
                expected_revision=record.revision,
                lease=lease,
            )
            return self._report(run_id, sealed, "sealed")
        if context.request is None:
            assert context.descriptor.kind == "manual"
            self._manual_handoff(
                binding, context.interrupt, context.descriptor
            )
            return self._report(run_id, record, "manual_pending")
        assert context.runner is not None
        try:
            launch = context.runner.prepare(context.request)
        except DefinitiveProviderFailure as failure:
            sealed = self._ledger.seal(
                record.effect_id,
                self._definitive_prelaunch_result(record, failure),
                expected_revision=record.revision,
                lease=lease,
            )
            return self._report(run_id, sealed, "sealed")
        self._check_launch(context.request, launch)
        launching = self._ledger.mark_launching(
            record.effect_id,
            expected_revision=record.revision,
            lease=lease,
            runner_binding_digest=context.request.runner_binding_digest,
            workspace_ref=launch.workspace_ref,
            launch_commitment_digest=launch_commitment_digest(
                context.request, launch
            ),
        )
        return self._report(run_id, launching, "launch_claimed")

    def _commit_runner_launch(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        context: _Context,
        record: EffectRecord,
        lease: Lease,
        launch: PreparedLaunch,
    ) -> RunnerObservation | ReconcileReport:
        assert context.runner is not None
        assert context.request is not None
        with self._runtime.commitment_guard(
            run_id, record.coordinate
        ) as guarded:
            if guarded.binding != binding:
                raise CoordinatorLineageError(
                    "native commitment binding differs from run catalog"
                )
            guarded_descriptor = parse_effect_descriptor(
                self._raw_descriptor(guarded.interrupt)
            )
            if (
                guarded.interrupt.coordinate != record.coordinate
                or guarded_descriptor.digest != record.descriptor_digest
            ):
                raise CoordinatorLineageError(
                    "native commitment guard observed a changed effect grant"
                )
            if (
                record.deadline_at is not None
                and record.deadline_at <= self._now()
            ):
                return self._report(run_id, record, "deadline_blocked")
            guarded_context = self._context(
                binding,
                guarded.snapshot,
                guarded.interrupt,
                descriptor=guarded_descriptor,
                effect_id=record.effect_id,
                record=record,
                resolve_grant=True,
            )
            if (
                guarded_context.request is None
                or guarded_context.grant is None
                or guarded_context.request != context.request
                or guarded_context.grant != context.grant
            ):
                raise CoordinatorLineageError(
                    "graph-owned effect request changed before commitment"
                )
            current = self._ledger.get(record.effect_id)
            if (
                current.revision != record.revision
                or current.phase != record.phase
                or not self._leases.is_current(lease)
            ):
                return self._report(run_id, current, "busy")
            with self._authority.commitment(
                guarded_context.grant,
                guarded_context.request,
                launch,
            ):
                return context.runner.ensure_started(launch)

    def _launch_observation_report(
        self,
        *,
        run_id: str,
        record: EffectRecord,
        lease: Lease,
        observation: RunnerObservation,
        expired: bool,
    ) -> ReconcileReport:
        if observation.state == "absent" and expired:
            sealed = self._ledger.seal(
                record.effect_id,
                self._timeout_result(record.effect_id),
                expected_revision=record.revision,
                lease=lease,
                runner_binding_digest=record.runner_binding_digest,
            )
            return self._report(run_id, sealed, "sealed")
        if observation.state == "indeterminate":
            indeterminate = self._ledger.mark_indeterminate(
                record.effect_id,
                expected_revision=record.revision,
                lease=lease,
            )
            return self._report(run_id, indeterminate, "indeterminate")
        if observation.state not in {"running", "terminal"}:
            raise ProviderContractViolation("unknown launch observation state")
        running = self._ledger.mark_running(
            record.effect_id,
            expected_revision=record.revision,
            lease=lease,
            runner_binding_digest=record.runner_binding_digest,
        )
        return self._report(run_id, running, "running")

    def _reconcile_launching_effect(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        context: _Context,
        record: EffectRecord,
        lease: Lease,
    ) -> ReconcileReport:
        assert context.runner is not None
        if not self._leases.is_current(lease):
            return self._report(run_id, record, "busy")
        expired = (
            record.deadline_at is not None
            and record.deadline_at <= self._now()
        )
        if expired:
            observation = context.runner.inspect(record.effect_id)
        else:
            assert context.request is not None
            launch = context.runner.prepare(context.request)
            self._check_launch(context.request, launch)
            if record.launch_commitment_digest != launch_commitment_digest(
                context.request, launch
            ):
                raise ProviderContractViolation(
                    "prepared launch differs from durable launch commitment"
                )
            committed = self._commit_runner_launch(
                run_id=run_id,
                binding=binding,
                context=context,
                record=record,
                lease=lease,
                launch=launch,
            )
            if isinstance(committed, ReconcileReport):
                return committed
            observation = committed
        self._check_observation(
            context.request if context.request is not None else record,
            observation,
        )
        return self._launch_observation_report(
            run_id=run_id,
            record=record,
            lease=lease,
            observation=observation,
            expired=expired,
        )

    def _reconcile_expired_running_effect(
        self,
        *,
        run_id: str,
        context: _Context,
        record: EffectRecord,
        lease: Lease,
    ) -> ReconcileReport:
        assert context.runner is not None
        if not self._leases.is_current(lease):
            return self._report(run_id, record, "busy")
        cancelled = context.runner.cancel(record.effect_id)
        self._check_observation(record, cancelled)
        safety = context.runner.quiesce(record.effect_id)
        timeout_result = self._timeout_result(record.effect_id)
        if not self._terminal_safety(
            context, safety, result=timeout_result, binding=record
        ):
            return self._report(run_id, record, "quiescence_pending")
        sealed = self._ledger.seal(
            record.effect_id,
            timeout_result,
            expected_revision=record.revision,
            lease=lease,
            runner_binding_digest=record.runner_binding_digest,
        )
        return self._report(run_id, sealed, "sealed")

    def _adopt_effect_successor(
        self,
        *,
        binding: RunBinding,
        context: _Context,
        record: EffectRecord,
        result: EffectResult,
    ) -> None:
        if result.snapshot_ref is None:
            return
        if not result.snapshot_ref.startswith("snapshot:"):
            raise ProviderContractViolation(
                "effect rollover snapshot reference is invalid"
            )
        if self._snapshot_resolver is not None:
            self._snapshot_resolver.adopt_successor(
                binding,
                context.interrupt,
                context.descriptor,
                record.effect_id,
                ProjectSnapshotRef(
                    result.snapshot_ref.removeprefix("snapshot:")
                ),
            )

    def _reconcile_live_running_effect(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        context: _Context,
        record: EffectRecord,
        lease: Lease,
    ) -> ReconcileReport:
        assert context.runner is not None
        observation = context.runner.inspect(record.effect_id)
        self._check_observation(record, observation)
        if observation.state == "running":
            return self._report(run_id, record, "running")
        if observation.state != "terminal":
            raise ProviderContractViolation(
                "provider result must be a closed ordinary EffectResult"
            )
        result = self._closed_result(observation.result)
        if result.effect_id != record.effect_id:
            raise ProviderContractViolation("provider result targets another effect")
        safety = context.runner.quiesce(record.effect_id)
        if not self._terminal_safety(
            context, safety, result=result, binding=record
        ):
            return self._report(run_id, record, "quiescence_pending")
        result = self._admit_artifacts(binding, context, record, result, safety)
        self._adopt_effect_successor(
            binding=binding,
            context=context,
            record=record,
            result=result,
        )
        sealed = self._ledger.seal(
            record.effect_id,
            result,
            expected_revision=record.revision,
            lease=lease,
            runner_binding_digest=record.runner_binding_digest,
        )
        return self._report(run_id, sealed, "sealed")

    def _reconcile_running_effect(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        context: _Context,
        record: EffectRecord,
        lease: Lease,
    ) -> ReconcileReport:
        expired = (
            record.deadline_at is not None
            and record.deadline_at <= self._now()
        )
        if expired:
            return self._reconcile_expired_running_effect(
                run_id=run_id,
                context=context,
                record=record,
                lease=lease,
            )
        return self._reconcile_live_running_effect(
            run_id=run_id,
            binding=binding,
            context=context,
            record=record,
            lease=lease,
        )

    def _dispatch_effect_phase(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        context: _Context,
        record: EffectRecord | None,
        lease: Lease,
    ) -> ReconcileReport:
        if record is None:
            return self._prepare_new_effect(
                run_id=run_id,
                binding=binding,
                context=context,
                lease=lease,
            )
        phase_handlers = {
            "prepared": self._reconcile_prepared_effect,
            "launching": self._reconcile_launching_effect,
            "running": self._reconcile_running_effect,
        }
        handler = phase_handlers.get(record.phase)
        if handler is not None:
            return handler(
                run_id=run_id,
                binding=binding,
                context=context,
                record=record,
                lease=lease,
            )
        if record.phase in {"sealed", "indeterminate"}:
            return self._report(run_id, record, "awaiting_delivery")
        return self._report(run_id, record, "unchanged")

    def _reconcile_context(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        snapshot: NativeSnapshot,
        interrupt: NativeInterrupt,
        descriptor: EffectDescriptor | ScopeDescriptor,
        effect_id: str,
        record: EffectRecord | None,
        lease: Lease,
    ) -> _Context | ReconcileReport:
        try:
            return self._context(
                binding,
                snapshot,
                interrupt,
                descriptor=descriptor,
                effect_id=effect_id,
                record=record,
                resolve_grant=(
                    record is None
                    or record.phase == "prepared"
                    or (
                        record.phase == "launching"
                        and (
                            record.deadline_at is None
                            or record.deadline_at > self._now()
                        )
                    )
                ),
            )
        except (EffectAuthorityDenied, EffectAuthorityUnavailable):
            if (
                record is None
                or record.phase != "launching"
                or not isinstance(descriptor, EffectDescriptor)
                or descriptor.runner is None
            ):
                raise
            observation = self._authority_blocked_observation(record=record)
            return self._commit_authority_blocked_observation(
                run_id=run_id,
                record=record,
                observation=observation,
                lease=lease,
            )

    def _prepare_new_effect(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        context: _Context,
        lease: Lease,
    ) -> ReconcileReport:
        if (
            isinstance(context.descriptor, EffectDescriptor)
            and context.descriptor.kind == "manual"
        ):
            self._manual_handoff(
                binding, context.interrupt, context.descriptor
            )
        prepared = self._ledger.prepare(
            context.interrupt.coordinate,
            context.descriptor,
            deadline_at=context.deadline_at,
            runner_binding_digest=(
                None if context.runner is None else context.runner.binding_digest
            ),
            workspace_ref=(
                None if context.grant is None else context.grant.workspace_ref
            ),
            request_digest=(
                None if context.request is None else context.request.request_digest
            ),
            grant_digest=None if context.grant is None else context.grant.digest,
            lease=lease,
        )
        return self._report(run_id, prepared, "prepared")

    def _definitive_prelaunch_result(
        self,
        record: EffectRecord,
        failure: DefinitiveProviderFailure,
    ) -> EffectResult:
        result = self._closed_result(failure.result)
        if (
            result.effect_id != record.effect_id
            or result.outcome != "ERROR"
            or result.result_ref is not None
            or result.artifact_refs
            or result.snapshot_ref is not None
            or result.diff_ref is not None
            or result.evidence_refs
        ):
            raise ProviderContractViolation(
                "definitive prelaunch rejection must be a closed ERROR"
            ) from failure
        return result
