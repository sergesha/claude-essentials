"""Durable owner-issued authority for exact artifact publication consent."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from lockstep.runtime.effects._owner_consent_values import (
    IssuedPublicationConsent,
    PublicationConsentCommitment,
    StoredPublicationConsent,
    _canonical,
    _coordinate_data,
    _digest,
    _text,
    _utc,
)
from lockstep.runtime.effects.authority import (
    EffectAuthorityDenied,
    EffectAuthorityGate,
    EffectGrant,
)
from lockstep.runtime.effects.models import AcceptanceResult
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.providers.base import EffectRequest, PreparedLaunch
from lockstep.runtime.publication import PreparedPublication
from lockstep.runtime.storage import SQLiteStore


class OwnerConsentAuthority:
    """Project-scoped durable bearer consent and combined effect authority."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        delegate: EffectAuthorityGate,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
        consent_ref_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(store, SQLiteStore):
            raise TypeError("owner consent requires SQLiteStore")
        self._store = store
        self._delegate = delegate
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._consent_ref_factory = consent_ref_factory or (
            lambda: f"consent:{secrets.token_hex(16)}"
        )

    def _now(self) -> datetime:
        return _utc(self._clock(), "owner consent clock")

    @staticmethod
    def _token_hash(token: object) -> str:
        checked = _text(token, "publication consent token")
        return hashlib.sha256(checked.encode("utf-8")).hexdigest()

    @staticmethod
    def _row_epoch(connection, table, project_identity: str) -> int | None:
        row = connection.execute(
            select(table.c.epoch).where(table.c.project_identity == project_identity)
        ).first()
        return None if row is None else int(row.epoch)

    def current_epoch(self, project_identity: str) -> int:
        project = _text(project_identity, "project_identity")
        table = self._store.tables.consent_epochs
        with self._store.read_connection() as connection:
            epoch = self._row_epoch(connection, table, project)
        return 1 if epoch is None else epoch

    def issue(
        self, commitment: PublicationConsentCommitment
    ) -> IssuedPublicationConsent:
        if not isinstance(commitment, PublicationConsentCommitment):
            raise TypeError("closed publication consent commitment is required")
        token = _text(self._token_factory(), "publication consent token")
        token_sha256 = self._token_hash(token)
        consent_ref = _text(self._consent_ref_factory(), "consent_ref")
        now = self._now().isoformat()
        epochs = self._store.tables.consent_epochs
        consents = self._store.tables.publication_consents
        try:
            with self._store._v2_write_transaction() as connection:
                epoch = self._row_epoch(
                    connection, epochs, commitment.project_identity
                )
                if epoch is None:
                    epoch = 1
                    connection.execute(
                        epochs.insert().values(
                            project_identity=commitment.project_identity,
                            epoch=epoch,
                            updated_at=now,
                        )
                    )
                source = commitment.source
                connection.execute(
                    consents.insert().values(
                        consent_ref=consent_ref,
                        token_sha256=token_sha256,
                        project_identity=commitment.project_identity,
                        public_run_id=commitment.public_run_id,
                        definition_digest=commitment.definition_digest,
                        source_thread_id=source.thread_id,
                        source_checkpoint_ns=source.checkpoint_ns,
                        source_checkpoint_id=source.checkpoint_id,
                        source_task_id=source.task_id,
                        source_interrupt_id=source.interrupt_id,
                        effect_id=commitment.effect_id,
                        descriptor_digest=commitment.descriptor_digest,
                        producer_effect_id=commitment.producer_effect_id,
                        artifact_ref=commitment.artifact_ref,
                        artifact_digest=commitment.artifact_digest,
                        destination=commitment.destination,
                        transformation=commitment.transformation,
                        audience=commitment.audience,
                        commitment_digest=commitment.digest,
                        consent_epoch=epoch,
                        issued_at=now,
                        redeemed_at=None,
                        receipt_digest=None,
                    )
                )
        except IntegrityError as exc:
            raise EffectAuthorityDenied(
                "publication consent is already issued for this exact current commitment"
            ) from exc
        return IssuedPublicationConsent(consent_ref, token, commitment.digest, epoch)

    @staticmethod
    def _commitment_from_row(values) -> PublicationConsentCommitment:
        source = NativeCoordinate(
            values["source_thread_id"],
            values["source_checkpoint_id"],
            values["source_checkpoint_ns"],
            values["source_task_id"],
            values["source_interrupt_id"],
        )
        data = {
            "schema": "lockstep.publication-consent-commitment/v1",
            "public_run_id": values["public_run_id"],
            "project_identity": values["project_identity"],
            "definition_digest": values["definition_digest"],
            "source": _coordinate_data(source),
            "effect_id": values["effect_id"],
            "descriptor_digest": values["descriptor_digest"],
            "producer_effect_id": values["producer_effect_id"],
            "artifact_ref": values["artifact_ref"],
            "artifact_digest": values["artifact_digest"],
            "destination": values["destination"],
            "transformation": values["transformation"],
            "audience": values["audience"],
        }
        digest = hashlib.sha256(_canonical(data)).hexdigest()
        if digest != values["commitment_digest"]:
            raise EffectAuthorityDenied("invalid or stale publication consent")
        if data["transformation"] != "identity" or data["audience"] != "local-project":
            raise EffectAuthorityDenied("invalid or stale publication consent")
        return PublicationConsentCommitment(
            data["schema"],
            data["public_run_id"],
            data["project_identity"],
            data["definition_digest"],
            source,
            data["effect_id"],
            data["descriptor_digest"],
            data["producer_effect_id"],
            data["artifact_ref"],
            data["artifact_digest"],
            data["destination"],
            data["transformation"],
            data["audience"],
            digest,
        )

    def _stored_from_row(self, values) -> StoredPublicationConsent:
        redeemed_at = values["redeemed_at"]
        return StoredPublicationConsent(
            values["consent_ref"],
            self._commitment_from_row(values),
            int(values["consent_epoch"]),
            None if redeemed_at is None else _utc(
                datetime.fromisoformat(redeemed_at), "redeemed_at"
            ),
            values["receipt_digest"],
        )

    def inspect_token(self, token: str) -> StoredPublicationConsent:
        token_sha256 = self._token_hash(token)
        consents = self._store.tables.publication_consents
        epochs = self._store.tables.consent_epochs
        with self._store.read_connection() as connection:
            row = connection.execute(
                select(consents).where(consents.c.token_sha256 == token_sha256)
            ).first()
            if row is None:
                raise EffectAuthorityDenied("invalid or stale publication consent")
            values = row._mapping
            epoch = self._row_epoch(connection, epochs, values["project_identity"])
            if epoch is None or int(values["consent_epoch"]) != epoch:
                raise EffectAuthorityDenied("invalid or stale publication consent")
            return self._stored_from_row(values)

    @staticmethod
    def _receipt_data(
        *,
        consent_ref: str,
        consent_epoch: int,
        commitment_digest: str,
        redeemed_at: str,
    ) -> dict[str, object]:
        return {
            "schema": "lockstep.publication-consent-receipt/v1",
            "consent_ref": consent_ref,
            "consent_epoch": consent_epoch,
            "commitment_digest": commitment_digest,
            "redeemed_at": redeemed_at,
        }

    @classmethod
    def _acceptance_from_row(cls, values) -> AcceptanceResult:
        redeemed_at = values["redeemed_at"]
        receipt_digest = values["receipt_digest"]
        if redeemed_at is None or receipt_digest is None:
            raise EffectAuthorityDenied("invalid or stale publication consent")
        expected_receipt = hashlib.sha256(
            _canonical(
                cls._receipt_data(
                    consent_ref=values["consent_ref"],
                    consent_epoch=int(values["consent_epoch"]),
                    commitment_digest=values["commitment_digest"],
                    redeemed_at=redeemed_at,
                )
            )
        ).hexdigest()
        if receipt_digest != expected_receipt:
            raise EffectAuthorityDenied("invalid or stale publication consent")
        return AcceptanceResult(
            "lockstep.acceptance-result/v1",
            values["effect_id"],
            "PASS",
            values["artifact_ref"],
            values["artifact_digest"],
            values["destination"],
            "identity",
            "local-project",
            values["consent_ref"],
            int(values["consent_epoch"]),
            receipt_digest,
        )

    def redeem(
        self,
        token: str,
        commitment: PublicationConsentCommitment,
    ) -> AcceptanceResult:
        if not isinstance(commitment, PublicationConsentCommitment):
            raise TypeError("closed publication consent commitment is required")
        token_sha256 = self._token_hash(token)
        consents = self._store.tables.publication_consents
        epochs = self._store.tables.consent_epochs
        with self._store._v2_write_transaction() as connection:
            row = connection.execute(
                select(consents).where(consents.c.token_sha256 == token_sha256)
            ).first()
            if row is None:
                raise EffectAuthorityDenied("invalid or stale publication consent")
            values = row._mapping
            epoch = self._row_epoch(connection, epochs, values["project_identity"])
            try:
                stored_commitment = self._commitment_from_row(values)
            except (TypeError, ValueError) as exc:
                raise EffectAuthorityDenied(
                    "invalid or stale publication consent"
                ) from exc
            if (
                epoch is None
                or int(values["consent_epoch"]) != epoch
                or stored_commitment != commitment
            ):
                raise EffectAuthorityDenied("invalid or stale publication consent")
            if values["redeemed_at"] is not None:
                return self._acceptance_from_row(values)
            redeemed_at = self._now().isoformat()
            receipt_digest = hashlib.sha256(
                _canonical(
                    self._receipt_data(
                        consent_ref=values["consent_ref"],
                        consent_epoch=epoch,
                        commitment_digest=commitment.digest,
                        redeemed_at=redeemed_at,
                    )
                )
            ).hexdigest()
            connection.execute(
                consents.update()
                .where(consents.c.token_sha256 == token_sha256)
                .values(
                    redeemed_at=redeemed_at,
                    receipt_digest=receipt_digest,
                )
            )
            updated = dict(values)
            updated["redeemed_at"] = redeemed_at
            updated["receipt_digest"] = receipt_digest
            return self._acceptance_from_row(updated)

    def revoke(self, project_identity: str) -> int:
        project = _text(project_identity, "project_identity")
        epochs = self._store.tables.consent_epochs
        now = self._now().isoformat()
        with self._store._v2_write_transaction() as connection:
            current = self._row_epoch(connection, epochs, project)
            next_epoch = (1 if current is None else current) + 1
            if current is None:
                connection.execute(
                    epochs.insert().values(
                        project_identity=project,
                        epoch=next_epoch,
                        updated_at=now,
                    )
                )
            else:
                connection.execute(
                    epochs.update()
                    .where(epochs.c.project_identity == project)
                    .values(epoch=next_epoch, updated_at=now)
                )
        return next_epoch

    @staticmethod
    def _publish_item(value: object) -> Mapping[str, object]:
        fields = {
            "artifact_ref",
            "artifact_blob",
            "destination",
            "transformation",
            "audience",
            "consent_ref",
            "approval_generation",
            "receipt_digest",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise EffectAuthorityDenied("invalid or stale publication authority")
        blob = value["artifact_blob"]
        if not isinstance(blob, Mapping) or set(blob) != {"sha256", "size"}:
            raise EffectAuthorityDenied("invalid or stale publication authority")
        _digest(blob["sha256"], "publication artifact digest")
        size = blob["size"]
        if type(size) is not int or size < 0:
            raise EffectAuthorityDenied("invalid or stale publication authority")
        return value

    def _publication_grant(self, connection, intent: EffectRequest) -> EffectGrant:
        if not isinstance(intent, EffectRequest):
            raise EffectAuthorityDenied("invalid or stale publication authority")
        if (
            intent.effect_kind != "publish"
            or intent.runner_selector != "project-publisher"
            or intent.required_capabilities != ("publication",)
            or intent.grant_digest is not None
            or intent.workspace_ref is not None
            or intent.request_digest != intent.intent_digest
            or not intent.inputs
            or len(intent.inputs) > 32
        ):
            raise EffectAuthorityDenied("invalid or stale publication authority")
        epochs = self._store.tables.consent_epochs
        consents = self._store.tables.publication_consents
        epoch = self._row_epoch(connection, epochs, intent.project_identity)
        if epoch is None:
            raise EffectAuthorityDenied("invalid or stale publication authority")
        consent_refs: set[str] = set()
        receipt_digests: set[str] = set()
        destinations: list[str] = []
        for name, raw_item in intent.inputs:
            _text(name, "publication item name")
            item = self._publish_item(raw_item)
            consent_ref = _text(item["consent_ref"], "consent_ref")
            receipt_digest = _digest(item["receipt_digest"], "receipt_digest")
            if consent_ref in consent_refs or receipt_digest in receipt_digests:
                raise EffectAuthorityDenied("publication consent set is not exact")
            consent_refs.add(consent_ref)
            receipt_digests.add(receipt_digest)
            row = connection.execute(
                select(consents).where(consents.c.consent_ref == consent_ref)
            ).first()
            if row is None:
                raise EffectAuthorityDenied("invalid or stale publication authority")
            values = row._mapping
            acceptance = self._acceptance_from_row(values)
            blob = item["artifact_blob"]
            destination = item["destination"]
            destinations.append(destination)
            if (
                values["project_identity"] != intent.project_identity
                or values["public_run_id"] != intent.public_run_id
                or values["definition_digest"] != intent.definition_digest
                or int(values["consent_epoch"]) != epoch
                or acceptance.artifact_ref != item["artifact_ref"]
                or acceptance.artifact_digest != blob["sha256"]
                or acceptance.destination != destination
                or acceptance.transformation != item["transformation"]
                or acceptance.audience != item["audience"]
                or acceptance.approval_generation != item["approval_generation"]
                or acceptance.receipt_digest != receipt_digest
            ):
                raise EffectAuthorityDenied("invalid or stale publication authority")
        if tuple(destinations) != intent.writes or len(set(destinations)) != len(
            destinations
        ):
            raise EffectAuthorityDenied("publication writes are not exact")
        try:
            return EffectGrant.build(
                intent,
                actor_binding_digest=intent.runner_binding_digest,
                required_authorities=("publication",),
                workspace_ref=None,
                parent_capability_generation=epoch,
                grant_generation=epoch,
                policy_epoch=epoch,
                config_epoch=0,
                approval_generation=epoch,
                expires_at=datetime.max.replace(tzinfo=UTC),
            )
        except (TypeError, ValueError) as exc:
            raise EffectAuthorityDenied(
                "invalid or stale publication authority"
            ) from exc

    def resolve(self, intent: EffectRequest) -> EffectGrant:
        if not isinstance(intent, EffectRequest):
            raise TypeError("closed EffectRequest is required")
        if intent.effect_kind != "publish":
            return self._delegate.resolve(intent)
        with self._store.read_connection() as connection:
            return self._publication_grant(connection, intent)

    @contextmanager
    def commitment(
        self,
        grant: EffectGrant,
        request: EffectRequest,
        launch: PreparedLaunch | PreparedPublication,
    ) -> Iterator[None]:
        if not isinstance(request, EffectRequest):
            raise TypeError("closed EffectRequest is required")
        if request.effect_kind != "publish":
            with self._delegate.commitment(grant, request, launch):
                yield
            return
        if not isinstance(grant, EffectGrant) or not isinstance(
            launch, PreparedPublication
        ):
            raise EffectAuthorityDenied("invalid or stale publication commitment")
        with self._store._v2_write_transaction() as connection:
            unbound = replace(
                request,
                grant_digest=None,
                workspace_ref=None,
                request_digest=request.intent_digest,
            )
            expected = self._publication_grant(connection, unbound)
            try:
                expected_request = unbound.bind_grant(expected)
            except (TypeError, ValueError) as exc:
                raise EffectAuthorityDenied(
                    "invalid or stale publication commitment"
                ) from exc
            if (
                grant != expected
                or request != expected_request
                or request.grant_digest != grant.digest
                or launch.publisher_binding_digest != request.runner_binding_digest
                or launch.publisher_binding_digest != grant.runner_binding_digest
            ):
                raise EffectAuthorityDenied(
                    "invalid or stale publication commitment"
                )
            yield
