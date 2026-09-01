from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import UniqueConstraint, func, select

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.effects.models import AcceptDescriptor
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.providers.base import EffectRequest, PreparedLaunch
from lockstep.runtime.publication import PreparedPublication
from lockstep.runtime.storage import SQLiteStore


def _accept_descriptor(
    *, destination: str = "docs/review.md"
) -> AcceptDescriptor:
    descriptor = parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "accept",
            "logical_id": "accept-review",
            "artifact_handle": "review.report",
            "producer_result_state_key": "review_result",
            "declared_name": "report",
            "destination": destination,
            "transformation": "identity",
            "audience": "local-project",
            "verdict": "PASS",
            "result_schema": "lockstep.acceptance-result/v1",
        }
    )
    assert isinstance(descriptor, AcceptDescriptor)
    return descriptor


def _binding(*, run_id: str = "run-1", project: str = "/project") -> RunBinding:
    return RunBinding(run_id, "thread-1", "a" * 64, "bundle:" + "b" * 64, project)


def _source(**changes: str) -> NativeCoordinate:
    values = {
        "thread_id": "thread-1",
        "checkpoint_ns": "child",
        "checkpoint_id": "checkpoint-1",
        "task_id": "task-1",
        "interrupt_id": "interrupt-1",
    }
    values.update(changes)
    return NativeCoordinate(**values)


def _commitment(**changes):
    from lockstep.runtime.effects.owner_consent import PublicationConsentCommitment

    values = {
        "binding": _binding(),
        "source": _source(),
        "effect_id": "accept-effect",
        "descriptor": _accept_descriptor(),
        "producer_effect_id": "producer-effect",
        "artifact_ref": "artifact:" + "c" * 64,
        "artifact_digest": "d" * 64,
    }
    values.update(changes)
    return PublicationConsentCommitment.build(**values)


def _publish_intent(result, **changes) -> EffectRequest:
    item = {
        "artifact_ref": result.artifact_ref,
        "artifact_blob": {"sha256": result.artifact_digest, "size": 17},
        "destination": result.destination,
        "transformation": result.transformation,
        "audience": result.audience,
        "consent_ref": result.consent_ref,
        "approval_generation": result.approval_generation,
        "receipt_digest": result.receipt_digest,
    }
    values = {
        "effect_id": "publish-effect",
        "public_run_id": "run-1",
        "project_identity": "/project",
        "definition_digest": "a" * 64,
        "coordinate": _source(
            checkpoint_id="publish-checkpoint",
            task_id="publish-task",
            interrupt_id="publish-interrupt",
        ),
        "descriptor_digest": "e" * 64,
        "effect_kind": "publish",
        "runner_selector": "project-publisher",
        "runner_binding_digest": "f" * 64,
        "required_capabilities": ("publication",),
        "inputs": (("item-0", item),),
        "writes": (result.destination,),
        "deadline_at": None,
    }
    values.update(changes)
    return EffectRequest.build(**values)


