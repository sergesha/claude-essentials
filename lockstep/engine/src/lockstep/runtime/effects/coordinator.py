"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.effects.authority import (
    EffectAuthorityDenied,
    EffectAuthorityGate,
    EffectAuthorityUnavailable,
    EffectGrant,
)
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
    RuntimeInputSelector,
    ScopeDescriptor,
    ScopeResult,
)
from lockstep.runtime.graph_runtime import GraphRuntime
from lockstep.runtime.leases import Lease, LeaseStore, LeaseUnavailable
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot
from lockstep.runtime.providers.base import (
    DefinitiveProviderFailure,
    EffectRequest,
    PreparedLaunch,
    RunnerAdapter,
    RunnerObservation,
    ScopeBinding,
    TerminalSafetyObservation,
    launch_commitment_digest,
)
from lockstep.runtime.providers.manual import (
    ManualHandoff,
    ManualProvider,
    ManualProviderError,
    ManualSubmission,
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
    grant: EffectGrant | None


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
        authority: EffectAuthorityGate,
        manual: ManualProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        owner_factory: Callable[[], str] | None = None,
        lease_ttl: float = 30.0,
    ) -> None:
        self._runtime = runtime
        self._catalog = catalog
        self._ledger = ledger
        self._leases = leases
        self._runners = dict(runners)
        self._runner_bindings = {
            runner.binding_digest: runner for runner in self._runners.values()
        }
        self._authority = authority
        self._manual = manual
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

    def _ancestor_results(
        self,
        run_id: str,
        binding: RunBinding,
        descriptor: EffectDescriptor | ScopeDescriptor,
        snapshot: NativeSnapshot,
        consumer: NativeInterrupt,
    ) -> tuple[tuple[ScopeResult, ScopeBinding], ...]:
        consumer_values = (
            snapshot.values
            if consumer.state_values is None
            else consumer.state_values
        )
        keys = (
            descriptor.scope_state_keys
            if isinstance(descriptor, EffectDescriptor)
            else descriptor.ancestor_deadline_state_keys
        )
        results = []
        for key in keys:
            if key not in consumer_values:
                raise CoordinatorLineageError(
                    f"protected descriptor references absent graph state {key!r}"
                )
            result = parse_scope_result(consumer_values[key])
            try:
                producer = self._ledger.get(result.effect_id)
            except KeyError as exc:
                raise CoordinatorLineageError(
                    f"state {key!r} has no ledger-proven scope producer"
                ) from exc
            if (
                producer.effect_kind != "scope"
                or producer.phase != "delivered"
                or producer.coordinate.thread_id != binding.thread_id
                or producer.descriptor_digest != result.scope_digest
                or producer.result != result
            ):
                raise CoordinatorLineageError(
                    f"state {key!r} is not backed by a delivered scope producer"
                )
            proof = self._runtime.interrupt_lineage(run_id, producer.coordinate)
            if proof is None or proof.disposition != "descended":
                raise CoordinatorLineageError(
                    f"scope producer for state {key!r} lacks compatible native lineage"
                )
            if not self._runtime.checkpoint_is_ancestor(
                run_id, producer.coordinate, consumer
            ):
                raise CoordinatorLineageError(
                    f"scope producer for state {key!r} is not an ancestor"
                )
            producer_descriptor = parse_effect_descriptor(
                self._raw_descriptor(
                    NativeInterrupt(proof.occurrence.coordinate, proof.occurrence.value)
                )
            )
            if (
                not isinstance(producer_descriptor, ScopeDescriptor)
                or producer_descriptor.digest != producer.descriptor_digest
                or producer_descriptor.result_state_key != key
            ):
                raise CoordinatorLineageError(
                    f"scope producer does not own declared result state {key!r}"
                )
            result_json = json.dumps(
                result.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            results.append(
                (
                    result,
                    ScopeBinding(
                        state_key=key,
                        producer_effect_id=producer.effect_id,
                        producer_coordinate=producer.coordinate,
                        scope_digest=result.scope_digest,
                        scope_result_digest=hashlib.sha256(result_json).hexdigest(),
                        runner_binding_digest=result.runner_binding_digest,
                    ),
                )
            )
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
            runner = self._runners[selector]
        except KeyError as exc:
            raise ProviderContractViolation(
                f"no trusted runner is bound for selector {selector!r}"
            ) from exc
        self._check_reconciliation_boundary(runner)
        return runner

    @staticmethod
    def _check_reconciliation_boundary(runner: RunnerAdapter) -> None:
        if runner.reconciliation_boundary != "local_durable_handle":
            raise ProviderContractViolation(
                "runner reconciliation must use only a local durable handle"
            )

    def _runner_for_binding(self, binding_digest: str) -> RunnerAdapter:
        try:
            runner = self._runner_bindings[binding_digest]
        except KeyError as exc:
            raise ProviderContractViolation(
                "durably bound runner is unavailable for recovery"
            ) from exc
        self._check_reconciliation_boundary(runner)
        return runner

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
        if launch.workspace_ref != request.workspace_ref:
            raise ProviderContractViolation(
                "prepared launch workspace differs from the exact effect grant"
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
        binding: EffectRequest | EffectRecord,
        observation: RunnerObservation | TerminalSafetyObservation,
    ) -> None:
        if (
            observation.effect_id != binding.effect_id
            or observation.request_digest != binding.request_digest
            or observation.runner_binding_digest != binding.runner_binding_digest
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
            observation.result_stable
            or observation.rollover_snapshot_ref is not None
            or observation.workspace_quarantined
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
        resolve_grant: bool,
    ) -> _Context:
        coordinate = interrupt.coordinate
        if isinstance(descriptor, EffectDescriptor) and any(
            isinstance(selector, RuntimeInputSelector)
            for _name, selector in descriptor.inputs
        ):
            raise ProviderContractViolation(
                "runtime snapshot selectors require the dedicated durable snapshot resolver"
            )
        now = self._now()
        verified_ancestors = self._ancestor_results(
            binding.public_run_id, binding, descriptor, snapshot, interrupt
        )
        ancestors = tuple(result for result, _scope in verified_ancestors)
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
                interrupt=interrupt,
                descriptor=descriptor,
                effect_id=effect_id,
                deadline_at=deadline_at,
                scope_result=scope_result,
                request=None,
                runner=runner,
                grant=None,
            )

        if descriptor.runner is None:
            # Manual work has no external runner. Task 7 owns its session/result path.
            return _Context(
                interrupt=interrupt,
                descriptor=descriptor,
                effect_id=effect_id,
                deadline_at=None,
                scope_result=None,
                request=None,
                runner=None,
                grant=None,
            )
        if descriptor.artifacts:
            # Task 10 owns ArtifactRegistry and the provider-neutral artifact
            # contract. Until that boundary exists, never erase descriptor
            # requirements while constructing an EffectRequest.
            raise ProviderContractViolation(
                "artifact-bearing effects require the ArtifactRegistry boundary"
            )
        runner = (
            self._runner_for(descriptor.runner.selector)
            if record is None
            else self._runner_for_binding(record.runner_binding_digest)
        )
        scope_bindings = []
        for ancestor, scope_binding in verified_ancestors:
            if ancestor.scope_kind == "call" and (
                ancestor.runner_selector != descriptor.runner.selector
                or ancestor.runner_binding_digest != runner.binding_digest
            ):
                raise ProviderContractViolation(
                    "call scope runner binding does not match the selected adapter"
                )
            scope_bindings.append(scope_binding)
        intent = EffectRequest.build(
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
                (
                    name,
                    (
                        snapshot.values
                        if interrupt.state_values is None
                        else interrupt.state_values
                    )[selector.state_key],
                )
                for name, selector in descriptor.inputs
            ),
            writes=descriptor.writes,
            deadline_at=deadline_at,
            scope_bindings=tuple(scope_bindings),
        )
        if not resolve_grant or (deadline_at is not None and deadline_at <= now):
            return _Context(
                interrupt=interrupt,
                descriptor=descriptor,
                effect_id=effect_id,
                deadline_at=deadline_at,
                scope_result=None,
                request=None,
                runner=runner,
                grant=None,
            )
        grant = self._authority.resolve(intent)
        if grant.required_authorities != runner.required_authorities:
            raise ProviderContractViolation(
                "effect grant authorities differ from trusted runner requirements"
            )
        request = intent.bind_grant(grant)
        if record is not None and (
            record.request_digest != request.request_digest
            or record.grant_digest != grant.digest
            or record.workspace_ref != grant.workspace_ref
        ):
            raise CoordinatorLineageError(
                "current request or grant differs from durable effect intent"
            )
        return _Context(
            interrupt=interrupt,
            descriptor=descriptor,
            effect_id=effect_id,
            deadline_at=deadline_at,
            scope_result=None,
            request=request,
            runner=runner,
            grant=grant,
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
        descriptor = parse_effect_descriptor(self._raw_descriptor(interrupt))
        if not isinstance(descriptor, (EffectDescriptor, ScopeDescriptor)):
            raise ProviderContractViolation(
                f"{descriptor.kind} execution requires its dedicated trusted runtime boundary"
            )
        if self._protected_lineage(run_id, coordinate, descriptor.digest) != "pending":
            raise CoordinatorLineageError(
                "effect source is not the exact current interrupt"
            )
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

    def _protected_lineage(
        self, run_id: str, coordinate, descriptor_digest: str
    ) -> str:
        proof = self._runtime.interrupt_lineage(run_id, coordinate)
        if proof is None:
            return "incompatible"
        try:
            descriptor = parse_effect_descriptor(
                self._raw_descriptor(
                    NativeInterrupt(proof.occurrence.coordinate, proof.occurrence.value)
                )
            )
        except (TypeError, ValueError) as exc:
            raise CoordinatorLineageError(
                "native lineage occurrence is not the protected effect source"
            ) from exc
        if descriptor.digest != descriptor_digest:
            raise CoordinatorLineageError(
                "native lineage descriptor differs from durable effect source"
            )
        return proof.disposition

    def _acquire(self, effect_id: str) -> Lease:
        return self._leases.acquire(
            "effect", effect_id, self._owner_factory(), self._lease_ttl
        )

    def _report(
        self, run_id: str, record: EffectRecord, action: str
    ) -> ReconcileReport:
        return ReconcileReport(run_id, record.effect_id, action, record.phase)

    def _manual_handoff(
        self,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: EffectDescriptor,
    ) -> ManualHandoff:
        if self._manual is None:
            raise ProviderContractViolation("manual provider is unavailable")
        if descriptor.kind != "manual" or descriptor.runner is not None:
            raise ProviderContractViolation("manual handoff requires a manual effect")
        try:
            handoff = self._manual.prepare_handoff(binding, interrupt, descriptor)
        except ManualProviderError as exc:
            raise ProviderContractViolation(str(exc)) from exc
        if (
            handoff.effect_id
            != derive_effect_id(interrupt.coordinate, descriptor.digest)
            or handoff.public_run_id != binding.public_run_id
            or handoff.project_identity != binding.project_identity
            or handoff.coordinate != interrupt.coordinate
            or handoff.descriptor_digest != descriptor.digest
            or handoff.writes != descriptor.writes
        ):
            raise ProviderContractViolation(
                "manual handoff differs from the exact protected interrupt"
            )
        return handoff

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
        binding: EffectRequest | EffectRecord,
    ) -> bool:
        assert isinstance(context.descriptor, EffectDescriptor)
        assert context.descriptor.runner is not None
        self._check_observation(binding, safety)
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
            if result is not None and result.snapshot_ref is None:
                if result.outcome != "ERROR":
                    raise ProviderContractViolation(
                        "managed PASS/FAIL requires an exact rollover snapshot"
                    )
                if (
                    safety.rollover_snapshot_ref is None
                    and not safety.workspace_quarantined
                ):
                    raise ProviderContractViolation(
                        "managed error requires rollover or quarantine proof"
                    )
            elif safety.rollover_snapshot_ref is None:
                raise ProviderContractViolation(
                    "managed completion requires independent snapshot rollover"
                )
            if (
                result is not None
                and result.snapshot_ref is not None
                and safety.rollover_snapshot_ref != result.snapshot_ref
            ):
                raise ProviderContractViolation(
                    "managed rollover does not match the sealed result snapshot"
                )
        return True

    def reconcile(self, run_id: str) -> ReconcileReport:
        binding = self._binding(run_id)
        snapshot = self._runtime.snapshot(run_id, subgraphs=True)
        records = self._ledger.list_nonterminal_for_thread(
            binding.thread_id, limit=self.MAX_DUE_PER_SCAN + 1
        )
        if len(records) > self.MAX_DUE_PER_SCAN:
            raise CoordinatorLineageError(
                "run exceeds the bounded nonterminal effect capacity"
            )
        pending_by_coordinate = {
            interrupt.coordinate: interrupt for interrupt in self._protected(snapshot)
        }
        for missing in (
            item for item in records if item.coordinate not in pending_by_coordinate
        ):
            lineage = self._protected_lineage(
                run_id, missing.coordinate, missing.descriptor_digest
            )
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
            try:
                context = self._context(
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
                runner = self._runner_for_binding(record.runner_binding_digest)
                observation = runner.inspect(record.effect_id)
                self._check_observation(record, observation)
                if observation.state == "absent":
                    return self._report(run_id, record, "authority_blocked")
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
            if record is None:
                if (
                    isinstance(context.descriptor, EffectDescriptor)
                    and context.descriptor.kind == "manual"
                ):
                    self._manual_handoff(binding, context.interrupt, context.descriptor)
                prepared = self._ledger.prepare(
                    context.interrupt.coordinate,
                    context.descriptor,
                    deadline_at=context.deadline_at,
                    runner_binding_digest=(
                        None
                        if context.runner is None
                        else context.runner.binding_digest
                    ),
                    workspace_ref=(
                        None if context.grant is None else context.grant.workspace_ref
                    ),
                    request_digest=(
                        None
                        if context.request is None
                        else context.request.request_digest
                    ),
                    grant_digest=(
                        None if context.grant is None else context.grant.digest
                    ),
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
                    self._manual_handoff(binding, context.interrupt, context.descriptor)
                    return self._report(run_id, record, "manual_pending")
                assert context.runner is not None
                try:
                    launch = context.runner.prepare(context.request)
                except DefinitiveProviderFailure as failure:
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
                    sealed = self._ledger.seal(
                        record.effect_id,
                        result,
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

            if record.phase == "launching":
                assert context.runner is not None
                if not self._leases.is_current(lease):
                    return self._report(run_id, record, "busy")
                expired = (
                    record.deadline_at is not None and record.deadline_at <= self._now()
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
                        assert guarded_context.grant is not None
                        with self._authority.commitment(
                            guarded_context.grant,
                            guarded_context.request,
                            launch,
                        ):
                            observation = context.runner.ensure_started(launch)
                self._check_observation(
                    context.request if context.request is not None else record,
                    observation,
                )
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

            if record.phase == "running":
                assert context.runner is not None
                expired = (
                    record.deadline_at is not None and record.deadline_at <= self._now()
                )
                if expired:
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
                    raise ProviderContractViolation(
                        "provider result targets another effect"
                    )
                safety = context.runner.quiesce(record.effect_id)
                if not self._terminal_safety(
                    context, safety, result=result, binding=record
                ):
                    return self._report(run_id, record, "quiescence_pending")
                sealed = self._ledger.seal(
                    record.effect_id,
                    result,
                    expected_revision=record.revision,
                    lease=lease,
                    runner_binding_digest=record.runner_binding_digest,
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

    def submit_manual(
        self,
        run_id: str,
        source,
        submission: ManualSubmission,
    ) -> ScenarioStatus:
        """Seal one protected manual result, then use ordinary native delivery."""

        if not isinstance(submission, ManualSubmission):
            raise ProviderContractViolation("closed ManualSubmission is required")
        binding = self._binding(run_id)
        snapshot = self._runtime.snapshot(run_id, subgraphs=True)
        matches = tuple(
            interrupt
            for interrupt in self._protected(snapshot)
            if interrupt.coordinate == source
        )
        if len(matches) != 1:
            raise CoordinatorLineageError(
                "manual source is not the exact current protected interrupt"
            )
        interrupt = matches[0]
        descriptor, effect_id = self._identity(run_id, binding, interrupt, None)
        if not isinstance(descriptor, EffectDescriptor) or descriptor.kind != "manual":
            raise ProviderContractViolation(
                "worker submission targets an engine effect"
            )
        try:
            record = self._ledger.get(effect_id)
        except KeyError as exc:
            raise CoordinatorLineageError(
                "manual handoff was not prepared before worker submission"
            ) from exc
        self._identity(run_id, binding, interrupt, record)
        if record.phase != "prepared":
            raise CoordinatorLineageError("manual effect is not awaiting one result")
        lease = self._acquire(effect_id)
        try:
            with self._runtime.commitment_guard(run_id, source) as guarded:
                if guarded.binding != binding or guarded.interrupt.coordinate != source:
                    raise CoordinatorLineageError(
                        "manual source changed before result commitment"
                    )
                guarded_descriptor = parse_effect_descriptor(
                    self._raw_descriptor(guarded.interrupt)
                )
                if (
                    not isinstance(guarded_descriptor, EffectDescriptor)
                    or guarded_descriptor.kind != "manual"
                    or guarded_descriptor.digest != record.descriptor_digest
                ):
                    raise CoordinatorLineageError(
                        "manual descriptor changed before result commitment"
                    )
                handoff = self._manual_handoff(
                    binding, guarded.interrupt, guarded_descriptor
                )
                current = self._ledger.get(effect_id)
                if (
                    current.revision != record.revision
                    or current.phase != "prepared"
                    or not self._leases.is_current(lease)
                ):
                    raise StaleEffectRevision(
                        "manual effect changed before result commitment"
                    )
                assert self._manual is not None
                result = self._closed_result(self._manual.complete(handoff, submission))
                if result.effect_id != effect_id:
                    raise ProviderContractViolation(
                        "manual result targets another effect"
                    )
                self._ledger.seal(
                    effect_id,
                    result,
                    expected_revision=current.revision,
                    lease=lease,
                )
        finally:
            self._leases.release(lease)
        return self.deliver_ready(run_id, [source.interrupt_id])

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
        held: dict[str, Lease] = {}
        current_deliverable: list[tuple[EffectRecord, NativeInterrupt]] = []
        try:
            for stale_record, interrupt in sorted(
                deliverable, key=lambda item: item[0].effect_id
            ):
                try:
                    held[stale_record.effect_id] = self._acquire(stale_record.effect_id)
                except LeaseUnavailable:
                    current_snapshot = self._runtime.snapshot(run_id, subgraphs=True)
                    return project_status(
                        binding, current_snapshot, self._leases, self._ledger
                    )
                current = self._ledger.get(stale_record.effect_id)
                if (
                    current.revision != stale_record.revision
                    or current.phase not in {"sealed", "indeterminate"}
                    or current.coordinate != interrupt.coordinate
                    or current.descriptor_digest != stale_record.descriptor_digest
                    or current.result is None
                    or not self._leases.is_current(held[current.effect_id])
                ):
                    current_snapshot = self._runtime.snapshot(run_id, subgraphs=True)
                    return project_status(
                        binding, current_snapshot, self._leases, self._ledger
                    )
                current_deliverable.append((current, interrupt))

            source = current_deliverable[0][1].coordinate
            results = {
                interrupt.coordinate.interrupt_id: record.result.to_dict()
                for record, interrupt in current_deliverable
            }
            committed = self._runtime.resume(run_id, source, results)
            if any(
                pending.coordinate.interrupt_id in results
                for pending in committed.pending
            ):
                raise CoordinatorLineageError(
                    "native resume returned without consuming the exact delivered interrupts"
                )
            for current, _interrupt in current_deliverable:
                if (
                    self._protected_lineage(
                        run_id,
                        current.coordinate,
                        current.descriptor_digest,
                    )
                    != "descended"
                ):
                    raise CoordinatorLineageError(
                        "native commit does not descend from the delivered source interrupt"
                    )
                self._ledger.mark_delivered(
                    current.effect_id,
                    expected_revision=current.revision,
                    lease=held[current.effect_id],
                )
        finally:
            for lease in reversed(tuple(held.values())):
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
