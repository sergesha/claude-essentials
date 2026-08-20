"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.effects.descriptors import (
    build_scope_result,
    derive_effect_id,
    parse_effect_descriptor,
    parse_effect_result,
    parse_scope_result,
)
from lockstep.runtime.effects.ledger import (
    EffectLedger,
    EffectRecord,
    StaleEffectLease,
    StaleEffectRevision,
)
from lockstep.runtime.effects.models import (
    EffectDescriptor,
    EffectResult,
    ScopeDescriptor,
    ScopeResult,
)
from lockstep.runtime.graph_runtime import GraphRuntime
from lockstep.runtime.leases import Lease, LeaseStore, LeaseUnavailable
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot
from lockstep.runtime.providers.base import (
    EffectRequest,
    PreparedLaunch,
    RunnerAdapter,
    RunnerObservation,
    ScopeBinding,
    TerminalSafetyObservation,
)
from lockstep.runtime.status import ScenarioStatus, project_status


class CoordinatorLineageError(RuntimeError):
    """Durable effect facts disagree with the current public native lineage."""


class ProviderContractViolation(RuntimeError):
    """A runner returned an unbound, malformed, or authority-bearing value."""


@dataclass(frozen=True)
class ReconcileReport:
    run_id: str
    effect_id: str | None
    action: str
    phase: str | None


@dataclass(frozen=True)
class _Context:
    interrupt: NativeInterrupt
    descriptor: EffectDescriptor | ScopeDescriptor
    effect_id: str
    deadline_at: datetime | None
    scope_result: ScopeResult | None
    request: EffectRequest | None
    runner: RunnerAdapter | None
    prepared_launch: PreparedLaunch | None


