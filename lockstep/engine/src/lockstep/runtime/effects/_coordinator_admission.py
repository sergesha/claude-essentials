"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from typing import Any

from lockstep.runtime.artifacts import ArtifactRecord
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects._coordinator_values import (
    CoordinatorLineageError,
    ProviderContractViolation,
)
from lockstep.runtime.effects.authority import (
    EffectAuthorityDenied,
)
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_acceptance_result,
    parse_effect_descriptor,
    parse_effect_result,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
    StaleEffectRevision,
)
from lockstep.runtime.effects.models import (
    AcceptanceResult,
    AcceptDescriptor,
    EffectDescriptor,
    EffectResult,
)
from lockstep.runtime.effects.owner_consent import (
    IssuedPublicationConsent,
    OwnerConsentAuthority,
    PublicationConsentCommitment,
    StoredPublicationConsent,
)
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot
from lockstep.runtime.providers.manual import (
    ManualSubmission,
)
from lockstep.runtime.status import ScenarioStatus, project_status


class _EffectCoordinatorAdmission:
    def _manual_submission_context(
        self,
        run_id: str,
        source: Any,
    ) -> tuple[RunBinding, NativeInterrupt, EffectDescriptor, str, EffectRecord]:
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
        descriptor, effect_id = self._identity(
            run_id, binding, interrupt, None
        )
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
            raise CoordinatorLineageError(
                "manual effect is not awaiting one result"
            )
        return binding, interrupt, descriptor, effect_id, record

    def _commit_manual_submission(
        self,
        *,
        run_id: str,
        source: Any,
        submission: ManualSubmission,
        binding: RunBinding,
        effect_id: str,
        record: EffectRecord,
    ) -> None:
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
                result = self._closed_result(
                    self._manual.complete(handoff, submission)
                )
                if result.effect_id != effect_id:
                    raise ProviderContractViolation(
                        "manual result targets another effect"
                    )
                if (
                    self._snapshot_resolver is not None
                    and result.fixed_error_code != "manifest_invalid"
                ):
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

    def submit_manual(
        self,
        run_id: str,
        source,
        submission: ManualSubmission,
    ) -> ScenarioStatus:
        """Seal one protected manual result, then use ordinary native delivery."""

        if not isinstance(submission, ManualSubmission):
            raise ProviderContractViolation("closed ManualSubmission is required")
        binding, _interrupt, _descriptor, effect_id, record = (
            self._manual_submission_context(run_id, source)
        )
        self._commit_manual_submission(
            run_id=run_id,
            source=source,
            submission=submission,
            binding=binding,
            effect_id=effect_id,
            record=record,
        )
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
        with self._runtime.decision_guard(run_id):
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

    @staticmethod
    def _acceptance_retry_commitment(
        stored: StoredPublicationConsent,
        binding: RunBinding,
        source: Any,
    ) -> PublicationConsentCommitment:
        commitment = stored.commitment
        if (
            commitment.public_run_id != binding.public_run_id
            or commitment.project_identity != binding.project_identity
            or commitment.definition_digest != binding.recipe_digest
            or commitment.source != source
        ):
            raise EffectAuthorityDenied("invalid or stale publication consent")
        return commitment

    def _acceptance_retry_state(
        self,
        commitment: PublicationConsentCommitment,
    ) -> tuple[EffectRecord, EffectRecord, ArtifactRecord]:
        try:
            record = self._ledger.get(commitment.effect_id)
            producer = self._ledger.get(commitment.producer_effect_id)
            if self._artifacts is None:
                raise ProviderContractViolation(
                    "acceptance retry requires ArtifactRegistry"
                )
            artifact = self._artifacts.read(commitment.artifact_ref)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise EffectAuthorityDenied(
                "invalid or stale publication consent"
            ) from exc
        return record, producer, artifact

    @staticmethod
    def _acceptance_retry_expected_result(
        stored: StoredPublicationConsent,
        commitment: PublicationConsentCommitment,
    ) -> AcceptanceResult:
        if stored.redeemed_at is None or stored.receipt_digest is None:
            raise EffectAuthorityDenied("invalid or stale publication consent")
        return AcceptanceResult(
            "lockstep.acceptance-result/v1",
            commitment.effect_id,
            "PASS",
            commitment.artifact_ref,
            commitment.artifact_digest,
            commitment.destination,
            commitment.transformation,
            commitment.audience,
            stored.consent_ref,
            stored.consent_epoch,
            stored.receipt_digest,
        )

    @staticmethod
    def _validate_acceptance_retry_record(
        record: EffectRecord,
        commitment: PublicationConsentCommitment,
        expected_result: AcceptanceResult,
    ) -> None:
        if (
            record.phase != "delivered"
            or record.effect_kind != "accept"
            or record.effect_id != commitment.effect_id
            or not isinstance(record.result, AcceptanceResult)
            or record.result != expected_result
            or record.coordinate != commitment.source
            or record.descriptor_digest != commitment.descriptor_digest
        ):
            raise EffectAuthorityDenied("invalid or stale publication consent")

    @staticmethod
    def _validate_acceptance_retry_producer(
        producer: EffectRecord,
        artifact: ArtifactRecord,
        commitment: PublicationConsentCommitment,
        binding: RunBinding,
    ) -> None:
        producer_result = producer.result
        if (
            producer.phase != "delivered"
            or not isinstance(producer_result, EffectResult)
            or producer.effect_id != commitment.producer_effect_id
            or commitment.artifact_ref not in producer_result.artifact_refs
            or artifact.producer_effect_id != producer.effect_id
            or artifact.producer_coordinate != producer.coordinate
            or artifact.descriptor_digest != producer.descriptor_digest
            or artifact.public_run_id != binding.public_run_id
            or artifact.project_identity != binding.project_identity
            or artifact.definition_digest != binding.recipe_digest
            or artifact.blob.sha256 != commitment.artifact_digest
        ):
            raise EffectAuthorityDenied("invalid or stale publication consent")

    def _validate_acceptance_retry_lineage(
        self,
        run_id: str,
        record: EffectRecord,
    ) -> None:
        if (
            self._protected_lineage(
                run_id, record.coordinate, record.descriptor_digest
            )
            != "descended"
        ):
            raise EffectAuthorityDenied("invalid or stale publication consent")

    def _redeem_delivered_acceptance_retry(
        self,
        *,
        run_id: str,
        source: Any,
        token: str,
        binding: RunBinding,
        snapshot: NativeSnapshot,
    ) -> ScenarioStatus:
        assert isinstance(self._authority, OwnerConsentAuthority)
        stored = self._authority.inspect_token(token)
        commitment = self._acceptance_retry_commitment(stored, binding, source)
        record, producer, artifact = self._acceptance_retry_state(commitment)
        expected_result = self._acceptance_retry_expected_result(stored, commitment)
        self._validate_acceptance_retry_record(record, commitment, expected_result)
        self._validate_acceptance_retry_producer(
            producer, artifact, commitment, binding
        )
        self._validate_acceptance_retry_lineage(run_id, record)
        result = self._authority.redeem(token, commitment)
        if result != expected_result:
            raise CoordinatorLineageError(
                "redeemed acceptance retry differs from durable result"
            )
        return project_status(binding, snapshot, self._leases, self._ledger)

    def _acceptance_submission_context(
        self,
        *,
        run_id: str,
        binding: RunBinding,
        snapshot: NativeSnapshot,
        interrupt: NativeInterrupt,
    ) -> tuple[
        AcceptDescriptor,
        str,
        PublicationConsentCommitment,
        EffectRecord,
    ]:
        descriptor, effect_id = self._identity(
            run_id, binding, interrupt, None
        )
        if not isinstance(descriptor, AcceptDescriptor):
            raise ProviderContractViolation("submission does not target acceptance")
        commitment, record = self._acceptance_commitment(
            run_id, binding, snapshot, interrupt, descriptor, effect_id
        )
        if record.phase not in {"prepared", "sealed"}:
            raise CoordinatorLineageError(
                "acceptance is not redeemable or awaiting delivery"
            )
        return descriptor, effect_id, commitment, record

    def _commit_acceptance_submission(
        self,
        *,
        run_id: str,
        source: Any,
        token: str,
        binding: RunBinding,
        descriptor: AcceptDescriptor,
        effect_id: str,
        commitment: PublicationConsentCommitment,
        record: EffectRecord,
    ) -> None:
        assert isinstance(self._authority, OwnerConsentAuthority)
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
        with self._runtime.decision_guard(run_id):
            binding = self._binding(run_id)
            snapshot = self._runtime.snapshot(run_id, subgraphs=True)
            matches = tuple(
                interrupt
                for interrupt in self._protected(snapshot)
                if interrupt.coordinate == source
            )
            if not matches:
                return self._redeem_delivered_acceptance_retry(
                    run_id=run_id,
                    source=source,
                    token=token,
                    binding=binding,
                    snapshot=snapshot,
                )
            if len(matches) != 1:
                raise CoordinatorLineageError(
                    "acceptance source is not the exact pending interrupt"
                )
            interrupt = matches[0]
            descriptor, effect_id, commitment, record = (
                self._acceptance_submission_context(
                    run_id=run_id,
                    binding=binding,
                    snapshot=snapshot,
                    interrupt=interrupt,
                )
            )
            self._commit_acceptance_submission(
                run_id=run_id,
                source=source,
                binding=binding,
                token=token,
                descriptor=descriptor,
                effect_id=effect_id,
                commitment=commitment,
                record=record,
            )
            return self.deliver_ready(run_id, [source.interrupt_id])
