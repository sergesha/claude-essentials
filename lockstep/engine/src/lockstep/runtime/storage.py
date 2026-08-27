"""SQLAlchemy Core schema for Lockstep-owned runtime facts.

This module is the only place runtime SQL tables are declared.  In particular,
the run catalog is deliberately limited to immutable discovery data; native
LangGraph state remains in the configured saver.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    inspect as sa_inspect,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url

from lockstep.runtime.owner_state import (
    initialize_owner_state,
    seal_owner_file,
    verify_owner_file,
)


@dataclass(frozen=True, slots=True)
class LegacyRunDriveClassification:
    public_run_id: str
    disposition: Literal["nonterminal", "terminal", "malformed"]

    def __post_init__(self) -> None:
        if type(self.public_run_id) is not str or not self.public_run_id:
            raise ValueError("public_run_id must be a non-empty string")
        if type(self.disposition) is not str or self.disposition not in {
            "nonterminal",
            "terminal",
            "malformed",
        }:
            raise ValueError("disposition must be nonterminal, terminal, or malformed")


@dataclass(frozen=True, slots=True)
class MigrationProgress:
    after_public_run_id: str | None
    completed: bool
    inserted_public_run_ids: tuple[str, ...]
    malformed_public_run_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.completed) is not bool:
            raise TypeError("completed must be a boolean")
        if self.after_public_run_id is not None and (
            type(self.after_public_run_id) is not str
            or not self.after_public_run_id
        ):
            raise ValueError("after_public_run_id must be a non-empty string")
        for name, values in (
            ("inserted_public_run_ids", self.inserted_public_run_ids),
            ("malformed_public_run_ids", self.malformed_public_run_ids),
        ):
            if type(values) is not tuple:
                raise TypeError(f"{name} must be a tuple")
            if any(type(value) is not str or not value for value in values):
                raise ValueError(f"{name} must contain non-empty strings")
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{name} must be sorted and unique")
        if not set(self.inserted_public_run_ids).isdisjoint(
            self.malformed_public_run_ids
        ):
            raise ValueError("migration progress result IDs must be disjoint")
        if (
            len(self.inserted_public_run_ids) + len(self.malformed_public_run_ids)
            > 128
        ):
            raise ValueError(
                "migration progress result IDs must contain at most 128 entries"
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


class RuntimeSchemaMigrator:
    """Private owner-state schema migration boundary."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def apply_run_drive_watch_page(
        self,
        *,
        expected_after_public_run_id: str | None,
        classified: tuple[LegacyRunDriveClassification, ...],
        exhausted: bool,
    ) -> MigrationProgress:
        raise NotImplementedError(
            "run-drive-watch migration behavior is staged in R2"
        )


class SQLiteStore:
    """Owner of the small Lockstep SQL schema and transaction boundaries."""

    def __init__(self, path: str | Path) -> None:
        raw = str(path)
        self.database_path: Path | None = None
        if raw == ":memory:":
            url = "sqlite+pysqlite:///:memory:"
        elif isinstance(path, str) and "://" in raw:
            parsed = make_url(raw)
            if parsed.get_backend_name() != "sqlite":
                raise ValueError("SQLiteStore accepts only SQLite URLs")
            url = raw
            if parsed.database not in (None, "", ":memory:"):
                self.database_path = Path(parsed.database)
        else:
            db_path = Path(path)
            self.database_path = db_path
            url = f"sqlite+pysqlite:///{db_path}"
        if self.database_path is not None:
            initialize_owner_state(self.database_path.parent)
            self._verify_sqlite_files()
        self.engine: Engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        existing_tables = set(sa_inspect(self.engine).get_table_names())
        if existing_tables and "runtime_schema_epoch" not in existing_tables:
            self.engine.dispose()
            raise RuntimeError(
                "runtime schema migration is required before opening this database"
            )
        self.metadata = MetaData()
        # Runtime-input facts are deliberately not part of the effect/catalog
        # schema metadata.  They share the transaction engine while retaining
        # their own neutral, append-only schema boundary.
        self.external_fact_metadata = MetaData()
        self.tables = _define_tables(self.metadata, self.external_fact_metadata)
        self.metadata.create_all(self.engine)
        self.external_fact_metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                self.tables.runtime_schema_epoch.insert()
                .prefix_with("OR IGNORE")
                .values(singleton=1, epoch=2)
            )
        self._seal_sqlite_files()

    def _sqlite_files(self) -> tuple[Path, ...]:
        if self.database_path is None:
            return ()
        path = self.database_path
        return (path, Path(f"{path}-journal"), Path(f"{path}-wal"), Path(f"{path}-shm"))

    def _verify_sqlite_files(self) -> None:
        for path in self._sqlite_files():
            if path.exists() or path.is_symlink():
                try:
                    verify_owner_file(path)
                except FileNotFoundError:
                    if path == self.database_path:
                        raise

    def _seal_sqlite_files(self) -> None:
        for path in self._sqlite_files():
            if path.exists():
                try:
                    seal_owner_file(path, writable=True)
                except FileNotFoundError:
                    if path == self.database_path:
                        raise

    @contextmanager
    def write_transaction(self) -> Iterator[Connection]:
        """Serialize SQLite read/compare/write operations with BEGIN IMMEDIATE."""

        self._verify_sqlite_files()
        connection = self.engine.connect()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._seal_sqlite_files()

    @contextmanager
    def read_connection(self) -> Iterator[Connection]:
        """Open a read connection only after rechecking the local state boundary."""

        self._verify_sqlite_files()
        connection = self.engine.connect()
        try:
            yield connection
        finally:
            connection.close()
            self._seal_sqlite_files()

    @contextmanager
    def _v2_write_transaction(self) -> Iterator[Connection]:
        """Staged surface; exact epoch fencing follows in its own cycle."""

        with self.write_transaction() as connection:
            yield connection

    def close(self) -> None:
        self.engine.dispose()
        self._seal_sqlite_files()


# Common spelling retained as an import alias, not a second implementation.
SqliteStore = SQLiteStore
