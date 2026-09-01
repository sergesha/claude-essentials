"""Production-backed cases for the B1 runtime-schema writer fence matrix."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.effects.descriptors import (
    parse_effect_descriptor,
    parse_effect_result,
)
from lockstep.runtime.effects.ledger import EffectLedger
from lockstep.runtime.effects.owner_consent import OwnerConsentAuthority
from lockstep.runtime.leases import LeaseStore
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.project_snapshots import ProjectSnapshotRef, ProjectSnapshotStore
from lockstep.runtime.publication import PreparedPublication
from lockstep.runtime.snapshot_resolver import RuntimeSnapshotFacts
from lockstep.runtime.storage import RuntimeSchemaMigrator, SQLiteStore
from tests.runtime._sqlite_store_image import StoreImage


EXPECTED_FENCE_ERROR = "runtime schema epoch 2 is required for v2 writes"


@dataclass(frozen=True)
class FenceCase:
    action: Callable[[], object]
    entered: Callable[[], bool] = lambda: False


def prepare_epoch_one(store: SQLiteStore) -> None:
    with store._v2_write_transaction():
        pass
    epoch = store.tables.runtime_schema_epoch
    with store.engine.begin() as connection:
        connection.execute(epoch.update().values(epoch=1))
    store.engine.dispose()


def observe_action(case: FenceCase, store: SQLiteStore) -> dict[str, object]:
    database = store.database_path
    if database is None:
        raise TypeError("writer fence requires a file-backed SQLite store")
    before = StoreImage.capture(database)
    error: BaseException | None = None
    try:
        case.action()
    except BaseException as exc:  # the exact fail-closed type is part of the oracle
        error = exc
    store.engine.dispose()
    after = StoreImage.capture(database)
    return {
        "error": None if error is None else f"{type(error).__name__}: {error}",
        "logical_rows_unchanged": after.logical_rows == before.logical_rows,
        "sqlite_family_unchanged": after.sqlite_family == before.sqlite_family,
        "authority_entered": case.entered(),
    }


def _binding(run_id: str = "writer-fence-run") -> RunBinding:
    return RunBinding(
        run_id,
        f"thread-{run_id}",
        "a" * 64,
        "bundle:" + "b" * 64,
        "/project",
    )


def _seed_admission(store: SQLiteStore, run_id: str = "writer-fence-run"):
    ledger = EffectLedger(store)
    binding = _binding(run_id)
    input_blob = _blob_store(store).put(b"{}")
    ledger.admit_start(RunCatalog(store), binding, input_blob)
    return ledger, binding


def _blob_store(store: SQLiteStore) -> BlobStore:
    return BlobStore(_owner_dir(store))


def _owner_dir(store: SQLiteStore) -> Path:
    database = store.database_path
    if database is None:
        raise TypeError("writer fence requires a file-backed SQLite store")
    return database.parent


def admission_case(store: SQLiteStore) -> FenceCase:
    ledger = EffectLedger(store)
    catalog = RunCatalog(store)
    binding = _binding()
    facts = RuntimeSnapshotFacts(store)
    blobs = _blob_store(store)
    input_blob = blobs.put(b"{}")
    snapshot_ref = ProjectSnapshotStore(_owner_dir(store), blobs).capture(
        {"src/app.py": blobs.put(b"VALUE = 1\n")},
        declared_paths=("src/app.py",),
        provenance={"purpose": "writer-fence-run-start"},
    )
    return FenceCase(
        lambda: ledger.admit_start(
            catalog,
            binding,
            input_blob,
            on_admit=lambda connection, admitted: facts.bind_run_start_in_transaction(
                connection, admitted, snapshot_ref
            ),
        )
    )


def storage_watch_delete_case(store: SQLiteStore) -> FenceCase:
    ledger, binding = _seed_admission(store)
    return FenceCase(lambda: ledger.acknowledge_run_drive_watch(binding.public_run_id))


def recovery_repair_case(store: SQLiteStore) -> FenceCase:
    migrator = RuntimeSchemaMigrator(store)
    return FenceCase(
        lambda: migrator.apply_run_drive_watch_page(
            expected_after_public_run_id=None,
            classified=(),
            exhausted=False,
        )
    )


def _manual_descriptor():
    return parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "manual",
            "logical_id": "writer-fence-effect",
            "runner": None,
            "inputs": {},
            "writes": ["src/"],
            "artifacts": [],
            "deadline_seconds": None,
            "scope_state_keys": [],
            "result_schema": "lockstep.effect-result/v1",
        }
    )


def _coordinate() -> NativeCoordinate:
    return NativeCoordinate("thread", "checkpoint", "ns", "task", "interrupt")


def _prepare_effect(ledger: EffectLedger):
    return ledger.prepare(
        _coordinate(),
        _manual_descriptor(),
        deadline_at=None,
        runner_binding_digest=None,
        workspace_ref=None,
    )


def effect_prepare_case(store: SQLiteStore) -> FenceCase:
    ledger = EffectLedger(store)
    return FenceCase(lambda: _prepare_effect(ledger))


def effect_transition_case(store: SQLiteStore) -> FenceCase:
    ledger = EffectLedger(store)
    prepared = _prepare_effect(ledger)
    result = parse_effect_result(
        {
            "schema": "lockstep.effect-result/v1",
            "effect_id": prepared.effect_id,
            "outcome": "PASS",
            "result_ref": "blob:" + "e" * 64,
            "artifact_refs": [],
            "snapshot_ref": None,
            "diff_ref": None,
            "fixed_error_code": None,
            "evidence_refs": [],
        }
    )
    return FenceCase(
        lambda: ledger.seal(
            prepared.effect_id,
            result,
            expected_revision=prepared.revision,
        )
    )


def effect_runtime_input_case(store: SQLiteStore) -> FenceCase:
    facts = RuntimeSnapshotFacts(store)
    descriptor = _manual_descriptor()
    return FenceCase(
        lambda: facts.bind_effect(
            "effect-runtime-input",
            "current_project_snapshot",
            _binding(),
            _coordinate(),
            descriptor.digest,
            ProjectSnapshotRef("f" * 64),
        )
    )


def lease_acquire_case(store: SQLiteStore) -> FenceCase:
    leases = LeaseStore(store)
    return FenceCase(lambda: leases.acquire("effect", "effect-1", "owner-1", 30))


def lease_release_case(store: SQLiteStore) -> FenceCase:
    leases = LeaseStore(store)
    lease = leases.acquire("effect", "effect-1", "owner-1", 30)
    return FenceCase(lambda: leases.release(lease))


def _commitment():
    from lockstep.runtime.effects.models import AcceptDescriptor
    from lockstep.runtime.effects.owner_consent import PublicationConsentCommitment

    descriptor = parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "accept",
            "logical_id": "accept-review",
            "artifact_handle": "review.report",
            "producer_result_state_key": "review_result",
            "declared_name": "report",
            "destination": "docs/review.md",
            "transformation": "identity",
            "audience": "local-project",
            "verdict": "PASS",
            "result_schema": "lockstep.acceptance-result/v1",
        }
    )
    if not isinstance(descriptor, AcceptDescriptor):
        raise TypeError("accept descriptor builder returned the wrong type")
    return PublicationConsentCommitment.build(
        binding=_binding("run-1"),
        source=NativeCoordinate(
            "thread-run-1", "checkpoint-1", "child", "task-1", "interrupt-1"
        ),
        effect_id="accept-effect",
        descriptor=descriptor,
        producer_effect_id="producer-effect",
        artifact_ref="artifact:" + "1" * 64,
        artifact_digest="2" * 64,
    )


def _authority(store: SQLiteStore) -> OwnerConsentAuthority:
    return OwnerConsentAuthority(
        store,
        delegate=object(),
        clock=lambda: datetime(2026, 8, 27, 12, tzinfo=UTC),
        token_factory=lambda: "writer-fence-token",
        consent_ref_factory=lambda: "consent:writer-fence",
    )


def consent_issue_case(store: SQLiteStore) -> FenceCase:
    authority = _authority(store)
    commitment = _commitment()
    return FenceCase(lambda: authority.issue(commitment))


def consent_redeem_case(store: SQLiteStore) -> FenceCase:
    authority = _authority(store)
    commitment = _commitment()
    issued = authority.issue(commitment)
    return FenceCase(lambda: authority.redeem(issued.token, commitment))


def consent_revoke_case(store: SQLiteStore) -> FenceCase:
    authority = _authority(store)
    return FenceCase(lambda: authority.revoke("/project"))


def _publish_intent(result):
    from lockstep.runtime.providers.base import EffectRequest

    return EffectRequest.build(
        effect_id="publish-effect",
        public_run_id="run-1",
        project_identity="/project",
        definition_digest="a" * 64,
        coordinate=NativeCoordinate(
            "thread-run-1",
            "publish-checkpoint",
            "child",
            "publish-task",
            "publish-interrupt",
        ),
        descriptor_digest="3" * 64,
        effect_kind="publish",
        runner_selector="project-publisher",
        runner_binding_digest="4" * 64,
        required_capabilities=("publication",),
        inputs=((
            "item-0",
            {
                "artifact_ref": result.artifact_ref,
                "artifact_blob": {"sha256": result.artifact_digest, "size": 17},
                "destination": result.destination,
                "transformation": result.transformation,
                "audience": result.audience,
                "consent_ref": result.consent_ref,
                "approval_generation": result.approval_generation,
                "receipt_digest": result.receipt_digest,
            },
        ),),
        writes=(result.destination,),
        deadline_at=None,
    )


def consent_commitment_case(store: SQLiteStore) -> FenceCase:
    authority = _authority(store)
    commitment = _commitment()
    result = authority.redeem(authority.issue(commitment).token, commitment)
    intent = _publish_intent(result)
    grant = authority.resolve(intent)
    request = intent.bind_grant(grant)
    prepared = PreparedPublication("5" * 64, "6" * 64, "4" * 64)
    entered = [False]

    def enter_authority() -> None:
        with authority.commitment(grant, request, prepared):
            entered[0] = True

    return FenceCase(
        enter_authority,
        entered=lambda: entered[0],
    )


CASE_FACTORIES = {
    "admission-ledger": admission_case,
    "watch-delete-v2-storage-control": storage_watch_delete_case,
    "recovery-repair-v2-storage-control": recovery_repair_case,
    "effect-prepare": effect_prepare_case,
    "effect-transition": effect_transition_case,
    "effect-runtime-input": effect_runtime_input_case,
    "effect-lease-acquire": lease_acquire_case,
    "effect-lease-release": lease_release_case,
    "consent-issue": consent_issue_case,
    "consent-redeem": consent_redeem_case,
    "consent-revoke": consent_revoke_case,
    "consent-publication-commitment": consent_commitment_case,
}
