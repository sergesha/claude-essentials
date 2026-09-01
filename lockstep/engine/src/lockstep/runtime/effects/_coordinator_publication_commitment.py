"""Validate publication authority and derive its immutable commitment."""

from __future__ import annotations

from typing import Any

from lockstep.runtime.effects._coordinator_values import CoordinatorLineageError
from lockstep.runtime.effects.authority import EffectGrant
from lockstep.runtime.effects.ledger import EffectRecord
from lockstep.runtime.providers.base import EffectRequest
from lockstep.runtime.publication import ProjectPublisher, PublicationRequest


class _EffectCoordinatorPublicationCommitment:
    @staticmethod
    def _validate_publication_authority(
        *,
        record: EffectRecord,
        request: EffectRequest,
        grant: EffectGrant,
        publisher: ProjectPublisher,
    ) -> None:
        if (
            record.request_digest != request.request_digest
            or record.grant_digest != grant.digest
            or record.runner_binding_digest != publisher.binding_digest
        ):
            raise CoordinatorLineageError(
                "publication authority differs from durable ledger facts"
            )

    @staticmethod
    def _prepared_publication_commitment(
        *,
        publisher: ProjectPublisher,
        publication_request: PublicationRequest,
    ) -> tuple[Any, str]:
        prepared_publication = publisher.prepare(publication_request)
        commitment_digest = publisher.commitment_digest(prepared_publication)
        return prepared_publication, commitment_digest
