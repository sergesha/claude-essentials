"""Private SQLAlchemy Core schema for Lockstep-owned runtime facts."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)


@dataclass(frozen=True)
class RuntimeTables:
    runs: Table
    run_start_inputs: Table
    effect_runtime_inputs: Table
    consent_epochs: Table
    publication_consents: Table
    leases: Table
    effects: Table
    effect_observations: Table
    run_drive_watches: Table
    runtime_schema_migrations: Table
    runtime_schema_epoch: Table


def _define_run_drive_tables(
    metadata: MetaData,
) -> tuple[Table, Table, Table]:
    run_drive_watches = Table(
        "run_drive_watches",
        metadata,
        Column("admission_seq", Integer, primary_key=True, autoincrement=True),
        Column(
            "public_run_id",
            String,
            ForeignKey("runs.public_run_id"),
            nullable=False,
            unique=True,
        ),
        Column("input_blob_sha256", String(64), nullable=True),
        Column("input_blob_size", Integer, nullable=True),
        Column("admitted_at", String, nullable=False),
        CheckConstraint(
            "((input_blob_sha256 IS NULL AND input_blob_size IS NULL) "
            "OR (input_blob_sha256 IS NOT NULL AND input_blob_size IS NOT NULL))",
            name="ck_run_drive_watch_input_blob_pair",
        ),
        sqlite_autoincrement=True,
    )
    runtime_schema_migrations = Table(
        "runtime_schema_migrations",
        metadata,
        Column("name", String, primary_key=True),
        Column("schema_version", Integer, nullable=False),
        Column("after_public_run_id", String, nullable=True),
        Column("completed_at", String, nullable=True),
        Column("updated_at", String, nullable=False),
    )
    runtime_schema_epoch = Table(
        "runtime_schema_epoch",
        metadata,
        Column("singleton", Integer, primary_key=True),
        Column("epoch", Integer, nullable=False),
        CheckConstraint(
            "singleton = 1",
            name="ck_runtime_schema_epoch_singleton",
        ),
    )
    return run_drive_watches, runtime_schema_migrations, runtime_schema_epoch


def _define_tables(metadata: MetaData, external_metadata: MetaData) -> RuntimeTables:
    runs = Table(
        "runs",
        metadata,
        Column("public_run_id", String, primary_key=True),
        Column("thread_id", String, nullable=False),
        Column("recipe_digest", String(64), nullable=False),
        Column("recipe_snapshot_ref", String, nullable=False),
        Column("project_identity", String, nullable=False),
        Column("created_at", String, nullable=False),
        UniqueConstraint("thread_id", name="uq_runs_thread_id"),
    )
    run_start_inputs = Table(
        "run_start_inputs",
        external_metadata,
        Column(
            "public_run_id",
            String,
            ForeignKey(runs.c.public_run_id),
            primary_key=True,
        ),
        Column("runtime_key", String, primary_key=True),
        Column("snapshot_ref", String(64), nullable=False),
        Column("project_identity", String, nullable=False),
        Column("definition_digest", String(64), nullable=False),
        Column("created_at", String, nullable=False),
    )
    effect_runtime_inputs = Table(
        "effect_runtime_inputs",
        external_metadata,
        Column("effect_id", String, primary_key=True),
        Column("runtime_key", String, primary_key=True),
        Column("public_run_id", String, ForeignKey(runs.c.public_run_id), nullable=False),
        Column("thread_id", String, nullable=False),
        Column("checkpoint_ns", String, nullable=False),
        Column("checkpoint_id", String, nullable=False),
        Column("task_id", String, nullable=False),
        Column("interrupt_id", String, nullable=False),
        Column("descriptor_digest", String(64), nullable=False),
        Column("snapshot_ref", String(64), nullable=False),
        Column("created_at", String, nullable=False),
    )
    consent_epochs = Table(
        "consent_epochs",
        metadata,
        Column("project_identity", String, primary_key=True),
        Column("epoch", Integer, nullable=False),
        Column("updated_at", String, nullable=False),
    )
    publication_consents = Table(
        "publication_consents",
        metadata,
        Column("consent_ref", String, primary_key=True),
        Column("token_sha256", String(64), nullable=False, unique=True),
        Column("project_identity", String, nullable=False),
        Column("public_run_id", String, nullable=False),
        Column("definition_digest", String(64), nullable=False),
        Column("source_thread_id", String, nullable=False),
        Column("source_checkpoint_ns", String, nullable=False),
        Column("source_checkpoint_id", String, nullable=False),
        Column("source_task_id", String, nullable=False),
        Column("source_interrupt_id", String, nullable=False),
        Column("effect_id", String, nullable=False),
        Column("descriptor_digest", String(64), nullable=False),
        Column("producer_effect_id", String, nullable=False),
        Column("artifact_ref", String, nullable=False),
        Column("artifact_digest", String(64), nullable=False),
        Column("destination", String, nullable=False),
        Column("transformation", String, nullable=False),
        Column("audience", String, nullable=False),
        Column("commitment_digest", String(64), nullable=False),
        Column("consent_epoch", Integer, nullable=False),
        Column("issued_at", String, nullable=False),
        Column("redeemed_at", String, nullable=True),
        Column("receipt_digest", String(64), nullable=True, unique=True),
        UniqueConstraint(
            "project_identity",
            "consent_epoch",
            "commitment_digest",
            name="uq_publication_consents_exact_epoch",
        ),
    )
    leases = Table(
        "leases",
        metadata,
        Column("scope", String, primary_key=True),
        Column("lease_key", String, primary_key=True),
        Column("owner", String, nullable=False),
        Column("epoch", Integer, nullable=False),
        Column("expires_at", String, nullable=False),
        Column("acquired_at", String, nullable=False),
    )
    effects = Table(
        "effects",
        metadata,
        Column("effect_id", String, primary_key=True),
        Column("thread_id", String, nullable=False),
        Column("checkpoint_ns", String, nullable=False),
        Column("checkpoint_id", String, nullable=False),
        Column("task_id", String, nullable=False),
        Column("interrupt_id", String, nullable=False),
        Column("descriptor_digest", String(64), nullable=False),
        Column("effect_kind", String, nullable=False),
        Column("deadline_at", String, nullable=True),
        Column("phase", String, nullable=False),
        Column("lease_epoch", Integer, nullable=False),
        Column("runner_binding_digest", String(64), nullable=True),
        Column("workspace_ref", String, nullable=True),
        Column("request_digest", String(64), nullable=True),
        Column("grant_digest", String(64), nullable=True),
        Column("launch_commitment_digest", String(64), nullable=True),
        Column("result_ref", String, nullable=True),
        Column("fixed_error_code", String, nullable=True),
        Column("created_at", String, nullable=False),
        Column("updated_at", String, nullable=False),
        Column("revision", Integer, nullable=False),
        UniqueConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "task_id",
            "interrupt_id",
            name="uq_effects_native_coordinate",
        ),
    )
    effect_observations = Table(
        "effect_observations",
        metadata,
        Column("effect_id", String, ForeignKey("effects.effect_id"), primary_key=True),
        Column("revision", Integer, primary_key=True),
        Column("phase", String, nullable=False),
        Column("result_json", String, nullable=True),
        Column("observed_at", String, nullable=False),
    )
    (
        run_drive_watches,
        runtime_schema_migrations,
        runtime_schema_epoch,
    ) = _define_run_drive_tables(
        metadata,
    )
    return RuntimeTables(
        runs=runs,
        run_start_inputs=run_start_inputs,
        effect_runtime_inputs=effect_runtime_inputs,
        consent_epochs=consent_epochs,
        publication_consents=publication_consents,
        leases=leases,
        effects=effects,
        effect_observations=effect_observations,
        run_drive_watches=run_drive_watches,
        runtime_schema_migrations=runtime_schema_migrations,
        runtime_schema_epoch=runtime_schema_epoch,
    )
