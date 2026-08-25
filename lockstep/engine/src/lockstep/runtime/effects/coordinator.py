"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.artifacts import ArtifactDeclaration, ArtifactRegistry
from lockstep.runtime.effects.authority import (
    EffectAuthorityDenied,
    EffectAuthorityGate,
    EffectAuthorityUnavailable,
    EffectGrant,
)
from lockstep.runtime.effects.descriptors import (
    build_scope_result,
    derive_effect_id,
    parse_decision_result,
    parse_effect_descriptor,
    parse_effect_result,
    parse_scope_result,
    parse_acceptance_result,
)
from lockstep.runtime.effects.ledger import (
    EffectLedger,
    EffectRecord,
    StaleEffectLease,
    StaleEffectRevision,
)
from lockstep.runtime.effects.owner_consent import (
    IssuedPublicationConsent,
    OwnerConsentAuthority,
    PublicationConsentCommitment,
)
from lockstep.runtime.effects.models import (
    AcceptDescriptor,
    AcceptanceResult,
    DecisionDescriptor,
    EffectDescriptor,
    EffectResult,
    RuntimeInputSelector,
    ScopeDescriptor,
    ScopeResult,
    PublishDescriptor,
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
from lockstep.runtime.project_snapshots import ProjectSnapshotRef
from lockstep.runtime.snapshot_resolver import RuntimeSnapshotResolver
from lockstep.runtime.publication import (
    ProjectPublisher,
    PublicationEntry,
    PublicationRequest,
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
        artifacts: ArtifactRegistry | None = None,
        publisher: ProjectPublisher | None = None,
        publisher_for: Callable[[RunBinding], ProjectPublisher] | None = None,
        manual: ManualProvider | None = None,
        snapshot_resolver: RuntimeSnapshotResolver | None = None,
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
        self._artifacts = artifacts
        self._publisher = publisher
        self._publisher_resolver = publisher_for
        self._manual = manual
        self._snapshot_resolver = snapshot_resolver
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner_factory = owner_factory or (lambda: secrets.token_hex(16))
        self._lease_ttl = lease_ttl

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coordinator clock must include a timezone")
        return value.astimezone(UTC)

    def _publisher_for(self, binding: RunBinding) -> ProjectPublisher:
        publisher = (
            self._publisher_resolver(binding)
            if self._publisher_resolver is not None
            else self._publisher
        )
        if not isinstance(publisher, ProjectPublisher):
            raise ProviderContractViolation(
                "publication requires a project-resolved ProjectPublisher"
            )
        if (
            self._publisher_resolver is not None
            and publisher.project_identity != binding.project_identity
        ):
            raise ProviderContractViolation(
                "publisher root differs from the run project identity"
            )
        return publisher

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
            if self._snapshot_resolver is None:
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
        runner = (
            self._runner_for(descriptor.runner.selector)
            if record is None
            else self._runner_for_binding(record.runner_binding_digest)
        )
        scope_bindings = []
        for ancestor, scope_binding in verified_ancestors:
            if ancestor.scope_kind == "call" and descriptor.kind == "managed" and (
                ancestor.runner_selector != descriptor.runner.selector
                or ancestor.runner_binding_digest != runner.binding_digest
            ):
                raise ProviderContractViolation(
                    "call scope runner binding does not match the selected adapter"
                )
            scope_bindings.append(scope_binding)
        runtime_inputs = (
            {}
            if self._snapshot_resolver is None
            else self._snapshot_resolver.inputs_for(
                binding, interrupt, descriptor, effect_id
            )
        )
        state_values = (
            snapshot.values if interrupt.state_values is None else interrupt.state_values
        )
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
                    runtime_inputs[name]
                    if isinstance(selector, RuntimeInputSelector)
                    else state_values[selector.state_key],
                )
                for name, selector in descriptor.inputs
            ),
            writes=descriptor.writes,
            artifacts=descriptor.artifacts,
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
    ) -> tuple[
        EffectDescriptor | ScopeDescriptor | DecisionDescriptor | AcceptDescriptor | PublishDescriptor,
        str,
    ]:
        coordinate = interrupt.coordinate
        if coordinate.thread_id != binding.thread_id:
            raise CoordinatorLineageError(
                "interrupt belongs to a foreign native thread"
            )
        descriptor = parse_effect_descriptor(self._raw_descriptor(interrupt))
        if not isinstance(
            descriptor,
            (
                EffectDescriptor,
                ScopeDescriptor,
                DecisionDescriptor,
                AcceptDescriptor,
                PublishDescriptor,
            ),
        ):
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

    def _reconcile_decision(
        self,
        run_id: str,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: DecisionDescriptor,
        effect_id: str,
        record: EffectRecord | None,
    ) -> ReconcileReport:
        """Evaluate a closed trusted decision without a runner or effect row."""

        if record is not None:
            raise CoordinatorLineageError(
                "trusted decision unexpectedly collides with an external-effect row"
            )
        if self._snapshot_resolver is None:
            raise ProviderContractViolation(
                "decision execution requires the durable runtime snapshot resolver"
            )
        with self._runtime.commitment_guard(run_id, interrupt.coordinate) as guarded:
            guarded_descriptor = parse_effect_descriptor(
                self._raw_descriptor(guarded.interrupt)
            )
            if (
                guarded.binding != binding
                or guarded.interrupt.coordinate != interrupt.coordinate
                or guarded_descriptor != descriptor
            ):
                raise CoordinatorLineageError(
                    "decision source changed before trusted evaluation"
                )
            result = self._snapshot_resolver.decide(
                binding, guarded.interrupt, descriptor, effect_id
            )
            parsed = parse_decision_result(result.to_dict(), descriptor=descriptor)
            if parsed != result or parsed.effect_id != effect_id:
                raise ProviderContractViolation("trusted decision result is not closed")
        committed = self._runtime.resume(
            run_id,
            interrupt.coordinate,
            {interrupt.coordinate.interrupt_id: result.to_dict()},
        )
        if any(
            item.coordinate.interrupt_id == interrupt.coordinate.interrupt_id
            for item in committed.pending
        ):
            raise CoordinatorLineageError(
                "native decision resume did not consume the exact interrupt"
            )
        return ReconcileReport(run_id, effect_id, "delivered", None)

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

    def _admit_artifacts(
        self,
        binding: RunBinding,
        context: _Context,
        record: EffectRecord,
        result: EffectResult,
        safety: TerminalSafetyObservation,
    ) -> EffectResult:
        if result.artifact_refs:
            raise ProviderContractViolation(
                "providers may not supply immutable artifact references"
            )
        assert isinstance(context.descriptor, EffectDescriptor)
        declarations = context.descriptor.artifacts
        if result.outcome != "PASS" or not declarations:
            return result
        if context.descriptor.kind != "managed":
            raise ProviderContractViolation(
                "artifact admission requires a managed rollover snapshot"
            )
        if self._artifacts is None:
            raise ProviderContractViolation(
                "artifact-bearing effects require an ArtifactRegistry"
            )
        if (
            result.snapshot_ref is None
            or result.snapshot_ref != safety.rollover_snapshot_ref
            or not result.snapshot_ref.startswith("snapshot:")
            or record.request_digest is None
            or record.workspace_ref is None
        ):
            raise ProviderContractViolation(
                "artifact admission lacks the exact producer rollover binding"
            )
        try:
            snapshot_ref = ProjectSnapshotRef(
                result.snapshot_ref.removeprefix("snapshot:")
            )
            refs = self._artifacts.register_set(
                public_run_id=binding.public_run_id,
                project_identity=binding.project_identity,
                definition_digest=binding.recipe_digest,
                producer_effect_id=record.effect_id,
                producer_request_digest=record.request_digest,
                workspace_ref=record.workspace_ref,
                producer_coordinate=record.coordinate,
                descriptor_digest=record.descriptor_digest,
                snapshot_ref=snapshot_ref,
                declarations=tuple(
                    ArtifactDeclaration(
                        item.name,
                        item.source_path,
                        item.media_type,
                        item.required,
                    )
                    for item in declarations
                ),
            )
            data = result.to_dict()
            data["artifact_refs"] = [str(ref) for ref in refs]
            return parse_effect_result(data)
        except ProviderContractViolation:
            raise
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise ProviderContractViolation(
                "artifact admission failed exact provenance validation"
            ) from exc

    @staticmethod
    def _interrupt_values(
        snapshot: NativeSnapshot, interrupt: NativeInterrupt
    ) -> Mapping[str, object]:
        return snapshot.values if interrupt.state_values is None else interrupt.state_values

    def _publication_intent(
        self,
        binding: RunBinding,
        snapshot: NativeSnapshot,
        interrupt: NativeInterrupt,
        descriptor: PublishDescriptor,
        effect_id: str,
        publisher: ProjectPublisher,
    ) -> tuple[EffectRequest, EffectGrant, PublicationRequest]:
        if self._artifacts is None:
            raise ProviderContractViolation(
                "publication requires ArtifactRegistry and ProjectPublisher ports"
            )
        values = self._interrupt_values(snapshot, interrupt)
        entries: list[PublicationEntry] = []
        intent_inputs: list[tuple[str, object]] = []
        approval_generation: int | None = None
        consent_refs: list[str] = []
        for ordinal, item in enumerate(descriptor.items):
            try:
                producer_result = parse_effect_result(
                    values[item.producer_result_state_key]
                )
                acceptance = parse_acceptance_result(
                    values[item.acceptance_result_state_key]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise CoordinatorLineageError(
                    "publication selectors lack closed delivered producer/consent results"
                ) from exc
            try:
                producer_record = self._ledger.get(producer_result.effect_id)
                acceptance_record = self._ledger.get(acceptance.effect_id)
            except KeyError as exc:
                raise CoordinatorLineageError(
                    "publication state lacks ledger-proven producers"
                ) from exc
            if (
                producer_record.phase != "delivered"
                or producer_record.result != producer_result
                or acceptance_record.phase != "delivered"
                or acceptance_record.result != acceptance
                or not self._runtime.checkpoint_is_ancestor(
                    binding.public_run_id, producer_record.coordinate, interrupt
                )
                or not self._runtime.checkpoint_is_ancestor(
                    binding.public_run_id, acceptance_record.coordinate, interrupt
                )
            ):
                raise CoordinatorLineageError(
                    "publication inputs are not exact delivered ancestors"
                )
            candidates = []
            for raw_ref in producer_result.artifact_refs:
                artifact = self._artifacts.read(raw_ref)
                if artifact.declared_name == item.declared_name:
                    candidates.append(artifact)
            if len(candidates) != 1:
                raise ProviderContractViolation(
                    "publication requires one exact declared artifact reference"
                )
            artifact = candidates[0]
            if (
                artifact.public_run_id != binding.public_run_id
                or artifact.project_identity != binding.project_identity
                or artifact.definition_digest != binding.recipe_digest
                or artifact.producer_effect_id != producer_record.effect_id
                or artifact.producer_coordinate != producer_record.coordinate
                or str(artifact.ref) != acceptance.artifact_ref
                or artifact.blob.sha256 != acceptance.artifact_digest
                or acceptance.destination != item.destination
                or acceptance.transformation != item.transformation
                or acceptance.audience != item.audience
            ):
                raise ProviderContractViolation(
                    "publication artifact and consent provenance differ"
                )
            if approval_generation is None:
                approval_generation = acceptance.approval_generation
            elif approval_generation != acceptance.approval_generation:
                raise ProviderContractViolation(
                    "publication items require one approval generation"
                )
            consent_refs.append(acceptance.consent_ref)
            entries.append(
                PublicationEntry(
                    artifact.ref,
                    item.destination,
                    item.transformation,
                )
            )
            intent_inputs.append(
                (
                    f"item-{ordinal}",
                    {
                        "artifact_ref": str(artifact.ref),
                        "artifact_blob": {
                            "sha256": artifact.blob.sha256,
                            "size": artifact.blob.size,
                        },
                        "destination": item.destination,
                        "transformation": item.transformation,
                        "audience": item.audience,
                        "consent_ref": acceptance.consent_ref,
                        "approval_generation": acceptance.approval_generation,
                        "receipt_digest": acceptance.receipt_digest,
                    },
                )
            )
        assert approval_generation is not None
        intent = EffectRequest.build(
            effect_id=effect_id,
            public_run_id=binding.public_run_id,
            project_identity=binding.project_identity,
            definition_digest=binding.recipe_digest,
            coordinate=interrupt.coordinate,
            descriptor_digest=descriptor.digest,
            effect_kind="publish",
            runner_selector="project-publisher",
            runner_binding_digest=publisher.binding_digest,
            required_capabilities=("publication",),
            inputs=tuple(intent_inputs),
            writes=tuple(item.destination for item in descriptor.items),
            deadline_at=None,
        )
        grant = self._authority.resolve(intent)
        if (
            grant.required_authorities != publisher.required_authorities
            or grant.workspace_ref is not None
            or grant.approval_generation != approval_generation
        ):
            raise ProviderContractViolation(
                "publication grant differs from publisher/consent authority"
            )
        request = intent.bind_grant(grant)
        publication_request = PublicationRequest.build(
            effect_id=effect_id,
            public_run_id=binding.public_run_id,
            project_identity=binding.project_identity,
            definition_digest=binding.recipe_digest,
            coordinate=interrupt.coordinate,
            descriptor_digest=descriptor.digest,
            authority_request_digest=request.request_digest,
            grant_digest=grant.digest,
            publisher_binding_digest=publisher.binding_digest,
            consent_ref="consent-set:" + hashlib.sha256(
                json.dumps(consent_refs, separators=(",", ":")).encode()
            ).hexdigest(),
            approval_generation=approval_generation,
            policy_epoch=grant.policy_epoch,
            config_epoch=grant.config_epoch,
            parent_capability_generation=grant.parent_capability_generation,
            entries=tuple(entries),
        )
        return request, grant, publication_request

    @staticmethod
    def _publication_result(effect_id: str, journal_digest: str) -> EffectResult:
        return parse_effect_result(
            {
                "schema": "lockstep.effect-result/v1",
                "effect_id": effect_id,
                "outcome": "PASS",
                "result_ref": f"publication:{journal_digest}",
                "artifact_refs": [],
                "snapshot_ref": None,
                "diff_ref": None,
                "fixed_error_code": None,
                "evidence_refs": [],
            }
        )

    @staticmethod
    def _publication_error_result(
        effect_id: str, journal_digest: str
    ) -> EffectResult:
        return parse_effect_result(
            {
                "schema": "lockstep.effect-result/v1",
                "effect_id": effect_id,
                "outcome": "ERROR",
                "result_ref": f"publication:{journal_digest}",
                "artifact_refs": [],
                "snapshot_ref": None,
                "diff_ref": None,
                "fixed_error_code": "provider_error",
                "evidence_refs": [],
            }
        )

    def _reconcile_acceptance(
        self,
        run_id: str,
        descriptor: AcceptDescriptor,
        interrupt: NativeInterrupt,
        effect_id: str,
        record: EffectRecord | None,
        lease: Lease,
    ) -> ReconcileReport:
        if record is None:
            prepared = self._ledger.prepare(
                interrupt.coordinate,
                descriptor,
                deadline_at=None,
                runner_binding_digest=None,
                workspace_ref=None,
                lease=lease,
            )
            return self._report(run_id, prepared, "prepared")
        if record.phase == "prepared":
            return self._report(run_id, record, "acceptance_pending")
        if record.phase in {"sealed", "indeterminate"}:
            return self._report(run_id, record, "awaiting_delivery")
        raise CoordinatorLineageError("acceptance has an impossible ledger phase")

    def _reconcile_publication(
        self,
        run_id: str,
        binding: RunBinding,
        snapshot: NativeSnapshot,
        descriptor: PublishDescriptor,
        interrupt: NativeInterrupt,
        effect_id: str,
        record: EffectRecord | None,
        lease: Lease,
    ) -> ReconcileReport:
        publisher = self._publisher_for(binding)
        if record is not None and record.phase in {"sealed", "indeterminate"}:
            return self._report(run_id, record, "awaiting_delivery")
        if record is not None and record.phase == "launching":
            recovering = publisher.prepared_for(
                record.effect_id, record.request_digest or ""
            )
            if recovering is not None and recovering[1] in {
                "applying", "applied", "rollback_pending", "rolled_back"
            }:
                prepared_publication, recovery_phase = recovering
                if (
                    record.launch_commitment_digest
                    != publisher.commitment_digest(prepared_publication)
                ):
                    raise CoordinatorLineageError(
                        "recovery journal differs from durable publication commitment"
                    )
                try:
                    publication_lease = self._leases.acquire(
                        "publication",
                        binding.project_identity,
                        self._owner_factory(),
                        self._lease_ttl,
                    )
                except LeaseUnavailable:
                    return self._report(run_id, record, "busy")
                try:
                    with self._runtime.commitment_guard(
                        run_id, record.coordinate
                    ) as guarded:
                        guarded_descriptor = parse_effect_descriptor(
                            self._raw_descriptor(guarded.interrupt)
                        )
                        current = self._ledger.get(record.effect_id)
                        if (
                            guarded.binding != binding
                            or guarded.interrupt.coordinate != record.coordinate
                            or guarded_descriptor != descriptor
                            or current.revision != record.revision
                            or current.phase != "launching"
                            or not self._leases.is_current(lease)
                            or not self._leases.is_current(publication_lease)
                        ):
                            return self._report(run_id, current, "busy")
                        if recovery_phase in {"rollback_pending", "rolled_back"}:
                            receipt = publisher.rollback_or_recover(
                                prepared_publication
                            )
                            result = self._publication_error_result(
                                record.effect_id, receipt.journal_digest
                            )
                        else:
                            receipt = publisher.apply_or_recover(
                                prepared_publication
                            )
                            result = self._publication_result(
                                record.effect_id, receipt.journal_digest
                            )
                    if receipt.phase not in {"applied", "rolled_back"}:
                        return self._report(
                            run_id, record, "publication_progress"
                        )
                    if receipt.phase == "applied":
                        if self._snapshot_resolver is not None:
                            self._snapshot_resolver.capture_successor(
                                binding,
                                interrupt,
                                descriptor,
                                effect_id,
                                purpose="publication",
                            )
                    sealed = self._ledger.seal(
                        record.effect_id,
                        result,
                        expected_revision=record.revision,
                        lease=lease,
                        runner_binding_digest=publisher.binding_digest,
                    )
                    return self._report(run_id, sealed, "sealed")
                finally:
                    self._leases.release(publication_lease)
        request, grant, publication_request = self._publication_intent(
            binding, snapshot, interrupt, descriptor, effect_id, publisher
        )
        if record is None:
            prepared = self._ledger.prepare(
                interrupt.coordinate,
                descriptor,
                deadline_at=None,
                runner_binding_digest=publisher.binding_digest,
                workspace_ref=None,
                request_digest=request.request_digest,
                grant_digest=grant.digest,
                lease=lease,
            )
            return self._report(run_id, prepared, "prepared")
        if (
            record.request_digest != request.request_digest
            or record.grant_digest != grant.digest
            or record.runner_binding_digest != publisher.binding_digest
        ):
            raise CoordinatorLineageError(
                "publication authority differs from durable ledger facts"
            )
        prepared_publication = publisher.prepare(publication_request)
        commitment_digest = publisher.commitment_digest(
            prepared_publication
        )
        if record.phase == "prepared":
            claimed = self._ledger.mark_launching(
                record.effect_id,
                expected_revision=record.revision,
                lease=lease,
                runner_binding_digest=publisher.binding_digest,
                launch_commitment_digest=commitment_digest,
            )
            return self._report(run_id, claimed, "publication_claimed")
        if record.phase == "launching":
            if record.launch_commitment_digest != commitment_digest:
                raise CoordinatorLineageError(
                    "publication journal differs from durable commitment"
                )
            try:
                publication_lease = self._leases.acquire(
                    "publication",
                    binding.project_identity,
                    self._owner_factory(),
                    self._lease_ttl,
                )
            except LeaseUnavailable:
                return self._report(run_id, record, "busy")
            try:
                with self._runtime.commitment_guard(
                    run_id, record.coordinate
                ) as guarded:
                    guarded_descriptor = parse_effect_descriptor(
                        self._raw_descriptor(guarded.interrupt)
                    )
                    if (
                        guarded.binding != binding
                        or guarded.interrupt.coordinate != record.coordinate
                        or guarded_descriptor != descriptor
                    ):
                        raise CoordinatorLineageError(
                            "publication graph authority changed before commitment"
                        )
                    current = self._ledger.get(record.effect_id)
                    if (
                        current.revision != record.revision
                        or current.phase != "launching"
                        or not self._leases.is_current(lease)
                        or not self._leases.is_current(publication_lease)
                    ):
                        return self._report(run_id, current, "busy")
                    with self._authority.commitment(
                        grant, request, prepared_publication
                    ):
                        receipt = publisher.apply_or_recover(
                            prepared_publication
                        )
                if receipt.phase != "applied":
                    return self._report(run_id, record, "publication_progress")
                if self._snapshot_resolver is not None:
                    self._snapshot_resolver.capture_successor(
                        binding,
                        interrupt,
                        descriptor,
                        effect_id,
                        purpose="publication",
                    )
                sealed = self._ledger.seal(
                    record.effect_id,
                    self._publication_result(
                        record.effect_id, receipt.journal_digest
                    ),
                    expected_revision=record.revision,
                    lease=lease,
                    runner_binding_digest=publisher.binding_digest,
                )
                return self._report(run_id, sealed, "sealed")
            finally:
                self._leases.release(publication_lease)
        raise CoordinatorLineageError("publication has an impossible ledger phase")

    def reconcile(
        self,
        run_id: str,
        *,
        coordinate=None,
        expected_descriptor_digest: str | None = None,
    ) -> ReconcileReport:
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
        missing_records = tuple(
            item for item in records if item.coordinate not in pending_by_coordinate
        )
        for missing in missing_records:
            lineage = self._protected_lineage(
                run_id, missing.coordinate, missing.descriptor_digest
            )
            if lineage == "descended" and missing.phase in {"sealed", "indeterminate"}:
                if coordinate is not None and coordinate != missing.coordinate:
                    continue
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
        if coordinate is not None:
            try:
                interrupt = pending_by_coordinate[coordinate]
            except KeyError as exc:
                if expected_descriptor_digest is not None:
                    effect_id = derive_effect_id(
                        coordinate, expected_descriptor_digest
                    )
                    try:
                        delivered = self._ledger.get(effect_id)
                    except KeyError:
                        delivered = None
                    if (
                        delivered is not None
                        and delivered.phase == "delivered"
                        and delivered.coordinate == coordinate
                        and delivered.descriptor_digest == expected_descriptor_digest
                        and self._protected_lineage(
                            run_id, coordinate, expected_descriptor_digest
                        )
                        == "descended"
                    ):
                        return self._report(run_id, delivered, "delivered")
                raise CoordinatorLineageError(
                    "selected effect coordinate is not exactly pending"
                ) from exc
            record = records_by_coordinate.get(coordinate)
        else:
            active = [
                item
                for item in records
                if item.phase not in {"sealed", "indeterminate"}
            ]
            if active:
                record = active[0]
                interrupt = pending_by_coordinate[record.coordinate]
            else:
                unrecorded = [
                    interrupt
                    for current_coordinate, interrupt in pending_by_coordinate.items()
                    if current_coordinate not in records_by_coordinate
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
            if isinstance(descriptor, DecisionDescriptor):
                return self._reconcile_decision(
                    run_id,
                    binding,
                    interrupt,
                    descriptor,
                    effect_id,
                    record,
                )
            if isinstance(descriptor, AcceptDescriptor):
                return self._reconcile_acceptance(
                    run_id, descriptor, interrupt, effect_id, record, lease
                )
            if isinstance(descriptor, PublishDescriptor):
                return self._reconcile_publication(
                    run_id,
                    binding,
                    snapshot,
                    descriptor,
                    interrupt,
                    effect_id,
                    record,
                    lease,
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
                result = self._admit_artifacts(
                    binding, context, record, result, safety
                )
                if result.snapshot_ref is not None:
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

    def reconcile_one(
        self,
        run_id: str,
        coordinate,
        *,
        expected_descriptor_digest: str | None = None,
    ) -> ReconcileReport:
        """Advance one exact current native interrupt by one monotonic decision."""

        return self.reconcile(
            run_id,
            coordinate=coordinate,
            expected_descriptor_digest=expected_descriptor_digest,
        )

    def reconcile_pending(self, run_id: str) -> tuple[ReconcileReport, ...]:
        """Sweep the current native task set once without owning branch progress."""

        binding = self._binding(run_id)
        snapshot = self._runtime.snapshot(run_id, subgraphs=True)
        protected = self._protected(snapshot)
        if len(protected) > self.MAX_DUE_PER_SCAN:
            raise CoordinatorLineageError(
                "run exceeds the bounded pending effect capacity"
            )
        if any(
            interrupt.coordinate.thread_id != binding.thread_id
            for interrupt in protected
        ):
            raise CoordinatorLineageError(
                "pending sweep contains a foreign native thread"
            )
        return tuple(
            self.reconcile_one(
                run_id,
                interrupt.coordinate,
                expected_descriptor_digest=parse_effect_descriptor(
                    self._raw_descriptor(interrupt)
                ).digest,
            )
            for interrupt in protected
        )

    def reconcile_consumed(self, run_id: str) -> tuple[ReconcileReport, ...]:
        """Drain exact post-commit effect facts absent from native pending state."""

        binding = self._binding(run_id)
        snapshot = self._runtime.snapshot(run_id, subgraphs=True)
        if self._protected(snapshot):
            raise CoordinatorLineageError(
                "consumed-effect recovery requires no protected pending tasks"
            )
        records = self._ledger.list_nonterminal_for_thread(
            binding.thread_id, limit=self.MAX_DUE_PER_SCAN + 1
        )
        if len(records) > self.MAX_DUE_PER_SCAN:
            raise CoordinatorLineageError(
                "run exceeds the bounded nonterminal effect capacity"
            )
        return tuple(
            self.reconcile_one(
                run_id,
                record.coordinate,
                expected_descriptor_digest=record.descriptor_digest,
            )
            for record in records
        )

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
                if self._snapshot_resolver is not None:
                    self._snapshot_resolver.capture_successor(
                        binding,
                        guarded.interrupt,
                        guarded_descriptor,
                        effect_id,
                        purpose="manual",
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

    def _acceptance_commitment(
        self,
        run_id: str,
        binding: RunBinding,
        snapshot: NativeSnapshot,
        interrupt: NativeInterrupt,
        descriptor: AcceptDescriptor,
        effect_id: str,
    ) -> tuple[PublicationConsentCommitment, EffectRecord]:
        if self._artifacts is None:
            raise ProviderContractViolation("acceptance requires ArtifactRegistry")
        values = self._interrupt_values(snapshot, interrupt)
        try:
            producer = parse_effect_result(
                values[descriptor.producer_result_state_key]
            )
            producer_record = self._ledger.get(producer.effect_id)
            record = self._ledger.get(effect_id)
            candidates = []
            for raw_ref in producer.artifact_refs:
                artifact = self._artifacts.read(raw_ref)
                if artifact.declared_name == descriptor.declared_name:
                    candidates.append(artifact)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise CoordinatorLineageError(
                "acceptance lacks exact delivered artifact provenance"
            ) from exc
        if len(candidates) != 1:
            raise CoordinatorLineageError(
                "acceptance requires one exact declared artifact"
            )
        artifact = candidates[0]
        if (
            record.coordinate != interrupt.coordinate
            or record.descriptor_digest != descriptor.digest
            or record.effect_kind != "accept"
            or producer_record.phase != "delivered"
            or producer_record.result != producer
            or str(artifact.ref) not in producer.artifact_refs
            or artifact.declared_name != descriptor.declared_name
            or artifact.producer_effect_id != producer.effect_id
            or artifact.producer_coordinate != producer_record.coordinate
            or artifact.public_run_id != binding.public_run_id
            or artifact.project_identity != binding.project_identity
            or artifact.definition_digest != binding.recipe_digest
            or not self._runtime.checkpoint_is_ancestor(
                run_id, producer_record.coordinate, interrupt
            )
        ):
            raise CoordinatorLineageError(
                "acceptance differs from the exact artifact producer"
            )
        commitment = PublicationConsentCommitment.build(
            binding=binding,
            source=interrupt.coordinate,
            effect_id=effect_id,
            descriptor=descriptor,
            producer_effect_id=producer.effect_id,
            artifact_ref=str(artifact.ref),
            artifact_digest=artifact.blob.sha256,
        )
        return commitment, record

    def _pending_acceptance(
        self, run_id: str, source
    ) -> tuple[
        RunBinding,
        NativeSnapshot,
        NativeInterrupt,
        AcceptDescriptor,
        str,
    ]:
        binding = self._binding(run_id)
        snapshot = self._runtime.snapshot(run_id, subgraphs=True)
        matches = tuple(
            interrupt
            for interrupt in self._protected(snapshot)
            if interrupt.coordinate == source
        )
        if len(matches) != 1:
            raise CoordinatorLineageError(
                "acceptance source is not the exact pending interrupt"
            )
        interrupt = matches[0]
        descriptor, effect_id = self._identity(
            run_id, binding, interrupt, None
        )
        if not isinstance(descriptor, AcceptDescriptor):
            raise ProviderContractViolation("submission does not target acceptance")
        return binding, snapshot, interrupt, descriptor, effect_id

    def preview_acceptance(
        self, run_id: str, source
    ) -> PublicationConsentCommitment:
        binding, snapshot, interrupt, descriptor, effect_id = (
            self._pending_acceptance(run_id, source)
        )
        commitment, record = self._acceptance_commitment(
            run_id,
            binding,
            snapshot,
            interrupt,
            descriptor,
            effect_id,
        )
        if record.phase != "prepared":
            raise CoordinatorLineageError(
                "acceptance is not awaiting owner consent"
            )
        return commitment

    def issue_acceptance_consent(
        self,
        run_id: str,
        source,
        expected_commitment_digest: str,
    ) -> IssuedPublicationConsent:
        if not isinstance(self._authority, OwnerConsentAuthority):
            raise ProviderContractViolation(
                "acceptance requires the owner consent authority"
            )
        binding, _snapshot, _interrupt, _descriptor, effect_id = (
            self._pending_acceptance(run_id, source)
        )
        lease = self._acquire(effect_id)
        try:
            with self._runtime.commitment_guard(run_id, source) as guarded:
                guarded_descriptor = parse_effect_descriptor(
                    self._raw_descriptor(guarded.interrupt)
                )
                if (
                    guarded.binding != binding
                    or guarded.interrupt.coordinate != source
                    or not isinstance(guarded_descriptor, AcceptDescriptor)
                ):
                    raise CoordinatorLineageError(
                        "acceptance changed before owner consent issuance"
                    )
                guarded_effect_id = derive_effect_id(
                    source, guarded_descriptor.digest
                )
                if guarded_effect_id != effect_id:
                    raise CoordinatorLineageError(
                        "acceptance changed before owner consent issuance"
                    )
                commitment, current = self._acceptance_commitment(
                    run_id,
                    binding,
                    guarded.snapshot,
                    guarded.interrupt,
                    guarded_descriptor,
                    effect_id,
                )
                if (
                    current.phase != "prepared"
                    or not self._leases.is_current(lease)
                    or commitment.digest != expected_commitment_digest
                ):
                    raise StaleEffectRevision(
                        "acceptance changed after owner consent preview"
                    )
                return self._authority.issue(commitment)
        finally:
            self._leases.release(lease)

    def submit_acceptance(
        self,
        run_id: str,
        source,
        token: str,
    ) -> ScenarioStatus:
        """Redeem one bearer token for its exact pending acceptance."""

        if not isinstance(self._authority, OwnerConsentAuthority):
            raise ProviderContractViolation(
                "acceptance requires the owner consent authority"
            )
        binding = self._binding(run_id)
        snapshot = self._runtime.snapshot(run_id, subgraphs=True)
        matches = tuple(
            interrupt
            for interrupt in self._protected(snapshot)
            if interrupt.coordinate == source
        )
        if not matches:
            stored = self._authority.inspect_token(token)
            commitment = stored.commitment
            if (
                commitment.public_run_id != binding.public_run_id
                or commitment.project_identity != binding.project_identity
                or commitment.definition_digest != binding.recipe_digest
                or commitment.source != source
            ):
                raise EffectAuthorityDenied("invalid or stale publication consent")
            result = self._authority.redeem(token, commitment)
            try:
                record = self._ledger.get(commitment.effect_id)
            except KeyError as exc:
                raise CoordinatorLineageError(
                    "accepted consent lacks a durable effect record"
                ) from exc
            if (
                record.phase != "delivered"
                or record.result != result
                or self._protected_lineage(
                    run_id, record.coordinate, record.descriptor_digest
                )
                != "descended"
            ):
                raise CoordinatorLineageError(
                    "delivered acceptance retry differs from durable lineage"
                )
            return project_status(binding, snapshot, self._leases, self._ledger)
        if len(matches) != 1:
            raise CoordinatorLineageError(
                "acceptance source is not the exact pending interrupt"
            )
        interrupt = matches[0]
        descriptor, effect_id = self._identity(run_id, binding, interrupt, None)
        if not isinstance(descriptor, AcceptDescriptor):
            raise ProviderContractViolation("submission does not target acceptance")
        commitment, record = self._acceptance_commitment(
            run_id, binding, snapshot, interrupt, descriptor, effect_id
        )
        if record.phase not in {"prepared", "sealed"}:
            raise CoordinatorLineageError(
                "acceptance is not redeemable or awaiting delivery"
            )
        lease = self._acquire(effect_id)
        try:
            with self._runtime.commitment_guard(run_id, source) as guarded:
                guarded_descriptor = parse_effect_descriptor(
                    self._raw_descriptor(guarded.interrupt)
                )
                if (
                    guarded.binding != binding
                    or guarded.interrupt.coordinate != source
                    or guarded_descriptor != descriptor
                ):
                    raise StaleEffectRevision(
                        "acceptance changed before consent commitment"
                    )
                guarded_commitment, current = self._acceptance_commitment(
                    run_id,
                    binding,
                    guarded.snapshot,
                    guarded.interrupt,
                    descriptor,
                    effect_id,
                )
                if (
                    guarded_commitment != commitment
                    or current.revision != record.revision
                    or current.phase not in {"prepared", "sealed"}
                    or not self._leases.is_current(lease)
                ):
                    raise StaleEffectRevision(
                        "acceptance changed before consent commitment"
                    )
                result = self._authority.redeem(token, guarded_commitment)
                try:
                    parsed = parse_acceptance_result(
                        result.to_dict(), descriptor=descriptor
                    )
                except (TypeError, ValueError) as exc:
                    raise ProviderContractViolation(
                        "owner authority returned an invalid acceptance result"
                    ) from exc
                if parsed != result or result.effect_id != effect_id:
                    raise ProviderContractViolation(
                        "owner authority returned a foreign acceptance result"
                    )
                if current.phase == "prepared":
                    self._ledger.seal(
                        effect_id,
                        result,
                        expected_revision=current.revision,
                        lease=lease,
                    )
                elif current.result != result:
                    raise CoordinatorLineageError(
                        "sealed acceptance differs from redeemed owner receipt"
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

            native_order = {
                interrupt.coordinate: index
                for index, interrupt in enumerate(self._protected(snapshot))
            }
            current_deliverable.sort(
                key=lambda item: native_order[item[1].coordinate]
            )
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
        for record in self._ledger.list_due(now, limit=self.MAX_DUE_PER_SCAN):
            binding = self._catalog.find_by_thread(record.coordinate.thread_id)
            reports.append(
                self.reconcile_one(
                    binding.public_run_id,
                    record.coordinate,
                    expected_descriptor_digest=record.descriptor_digest,
                )
            )
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
