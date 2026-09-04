"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from datetime import UTC, datetime

from lockstep.runtime.artifacts import (
    ArtifactDeclaration,
)
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects._coordinator_values import (
    CoordinatorLineageError,
    ProviderContractViolation,
    ReconcileAction,
    ReconcileReport,
    _Context,
)
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_decision_result,
    parse_effect_descriptor,
    parse_effect_result,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.effects.models import (
    AcceptDescriptor,
    DecisionDescriptor,
    EffectDescriptor,
    EffectResult,
    PublishDescriptor,
    ScopeDescriptor,
)
from lockstep.runtime.leases import Lease
from lockstep.runtime.native_models import NativeInterrupt
from lockstep.runtime.project_snapshots import ProjectSnapshotRef
from lockstep.runtime.providers.base import (
    TerminalSafetyObservation,
)
from lockstep.runtime.providers.manual import (
    ManualHandoff,
    ManualProviderError,
)


class _EffectCoordinatorFoundation:
    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coordinator clock must include a timezone")
        return value.astimezone(UTC)

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
        return ReconcileReport(
            run_id, effect_id, ReconcileAction.DELIVERED.value, None
        )

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
