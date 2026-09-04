"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from lockstep.runtime.artifacts import (
    ArtifactRecord,
)
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects._coordinator_values import (
    CoordinatorLineageError,
    ProviderContractViolation,
    ReconcileAction,
    ReconcileReport,
    _PublicationItemContext,
    make_reconcile_report,
)
from lockstep.runtime.effects.authority import (
    EffectGrant,
)
from lockstep.runtime.effects.descriptors import (
    parse_acceptance_result,
    parse_effect_result,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.effects.models import (
    AcceptanceResult,
    AcceptDescriptor,
    EffectResult,
    PublishDescriptor,
)
from lockstep.runtime.leases import Lease, LeaseUnavailable
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot
from lockstep.runtime.providers.base import (
    EffectRequest,
)
from lockstep.runtime.publication import (
    ProjectPublisher,
    PublicationEntry,
    PublicationRequest,
)


class _EffectCoordinatorPublicationPlanning:
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

    def _publication_ancestor_results(
        self,
        *,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        values: Mapping[str, object],
        item: Any,
    ) -> tuple[EffectResult, AcceptanceResult, EffectRecord]:
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
        return producer_result, acceptance, producer_record

    def _publication_artifact(
        self,
        *,
        binding: RunBinding,
        item: Any,
        producer_result: EffectResult,
        acceptance: AcceptanceResult,
        producer_record: EffectRecord,
    ) -> ArtifactRecord:
        assert self._artifacts is not None
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
        return artifact

    def _publication_item_context(
        self,
        *,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        values: Mapping[str, object],
        item: Any,
        ordinal: int,
    ) -> _PublicationItemContext:
        producer_result, acceptance, producer_record = (
            self._publication_ancestor_results(
                binding=binding,
                interrupt=interrupt,
                values=values,
                item=item,
            )
        )
        artifact = self._publication_artifact(
            binding=binding,
            item=item,
            producer_result=producer_result,
            acceptance=acceptance,
            producer_record=producer_record,
        )
        return _PublicationItemContext(
            entry=PublicationEntry(
                artifact.ref,
                item.destination,
                item.transformation,
            ),
            intent_input=(
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
            ),
            approval_generation=acceptance.approval_generation,
            consent_ref=acceptance.consent_ref,
        )

    @staticmethod
    def _publication_effect_intent(
        *,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: PublishDescriptor,
        effect_id: str,
        publisher: ProjectPublisher,
        items: tuple[_PublicationItemContext, ...],
    ) -> EffectRequest:
        return EffectRequest.build(
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
            inputs=tuple(item.intent_input for item in items),
            writes=tuple(item.destination for item in descriptor.items),
            deadline_at=None,
        )

    @staticmethod
    def _bound_publication_request(
        *,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: PublishDescriptor,
        effect_id: str,
        publisher: ProjectPublisher,
        request: EffectRequest,
        grant: EffectGrant,
        items: tuple[_PublicationItemContext, ...],
        approval_generation: int,
    ) -> PublicationRequest:
        consent_refs = [item.consent_ref for item in items]
        return PublicationRequest.build(
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
            entries=tuple(item.entry for item in items),
        )

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
        items = tuple(
            self._publication_item_context(
                binding=binding,
                interrupt=interrupt,
                values=values,
                item=item,
                ordinal=ordinal,
            )
            for ordinal, item in enumerate(descriptor.items)
        )
        approval_generation: int | None = None
        for item in items:
            if approval_generation is None:
                approval_generation = item.approval_generation
            elif approval_generation != item.approval_generation:
                raise ProviderContractViolation(
                    "publication items require one approval generation"
                )
        assert approval_generation is not None
        intent = self._publication_effect_intent(
            binding=binding,
            interrupt=interrupt,
            descriptor=descriptor,
            effect_id=effect_id,
            publisher=publisher,
            items=items,
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
        publication_request = self._bound_publication_request(
            binding=binding,
            interrupt=interrupt,
            descriptor=descriptor,
            effect_id=effect_id,
            publisher=publisher,
            request=request,
            grant=grant,
            items=items,
            approval_generation=approval_generation,
        )
        return request, grant, publication_request

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
            return make_reconcile_report(run_id, prepared, ReconcileAction.PREPARED)
        if record.phase == "prepared":
            return make_reconcile_report(run_id, record, ReconcileAction.ACCEPTANCE_PENDING)
        if record.phase in {"sealed", "indeterminate"}:
            return make_reconcile_report(run_id, record, ReconcileAction.AWAITING_DELIVERY)
        raise CoordinatorLineageError("acceptance has an impossible ledger phase")

    def _publication_lease(self, binding: RunBinding) -> Lease | None:
        try:
            return self._leases.acquire(
                "publication",
                binding.project_identity,
                self._owner_factory(),
                self._lease_ttl,
            )
        except LeaseUnavailable:
            return None