class EffectCoordinator:
    """Make one monotonic external-effect reconciliation decision per call."""

    MAX_DUE_PER_SCAN = 128

    def __init__(
        self,
        *,
        runtime: GraphRuntime,
        catalog: RunCatalog,
        ledger: EffectLedger,
        leases: LeaseStore,
        runners: Mapping[str, RunnerAdapter],
        clock: Callable[[], datetime] | None = None,
        owner_factory: Callable[[], str] | None = None,
        lease_ttl: float = 30.0,
    ) -> None:
        self._runtime = runtime
        self._catalog = catalog
        self._ledger = ledger
        self._leases = leases
        self._runners = dict(runners)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner_factory = owner_factory or (lambda: secrets.token_hex(16))
        self._lease_ttl = lease_ttl

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coordinator clock must include a timezone")
        return value.astimezone(UTC)

    def _binding(self, run_id: str) -> RunBinding:
        catalog_binding = self._catalog.get(run_id)
        runtime_binding = self._runtime.binding(run_id)
        if catalog_binding != runtime_binding:
            raise CoordinatorLineageError(
                "runtime binding differs from immutable run catalog lineage"
            )
        return catalog_binding

    @staticmethod
    def _raw_descriptor(interrupt: NativeInterrupt) -> object | None:
        if not isinstance(interrupt.value, dict):
            return None
        return interrupt.value.get("lockstep_effect")

    def _protected(self, snapshot: NativeSnapshot) -> list[NativeInterrupt]:
        return [
            interrupt
            for interrupt in snapshot.pending
            if isinstance(self._raw_descriptor(interrupt), dict)
            and self._raw_descriptor(interrupt).get("schema") == "lockstep.effect/v1"
        ]

    @staticmethod
    def _ancestor_results(
        descriptor: EffectDescriptor | ScopeDescriptor, snapshot: NativeSnapshot
    ) -> tuple[ScopeResult, ...]:
        keys = (
            descriptor.scope_state_keys
            if isinstance(descriptor, EffectDescriptor)
            else descriptor.ancestor_deadline_state_keys
        )
        results = []
        for key in keys:
            if key not in snapshot.values:
                raise CoordinatorLineageError(
                    f"protected descriptor references absent graph state {key!r}"
                )
            results.append(parse_scope_result(snapshot.values[key]))
        return tuple(results)

    @staticmethod
    def _deadline_candidates(
        descriptor: EffectDescriptor | ScopeDescriptor,
        ancestors: Sequence[ScopeResult],
        now: datetime,
    ) -> list[datetime]:
        candidates = [
            item.absolute_deadline
            for item in ancestors
            if item.absolute_deadline is not None
        ]
        if any(item.outcome == "ERROR" for item in ancestors):
            candidates.append(now)
        duration = (
            descriptor.deadline_seconds
            if isinstance(descriptor, EffectDescriptor)
            else descriptor.duration_seconds
        )
        if duration is not None:
            candidates.append(now + timedelta(seconds=duration))
        return candidates

    def _runner_for(self, selector: str) -> RunnerAdapter:
        try:
            return self._runners[selector]
        except KeyError as exc:
            raise ProviderContractViolation(
                f"no trusted runner is bound for selector {selector!r}"
            ) from exc

    @staticmethod
    def _check_launch(request: EffectRequest, launch: PreparedLaunch) -> None:
        if (
            launch.effect_id != request.effect_id
            or launch.request_digest != request.request_digest
            or launch.runner_binding_digest != request.runner_binding_digest
        ):
            raise ProviderContractViolation(
                "prepared launch does not match the immutable effect request"
            )
        for label, value, optional in (
            ("launch_ref", launch.launch_ref, False),
            ("workspace_ref", launch.workspace_ref, True),
        ):
            if value is None and optional:
                continue
            if (
                not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > 4096
            ):
                raise ProviderContractViolation(
                    f"provider {label} must be a bounded non-empty string"
                )

    @staticmethod
    def _closed_result(value: object) -> EffectResult:
        if not isinstance(value, EffectResult):
            raise ProviderContractViolation(
                "provider result must be a closed bounded EffectResult"
            )
        try:
            parsed = parse_effect_result(value.to_dict())
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ProviderContractViolation(
                "provider result must be a closed bounded EffectResult"
            ) from exc
        if parsed != value:
            raise ProviderContractViolation(
                "provider result must be a canonical closed bounded EffectResult"
            )
        return parsed

    @staticmethod
    def _check_observation(
        request: EffectRequest,
        observation: RunnerObservation | TerminalSafetyObservation,
    ) -> None:
        if (
            observation.effect_id != request.effect_id
            or observation.request_digest != request.request_digest
            or observation.runner_binding_digest != request.runner_binding_digest
        ):
            raise ProviderContractViolation(
                "runner observation does not match the immutable effect request"
            )
        if isinstance(observation, RunnerObservation):
            if observation.state == "terminal":
                EffectCoordinator._closed_result(observation.result)
            elif observation.result is not None:
                raise ProviderContractViolation(
                    "nonterminal runner observation cannot carry a result"
                )
        elif observation.state == "pending" and (
            observation.result_stable or observation.rollover_snapshot_ref is not None
        ):
            raise ProviderContractViolation(
                "pending terminal-safety observation cannot carry proof fields"
            )

    def _context(
        self,
        binding: RunBinding,
        snapshot: NativeSnapshot,
        interrupt: NativeInterrupt,
        *,
        descriptor: EffectDescriptor | ScopeDescriptor,
        effect_id: str,
        record: EffectRecord | None,
    ) -> _Context:
        coordinate = interrupt.coordinate
        now = self._now()
        ancestors = self._ancestor_results(descriptor, snapshot)
        candidates = self._deadline_candidates(descriptor, ancestors, now)
        deadline_at = (
            record.deadline_at
            if record is not None
            else (min(candidates) if candidates else None)
        )
        if isinstance(descriptor, ScopeDescriptor):
            runner = (
                self._runner_for(descriptor.runner_selector)
                if descriptor.runner_selector is not None
                else None
            )
            binding_digest = None if runner is None else runner.binding_digest
            scope_result = build_scope_result(
                effect_id=effect_id,
                scope_digest=descriptor.digest,
                scope_kind=descriptor.scope_kind,
                now=now,
                duration_seconds=(
                    None if record is not None else descriptor.duration_seconds
                ),
                ancestors=ancestors,
                runner_selector=descriptor.runner_selector,
                runner_binding_digest=binding_digest,
            )
            if record is not None and scope_result.outcome == "PASS":
                if deadline_at is not None and deadline_at <= now:
                    scope_result = ScopeResult(
                        schema="lockstep.scope-result/v1",
                        effect_id=effect_id,
                        outcome="ERROR",
                        scope_kind=descriptor.scope_kind,
                        scope_digest=descriptor.digest,
                        fixed_error_code="scope_timeout",
                    )
                else:
                    scope_result = ScopeResult(
                        scope_result.schema,
                        scope_result.effect_id,
                        scope_result.outcome,
                        scope_result.scope_kind,
                        scope_result.scope_digest,
                        deadline_at,
                        scope_result.runner_selector,
                        scope_result.runner_binding_digest,
                    )
            return _Context(
                interrupt,
                descriptor,
                effect_id,
                deadline_at,
                scope_result,
                None,
                runner,
                None,
            )

        if descriptor.runner is None:
            # Manual work has no external runner. Task 7 owns its session/result path.
            return _Context(
                interrupt,
                descriptor,
                effect_id,
                None,
                None,
                None,
                None,
                None,
            )
        runner = self._runner_for(descriptor.runner.selector)
        if record is not None and record.runner_binding_digest != runner.binding_digest:
            raise ProviderContractViolation(
                "current runner binding differs from the durable effect intent"
            )
        scope_bindings = []
        for ancestor in ancestors:
            if ancestor.scope_kind == "call" and (
                ancestor.runner_selector != descriptor.runner.selector
                or ancestor.runner_binding_digest != runner.binding_digest
            ):
                raise ProviderContractViolation(
                    "call scope runner binding does not match the selected adapter"
                )
            scope_bindings.append(
                ScopeBinding(
                    scope_digest=ancestor.scope_digest,
                    runner_binding_digest=ancestor.runner_binding_digest,
                )
            )
        request = EffectRequest.build(
            effect_id=effect_id,
            public_run_id=binding.public_run_id,
            project_identity=binding.project_identity,
            definition_digest=binding.recipe_digest,
            coordinate=coordinate,
            descriptor_digest=descriptor.digest,
            effect_kind=descriptor.kind,
            runner_selector=descriptor.runner.selector,
            runner_binding_digest=runner.binding_digest,
            required_capabilities=descriptor.runner.required_capabilities,
            inputs=tuple(
                (name, snapshot.values[selector.state_key])
                for name, selector in descriptor.inputs
            ),
            writes=descriptor.writes,
            deadline_at=deadline_at,
            scope_bindings=tuple(scope_bindings),
        )
        return _Context(
            interrupt,
            descriptor,
            effect_id,
            deadline_at,
            None,
            request,
            runner,
            None,
        )

    def _identity(
        self,
        run_id: str,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        record: EffectRecord | None,
    ) -> tuple[EffectDescriptor | ScopeDescriptor, str]:
        coordinate = interrupt.coordinate
        if coordinate.thread_id != binding.thread_id:
            raise CoordinatorLineageError(
                "interrupt belongs to a foreign native thread"
            )
        if self._runtime.coordinate_lineage(run_id, coordinate) != "pending":
            raise CoordinatorLineageError(
                "effect source is not the exact current interrupt"
            )
        descriptor = parse_effect_descriptor(self._raw_descriptor(interrupt))
        effect_id = derive_effect_id(coordinate, descriptor.digest)
        if record is not None and (
            record.effect_id != effect_id
            or record.coordinate != coordinate
            or record.descriptor_digest != descriptor.digest
        ):
            raise CoordinatorLineageError(
                "ledger fact does not match the exact pending descriptor coordinate"
            )
        return descriptor, effect_id

    def _acquire(self, effect_id: str) -> Lease:
        return self._leases.acquire(
            "effect", effect_id, self._owner_factory(), self._lease_ttl
        )

    def _report(
        self, run_id: str, record: EffectRecord, action: str
    ) -> ReconcileReport:
        return ReconcileReport(run_id, record.effect_id, action, record.phase)

    @staticmethod
    def _timeout_result(effect_id: str) -> EffectResult:
        return parse_effect_result(
            {
                "schema": "lockstep.effect-result/v1",
                "effect_id": effect_id,
                "outcome": "ERROR",
                "result_ref": None,
                "artifact_refs": [],
                "snapshot_ref": None,
                "diff_ref": None,
                "fixed_error_code": "deadline_timeout",
                "evidence_refs": [],
            }
        )

    def _terminal_safety(
        self,
        context: _Context,
        safety: TerminalSafetyObservation,
        *,
        result: EffectResult | None,
    ) -> bool:
        assert context.request is not None
        assert isinstance(context.descriptor, EffectDescriptor)
        assert context.descriptor.runner is not None
        self._check_observation(context.request, safety)
        if safety.state == "pending":
            return False
        if safety.state != "proven":
            raise ProviderContractViolation("terminal-safety proof is incomplete")
        if (
            "result_stability" in context.descriptor.runner.required_capabilities
            and not safety.result_stable
        ):
            raise ProviderContractViolation("required result-stability proof is absent")
        if context.descriptor.kind == "managed":
            if safety.rollover_snapshot_ref is None:
                raise ProviderContractViolation(
                    "managed completion requires independent snapshot rollover"
                )
            if (
                result is not None
                and safety.rollover_snapshot_ref != result.snapshot_ref
            ):
                raise ProviderContractViolation(
                    "managed rollover does not match the sealed result snapshot"
                )
        return True

    def reconcile(self, run_id: str) -> ReconcileReport:
        binding = self._binding(run_id)
        snapshot = self._runtime.snapshot(run_id, subgraphs=True)
        records = [
            record
            for record in self._ledger.list_nonterminal()
            if record.coordinate.thread_id == binding.thread_id
        ]
        pending_by_coordinate = {
            interrupt.coordinate: interrupt for interrupt in self._protected(snapshot)
        }
        for missing in (
            item for item in records if item.coordinate not in pending_by_coordinate
        ):
            lineage = self._runtime.coordinate_lineage(run_id, missing.coordinate)
            if lineage == "descended" and missing.phase in {"sealed", "indeterminate"}:
                try:
                    lease = self._acquire(missing.effect_id)
                except LeaseUnavailable:
                    return self._report(run_id, missing, "busy")
                try:
                    delivered = self._ledger.mark_delivered(
                        missing.effect_id,
                        expected_revision=missing.revision,
                        lease=lease,
                    )
                finally:
                    self._leases.release(lease)
                return self._report(run_id, delivered, "delivered")
            raise CoordinatorLineageError(
                "nonterminal effect is absent from compatible native lineage"
            )

        records_by_coordinate = {item.coordinate: item for item in records}
        active = [
            item for item in records if item.phase not in {"sealed", "indeterminate"}
        ]
        if active:
            record = active[0]
            interrupt = pending_by_coordinate[record.coordinate]
        else:
            unrecorded = [
                interrupt
                for coordinate, interrupt in pending_by_coordinate.items()
                if coordinate not in records_by_coordinate
            ]
            if unrecorded:
                record = None
                interrupt = unrecorded[0]
            elif records:
                record = records[0]
                interrupt = pending_by_coordinate[record.coordinate]
            else:
                return ReconcileReport(run_id, None, "no_effect", None)

        descriptor, effect_id = self._identity(run_id, binding, interrupt, record)
        try:
            lease = self._acquire(effect_id)
        except LeaseUnavailable:
            return ReconcileReport(
                run_id,
                effect_id,
                "busy",
                None if record is None else record.phase,
            )
        try:
            if record is not None:
                current = self._ledger.get(record.effect_id)
                if (
                    current.revision != record.revision
                    or current.phase != record.phase
                    or not self._leases.is_current(lease)
                ):
                    return self._report(run_id, current, "busy")
                record = current
            else:
                try:
                    record = self._ledger.get(effect_id)
                except KeyError:
                    pass
            descriptor, checked_effect_id = self._identity(
                run_id, binding, interrupt, record
            )
            if checked_effect_id != effect_id or not self._leases.is_current(lease):
                return ReconcileReport(
                    run_id,
                    effect_id,
                    "busy",
                    None if record is None else record.phase,
                )
            context = self._context(
                binding,
                snapshot,
                interrupt,
                descriptor=descriptor,
                effect_id=effect_id,
                record=record,
            )
            if record is None:
                prepared = self._ledger.prepare(
                    context.interrupt.coordinate,
                    context.descriptor,
                    deadline_at=context.deadline_at,
                    runner_binding_digest=(
                        None
                        if context.request is None
                        else context.request.runner_binding_digest
                    )
                    if not isinstance(context.descriptor, ScopeDescriptor)
                    else (
                        None
                        if context.runner is None
                        else context.runner.binding_digest
                    ),
                    workspace_ref=None,
                    lease=lease,
                )
                return self._report(run_id, prepared, "prepared")

            if record.phase == "prepared":
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
                if context.request is None:
                    return self._report(run_id, record, "manual_pending")
                if record.deadline_at is not None and record.deadline_at <= self._now():
                    sealed = self._ledger.seal(
                        record.effect_id,
                        self._timeout_result(record.effect_id),
                        expected_revision=record.revision,
                        lease=lease,
                    )
                    return self._report(run_id, sealed, "sealed")
                assert context.runner is not None
                launch = context.runner.prepare(context.request)
                self._check_launch(context.request, launch)
                launching = self._ledger.mark_launching(
                    record.effect_id,
                    expected_revision=record.revision,
                    lease=lease,
                    runner_binding_digest=context.request.runner_binding_digest,
                    workspace_ref=launch.workspace_ref,
                )
                return self._report(run_id, launching, "launch_claimed")

            if record.phase == "launching":
                assert context.runner is not None
                assert context.request is not None
                if not self._leases.is_current(lease):
                    return self._report(run_id, record, "busy")
                expired = (
                    record.deadline_at is not None and record.deadline_at <= self._now()
                )
                if expired:
                    observation = context.runner.inspect(record.effect_id)
                else:
                    launch = context.runner.prepare(context.request)
                    self._check_launch(context.request, launch)
                    if launch.workspace_ref != record.workspace_ref:
                        raise ProviderContractViolation(
                            "prepared workspace differs from the durable effect intent"
                        )
                    current = self._ledger.get(record.effect_id)
                    if (
                        current.revision != record.revision
                        or current.phase != record.phase
                        or not self._leases.is_current(lease)
                    ):
                        return self._report(run_id, current, "busy")
                    observation = context.runner.ensure_started(launch)
                self._check_observation(context.request, observation)
                if observation.state == "absent" and expired:
                    sealed = self._ledger.seal(
                        record.effect_id,
                        self._timeout_result(record.effect_id),
                        expected_revision=record.revision,
                        lease=lease,
                        runner_binding_digest=context.request.runner_binding_digest,
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
                    runner_binding_digest=context.request.runner_binding_digest,
                )
                return self._report(run_id, running, "running")

            if record.phase == "running":
                assert context.runner is not None
                assert context.request is not None
                expired = (
                    record.deadline_at is not None and record.deadline_at <= self._now()
                )
                if expired:
                    if not self._leases.is_current(lease):
                        return self._report(run_id, record, "busy")
                    cancelled = context.runner.cancel(record.effect_id)
                    self._check_observation(context.request, cancelled)
                    safety = context.runner.quiesce(record.effect_id)
                    if not self._terminal_safety(context, safety, result=None):
                        return self._report(run_id, record, "quiescence_pending")
                    sealed = self._ledger.seal(
                        record.effect_id,
                        self._timeout_result(record.effect_id),
                        expected_revision=record.revision,
                        lease=lease,
                        runner_binding_digest=context.request.runner_binding_digest,
                    )
                    return self._report(run_id, sealed, "sealed")
                observation = context.runner.inspect(record.effect_id)
                self._check_observation(context.request, observation)
                if observation.state == "running":
                    return self._report(run_id, record, "running")
                if observation.state != "terminal":
                    raise ProviderContractViolation(
                        "provider result must be a closed ordinary EffectResult"
                    )
                result = self._closed_result(observation.result)
                if result.effect_id != record.effect_id:
                    raise ProviderContractViolation(
                        "provider result targets another effect"
                    )
                safety = context.runner.quiesce(record.effect_id)
                if not self._terminal_safety(context, safety, result=result):
                    return self._report(run_id, record, "quiescence_pending")
                sealed = self._ledger.seal(
                    record.effect_id,
                    result,
                    expected_revision=record.revision,
                    lease=lease,
                    runner_binding_digest=context.request.runner_binding_digest,
                )
                return self._report(run_id, sealed, "sealed")

            if record.phase in {"sealed", "indeterminate"}:
                return self._report(run_id, record, "awaiting_delivery")
            return self._report(run_id, record, "unchanged")
        except (StaleEffectLease, StaleEffectRevision):
            current = self._ledger.get(effect_id)
            return self._report(run_id, current, "busy")
        finally:
            self._leases.release(lease)

    def deliver_ready(
        self, run_id: str, interrupt_ids: Sequence[str] | None = None
    ) -> ScenarioStatus:
        binding = self._binding(run_id)
        snapshot = self._runtime.snapshot(run_id, subgraphs=True)
        requested = None
        if interrupt_ids is not None:
            if (
                not interrupt_ids
                or len(interrupt_ids) > self.MAX_DUE_PER_SCAN
                or any(not isinstance(item, str) or not item for item in interrupt_ids)
                or len(set(interrupt_ids)) != len(interrupt_ids)
            ):
                raise CoordinatorLineageError(
                    "requested interrupt selectors must be a bounded unique list"
                )
            requested = set(interrupt_ids)
            pending_protected_ids = {
                interrupt.coordinate.interrupt_id
                for interrupt in self._protected(snapshot)
            }
            unknown = requested - pending_protected_ids
            if unknown:
                raise CoordinatorLineageError(
                    f"requested interrupt is not an exact pending effect: {sorted(unknown)}"
                )
        deliverable: list[tuple[EffectRecord, NativeInterrupt]] = []
        for interrupt in self._protected(snapshot):
            if (
                requested is not None
                and interrupt.coordinate.interrupt_id not in requested
            ):
                continue
            descriptor = parse_effect_descriptor(self._raw_descriptor(interrupt))
            effect_id = derive_effect_id(interrupt.coordinate, descriptor.digest)
            try:
                record = self._ledger.get(effect_id)
            except KeyError:
                continue
            if (
                record.phase in {"sealed", "indeterminate"}
                and record.coordinate == interrupt.coordinate
                and record.descriptor_digest == descriptor.digest
                and record.result is not None
            ):
                deliverable.append((record, interrupt))
        if not deliverable:
            return project_status(binding, snapshot, self._leases, self._ledger)
        source = deliverable[0][1].coordinate
        results = {
            interrupt.coordinate.interrupt_id: record.result.to_dict()
            for record, interrupt in deliverable
        }
        committed = self._runtime.resume(run_id, source, results)
        if any(
            pending.coordinate.interrupt_id in results for pending in committed.pending
        ):
            raise CoordinatorLineageError(
                "native resume returned without consuming the exact delivered interrupts"
            )
        for stale_record, _interrupt in deliverable:
            if (
                self._runtime.coordinate_lineage(run_id, stale_record.coordinate)
                != "descended"
            ):
                raise CoordinatorLineageError(
                    "native commit does not descend from the delivered source interrupt"
                )
            current = self._ledger.get(stale_record.effect_id)
            lease = self._acquire(current.effect_id)
            try:
                self._ledger.mark_delivered(
                    current.effect_id,
                    expected_revision=current.revision,
                    lease=lease,
                )
            finally:
                self._leases.release(lease)
        return project_status(binding, committed, self._leases, self._ledger)

    def reconcile_due(self, now: datetime) -> tuple[ReconcileReport, ...]:
        reports = []
        seen_runs: set[str] = set()
        for record in self._ledger.list_due(now, limit=self.MAX_DUE_PER_SCAN):
            binding = self._catalog.find_by_thread(record.coordinate.thread_id)
            if binding.public_run_id in seen_runs:
                continue
            seen_runs.add(binding.public_run_id)
            reports.append(self.reconcile(binding.public_run_id))
        return tuple(reports)

    def next_wakeup_delay(self, now: datetime) -> float:
        deadline = self._ledger.next_deadline()
        if deadline is None:
            return 1.0
        current = now.astimezone(UTC)
        remaining = (deadline - current).total_seconds()
        return 1.0 if remaining <= 0 else min(1.0, remaining)

    def wait_and_reconcile_due(
        self, wakeup: Callable[[float], object]
    ) -> tuple[ReconcileReport, ...]:
        """Run one externally owned, deterministic deadline-wakeup cycle."""

        wakeup(self.next_wakeup_delay(self._now()))
        return self.reconcile_due(self._now())