def test_owner_consent_tables_are_exact_and_additive(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    try:
        epochs = store.tables.consent_epochs
        consents = store.tables.publication_consents

        assert tuple(epochs.c.keys()) == (
            "project_identity",
            "epoch",
            "updated_at",
        )
        assert tuple(consents.c.keys()) == (
            "consent_ref",
            "token_sha256",
            "project_identity",
            "public_run_id",
            "definition_digest",
            "source_thread_id",
            "source_checkpoint_ns",
            "source_checkpoint_id",
            "source_task_id",
            "source_interrupt_id",
            "effect_id",
            "descriptor_digest",
            "producer_effect_id",
            "artifact_ref",
            "artifact_digest",
            "destination",
            "transformation",
            "audience",
            "commitment_digest",
            "consent_epoch",
            "issued_at",
            "redeemed_at",
            "receipt_digest",
        )
        assert epochs.primary_key.columns.keys() == ["project_identity"]
        assert consents.primary_key.columns.keys() == ["consent_ref"]
        uniques = {
            (constraint.name, tuple(column.name for column in constraint.columns))
            for constraint in consents.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        assert (None, ("token_sha256",)) in uniques
        assert (None, ("receipt_digest",)) in uniques
        assert (
            "uq_publication_consents_exact_epoch",
            ("project_identity", "consent_epoch", "commitment_digest"),
        ) in uniques
        assert not ({"raw_token", "session_id", "status"} & set(consents.c.keys()))
    finally:
        store.close()


def test_publication_consent_commitment_binds_every_exact_input() -> None:
    from lockstep.runtime.effects.owner_consent import PublicationConsentCommitment

    descriptor = _accept_descriptor()
    commitment = PublicationConsentCommitment.build(
        binding=_binding(),
        source=_source(),
        effect_id="accept-effect",
        descriptor=descriptor,
        producer_effect_id="producer-effect",
        artifact_ref="artifact:" + "c" * 64,
        artifact_digest="d" * 64,
    )
    expected = {
        "schema": "lockstep.publication-consent-commitment/v1",
        "public_run_id": "run-1",
        "project_identity": "/project",
        "definition_digest": "a" * 64,
        "source": {
            "thread_id": "thread-1",
            "checkpoint_ns": "child",
            "checkpoint_id": "checkpoint-1",
            "task_id": "task-1",
            "interrupt_id": "interrupt-1",
        },
        "effect_id": "accept-effect",
        "descriptor_digest": descriptor.digest,
        "producer_effect_id": "producer-effect",
        "artifact_ref": "artifact:" + "c" * 64,
        "artifact_digest": "d" * 64,
        "destination": "docs/review.md",
        "transformation": "identity",
        "audience": "local-project",
    }
    expected_digest = hashlib.sha256(
        json.dumps(
            expected,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()

    assert commitment.digest == expected_digest
    assert commitment.to_dict() == {**expected, "digest": expected_digest}

    changed = (
        PublicationConsentCommitment.build(
            binding=replace(_binding(), public_run_id="run-2"),
            source=_source(), effect_id="accept-effect", descriptor=descriptor,
            producer_effect_id="producer-effect", artifact_ref="artifact:" + "c" * 64,
            artifact_digest="d" * 64,
        ),
        PublicationConsentCommitment.build(
            binding=replace(_binding(), project_identity="/other"),
            source=_source(), effect_id="accept-effect", descriptor=descriptor,
            producer_effect_id="producer-effect", artifact_ref="artifact:" + "c" * 64,
            artifact_digest="d" * 64,
        ),
        PublicationConsentCommitment.build(
            binding=replace(_binding(), recipe_digest="e" * 64),
            source=_source(), effect_id="accept-effect", descriptor=descriptor,
            producer_effect_id="producer-effect", artifact_ref="artifact:" + "c" * 64,
            artifact_digest="d" * 64,
        ),
        PublicationConsentCommitment.build(
            binding=_binding(), source=_source(interrupt_id="interrupt-2"),
            effect_id="accept-effect", descriptor=descriptor,
            producer_effect_id="producer-effect", artifact_ref="artifact:" + "c" * 64,
            artifact_digest="d" * 64,
        ),
        PublicationConsentCommitment.build(
            binding=_binding(), source=_source(), effect_id="accept-effect-2",
            descriptor=descriptor, producer_effect_id="producer-effect",
            artifact_ref="artifact:" + "c" * 64, artifact_digest="d" * 64,
        ),
        PublicationConsentCommitment.build(
            binding=_binding(), source=_source(), effect_id="accept-effect",
            descriptor=_accept_descriptor(destination="docs/other.md"),
            producer_effect_id="producer-effect", artifact_ref="artifact:" + "c" * 64,
            artifact_digest="d" * 64,
        ),
        PublicationConsentCommitment.build(
            binding=_binding(), source=_source(), effect_id="accept-effect",
            descriptor=descriptor, producer_effect_id="producer-effect-2",
            artifact_ref="artifact:" + "c" * 64, artifact_digest="d" * 64,
        ),
        PublicationConsentCommitment.build(
            binding=_binding(), source=_source(), effect_id="accept-effect",
            descriptor=descriptor, producer_effect_id="producer-effect",
            artifact_ref="artifact:" + "f" * 64, artifact_digest="d" * 64,
        ),
        PublicationConsentCommitment.build(
            binding=_binding(), source=_source(), effect_id="accept-effect",
            descriptor=descriptor, producer_effect_id="producer-effect",
            artifact_ref="artifact:" + "c" * 64, artifact_digest="f" * 64,
        ),
    )
    assert len({commitment.digest, *(item.digest for item in changed)}) == 10


def test_issue_stores_only_token_sha256_and_rejects_a_second_live_token(
    tmp_path,
) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied
    from lockstep.runtime.effects.owner_consent import OwnerConsentAuthority

    raw_token = "owner-bearer-token-NEVER-STORED-0123456789"
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    authority = OwnerConsentAuthority(
        store,
        delegate=object(),
        clock=lambda: datetime(2026, 8, 25, 12, tzinfo=UTC),
        token_factory=lambda: raw_token,
        consent_ref_factory=lambda: "consent:exact-1",
    )
    commitment = _commitment()
    try:
        issued = authority.issue(commitment)
        assert issued.consent_ref == "consent:exact-1"
        assert issued.token == raw_token
        assert issued.commitment_digest == commitment.digest
        assert issued.consent_epoch == 1
        assert authority.current_epoch("/project") == 1

        inspected = authority.inspect_token(raw_token)
        assert inspected.consent_ref == issued.consent_ref
        assert inspected.commitment == commitment
        assert inspected.consent_epoch == 1
        assert inspected.redeemed_at is None
        assert inspected.receipt_digest is None
        assert not hasattr(inspected, "token")

        table = store.tables.publication_consents
        with store.read_connection() as connection:
            row = connection.execute(select(table)).one()._mapping
            count = connection.scalar(select(func.count()).select_from(table))
        assert count == 1
        assert row["token_sha256"] == hashlib.sha256(raw_token.encode()).hexdigest()
        assert raw_token not in repr(dict(row))
        assert raw_token.encode() not in (tmp_path / "runtime.sqlite").read_bytes()

        with pytest.raises(EffectAuthorityDenied, match="already issued") as exc:
            authority.issue(commitment)
        assert raw_token not in str(exc.value)
        with store.read_connection() as connection:
            assert connection.scalar(select(func.count()).select_from(table)) == 1
    finally:
        store.close()


def test_redemption_is_exact_atomic_and_idempotent_under_concurrent_retry(
    tmp_path,
) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied
    from lockstep.runtime.effects.owner_consent import OwnerConsentAuthority

    token = "owner-token-exact-redemption"
    ticks = iter(
        datetime(2026, 8, 25, 12, tzinfo=UTC) + timedelta(seconds=index)
        for index in range(20)
    )
    clock_lock = threading.Lock()

    def clock() -> datetime:
        with clock_lock:
            return next(ticks)

    store = SQLiteStore(tmp_path / "runtime.sqlite")
    authority = OwnerConsentAuthority(
        store,
        delegate=object(),
        clock=clock,
        token_factory=lambda: token,
        consent_ref_factory=lambda: "consent:redeem-1",
    )
    commitment = _commitment()
    try:
        authority.issue(commitment)
        table = store.tables.publication_consents

        with store.read_connection() as connection:
            before = dict(connection.execute(select(table)).one()._mapping)
        mismatches = (
            replace(commitment, project_identity="/foreign", digest="0" * 64),
            replace(commitment, public_run_id="run-2", digest="1" * 64),
            replace(
                commitment,
                source=_source(interrupt_id="interrupt-2"),
                digest="2" * 64,
            ),
            replace(commitment, descriptor_digest="3" * 64, digest="3" * 64),
            replace(commitment, producer_effect_id="producer-2", digest="4" * 64),
            replace(commitment, artifact_ref="artifact:" + "5" * 64, digest="5" * 64),
            replace(commitment, artifact_digest="6" * 64, digest="6" * 64),
            replace(commitment, destination="docs/other.md", digest="7" * 64),
            replace(commitment, transformation="rewrite", digest="8" * 64),
            replace(commitment, audience="external", digest="9" * 64),
        )
        for invalid_token, candidate in (("wrong-token", commitment), *(
            (token, item) for item in mismatches
        )):
            with pytest.raises(EffectAuthorityDenied, match="invalid or stale") as exc:
                authority.redeem(invalid_token, candidate)
            assert invalid_token not in str(exc.value)
        with store.read_connection() as connection:
            assert dict(connection.execute(select(table)).one()._mapping) == before

        barrier = threading.Barrier(2)

        def redeem() -> object:
            barrier.wait()
            return authority.redeem(token, commitment)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(redeem)
            second_future = pool.submit(redeem)
            first = first_future.result(timeout=10)
            second = second_future.result(timeout=10)
        assert first == second == authority.redeem(token, commitment)
        assert first.to_dict() == {
            "schema": "lockstep.acceptance-result/v1",
            "effect_id": "accept-effect",
            "outcome": "PASS",
            "artifact_ref": "artifact:" + "c" * 64,
            "artifact_digest": "d" * 64,
            "destination": "docs/review.md",
            "transformation": "identity",
            "audience": "local-project",
            "consent_ref": "consent:redeem-1",
            "approval_generation": 1,
            "receipt_digest": first.receipt_digest,
        }
        inspected = authority.inspect_token(token)
        assert inspected.redeemed_at == datetime(2026, 8, 25, 12, 0, 1, tzinfo=UTC)
        expected_receipt = {
            "schema": "lockstep.publication-consent-receipt/v1",
            "consent_ref": "consent:redeem-1",
            "consent_epoch": 1,
            "commitment_digest": commitment.digest,
            "redeemed_at": "2026-08-25T12:00:01+00:00",
        }
        assert first.receipt_digest == hashlib.sha256(
            json.dumps(
                expected_receipt,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        with store.read_connection() as connection:
            row = connection.execute(select(table)).one()._mapping
        assert row["redeemed_at"] == "2026-08-25T12:00:01+00:00"
        assert row["receipt_digest"] == first.receipt_digest
    finally:
        store.close()


def test_revoke_advances_only_the_exact_project_epoch_and_stales_old_tokens(
    tmp_path,
) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied
    from lockstep.runtime.effects.owner_consent import OwnerConsentAuthority

    tokens = iter(("project-one-token", "project-two-token", "project-one-new-token"))
    refs = iter(("consent:p1-old", "consent:p2", "consent:p1-new"))
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    authority = OwnerConsentAuthority(
        store,
        delegate=object(),
        clock=lambda: datetime(2026, 8, 25, 12, tzinfo=UTC),
        token_factory=lambda: next(tokens),
        consent_ref_factory=lambda: next(refs),
    )
    one = _commitment()
    two = _commitment(
        binding=_binding(run_id="run-2", project="/project-two"),
        source=_source(thread_id="thread-2"),
    )
    try:
        issued_one = authority.issue(one)
        issued_two = authority.issue(two)
        authority.redeem(issued_one.token, one)
        authority.redeem(issued_two.token, two)

        assert authority.revoke("/project") == 2
        assert authority.current_epoch("/project") == 2
        assert authority.current_epoch("/project-two") == 1
        for operation in (
            lambda: authority.inspect_token(issued_one.token),
            lambda: authority.redeem(issued_one.token, one),
        ):
            with pytest.raises(EffectAuthorityDenied, match="invalid or stale"):
                operation()
        assert authority.inspect_token(issued_two.token).consent_epoch == 1
        assert authority.redeem(issued_two.token, two).approval_generation == 1

        replacement = authority.issue(one)
        assert replacement.consent_epoch == 2
        assert replacement.token == "project-one-new-token"
        assert authority.revoke("/never-seen") == 2
        assert authority.current_epoch("/never-seen") == 2
    finally:
        store.close()


def test_publish_grant_is_deterministic_exact_and_never_delegated(tmp_path) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied
    from lockstep.runtime.effects.owner_consent import OwnerConsentAuthority

    class RejectingDelegate:
        def resolve(self, _intent):
            pytest.fail("publish resolution delegated")

        @contextmanager
        def commitment(self, *_args):
            pytest.fail("publish commitment delegated")
            yield

    store = SQLiteStore(tmp_path / "runtime.sqlite")
    authority = OwnerConsentAuthority(
        store,
        delegate=RejectingDelegate(),
        clock=lambda: datetime(2026, 8, 25, 12, tzinfo=UTC),
        token_factory=lambda: "grant-token",
        consent_ref_factory=lambda: "consent:grant-1",
    )
    commitment = _commitment()
    try:
        result = authority.redeem(authority.issue(commitment).token, commitment)
        intent = _publish_intent(result)
        grant = authority.resolve(intent)
        assert authority.resolve(intent) == grant
        assert grant.intent_digest == intent.intent_digest
        assert grant.actor_binding_digest == "f" * 64
        assert grant.required_authorities == ("publication",)
        assert grant.workspace_ref is None
        assert grant.approval_generation == 1
        assert grant.grant_generation == 1
        assert grant.policy_epoch == 1
        assert grant.parent_capability_generation == 1
        assert grant.config_epoch == 0
        assert grant.expires_at == datetime.max.replace(tzinfo=UTC)

        item = dict(intent.inputs[0][1])
        mutations = (
            replace(intent, runner_selector="foreign-publisher"),
            replace(intent, required_capabilities=("publication", "workspace")),
            replace(intent, workspace_ref="workspace:forged"),
            _publish_intent(result, project_identity="/foreign"),
            _publish_intent(result, public_run_id="run-2"),
            _publish_intent(result, definition_digest="0" * 64),
            _publish_intent(
                result,
                inputs=(("item-0", {**item, "artifact_ref": "artifact:" + "0" * 64}),),
            ),
            _publish_intent(
                result,
                inputs=(("item-0", {**item, "artifact_blob": {"sha256": "0" * 64, "size": 17}}),),
            ),
            _publish_intent(
                result,
                inputs=(("item-0", {**item, "destination": "docs/other.md"}),),
            ),
            _publish_intent(
                result,
                inputs=(("item-0", {**item, "transformation": "rewrite"}),),
            ),
            _publish_intent(
                result,
                inputs=(("item-0", {**item, "audience": "external"}),),
            ),
            _publish_intent(
                result,
                inputs=(("item-0", {**item, "approval_generation": 2}),),
            ),
            _publish_intent(
                result,
                inputs=(("item-0", {**item, "receipt_digest": "0" * 64}),),
            ),
            _publish_intent(
                result,
                inputs=(
                    ("item-0", item),
                    ("item-1", item),
                ),
                writes=(result.destination, result.destination),
            ),
        )
        for changed in mutations:
            with pytest.raises(EffectAuthorityDenied, match="invalid|stale|exact"):
                authority.resolve(changed)
    finally:
        store.close()


def test_nonpublish_resolution_and_commitment_delegate_unchanged(tmp_path) -> None:
    from tests.runtime.providers.fakes import FakeEffectAuthority
    from lockstep.runtime.effects.owner_consent import OwnerConsentAuthority

    delegate = FakeEffectAuthority(clock=lambda: datetime(2026, 8, 25, 12, tzinfo=UTC))
    intent = EffectRequest.build(
        effect_id="managed-effect",
        public_run_id="run-1",
        project_identity="/project",
        definition_digest="a" * 64,
        coordinate=_source(),
        descriptor_digest="b" * 64,
        effect_kind="managed",
        runner_selector="codex",
        runner_binding_digest="c" * 64,
        required_capabilities=("workspace",),
        inputs=(("brief", {"task": "implement"}),),
        writes=("src/",),
        deadline_at=None,
    )
    expected = delegate.authorize(intent)
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    authority = OwnerConsentAuthority(store, delegate=delegate)
    try:
        grant = authority.resolve(intent)
        request = intent.bind_grant(grant)
        launch = PreparedLaunch(
            "managed-effect",
            request.request_digest,
            "c" * 64,
            "launch:managed-effect",
            workspace_ref=grant.workspace_ref,
        )
        with authority.commitment(grant, request, launch):
            pass
        assert grant == expected
        assert delegate.resolve_calls == [intent.intent_digest]
        assert delegate.commit_calls == [grant.digest]
    finally:
        store.close()


def test_publish_commitment_linearizes_with_revocation_and_rechecks_exact_grant(
    tmp_path,
) -> None:
    from lockstep.runtime.effects.authority import EffectAuthorityDenied
    from lockstep.runtime.effects.owner_consent import OwnerConsentAuthority

    store = SQLiteStore(tmp_path / "runtime.sqlite")
    authority = OwnerConsentAuthority(
        store,
        delegate=object(),
        clock=lambda: datetime(2026, 8, 25, 12, tzinfo=UTC),
        token_factory=lambda: "commit-token",
        consent_ref_factory=lambda: "consent:commit-1",
    )
    commitment = _commitment()
    try:
        result = authority.redeem(authority.issue(commitment).token, commitment)
        intent = _publish_intent(result)
        grant = authority.resolve(intent)
        request = intent.bind_grant(grant)
        prepared = PreparedPublication("1" * 64, "2" * 64, "f" * 64)

        entered = threading.Event()
        release = threading.Event()
        revoked = threading.Event()

        def hold_commitment() -> None:
            with authority.commitment(grant, request, prepared):
                entered.set()
                assert release.wait(10)

        def revoke() -> None:
            authority.revoke("/project")
            revoked.set()

        holding = threading.Thread(target=hold_commitment)
        holding.start()
        assert entered.wait(10)
        revoking = threading.Thread(target=revoke)
        revoking.start()
        assert not revoked.wait(0.1)
        release.set()
        holding.join(10)
        revoking.join(10)
        assert revoked.is_set()
        assert authority.current_epoch("/project") == 2

        with pytest.raises(EffectAuthorityDenied, match="invalid|stale|exact"):
            authority.commitment(grant, request, prepared).__enter__()
        for bad_grant, bad_request, bad_launch in (
            (replace(grant, digest="0" * 64), request, prepared),
            (grant, replace(request, grant_digest="0" * 64), prepared),
            (grant, request, replace(prepared, publisher_binding_digest="0" * 64)),
        ):
            with pytest.raises(EffectAuthorityDenied, match="invalid|stale|exact"):
                with authority.commitment(bad_grant, bad_request, bad_launch):
                    pass
    finally:
        store.close()
