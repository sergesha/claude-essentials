"""Private runtime schema migration boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.engine import Connection

from lockstep.runtime._storage_schema import RuntimeTables, _define_tables
from lockstep.runtime.advisory_lock import advisory_file_lock
from lockstep.runtime.owner_state import seal_owner_file, verify_owner_file

if TYPE_CHECKING:
    from lockstep.runtime.storage import SQLiteStore


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


_RUN_DRIVE_WATCH_MIGRATION = "run-drive-watch-v2"
_LEGACY_RUN_DRIVE_WATCH_TABLE = "effect_dispatch_watches"
_MAX_RUN_INPUT_BLOB_BYTES = 64 * 1024 * 1024

_SchemaManifest = tuple[tuple[str, str, str, str | None], ...]


def _define_legacy_run_drive_watch(metadata: MetaData) -> Table:
    return Table(
        _LEGACY_RUN_DRIVE_WATCH_TABLE,
        metadata,
        Column(
            "public_run_id",
            String,
            ForeignKey("runs.public_run_id"),
            primary_key=True,
        ),
        Column("input_blob_sha256", String(64), nullable=False),
        Column("input_blob_size", Integer, nullable=False),
        Column("admitted_at", String, nullable=False),
    )


def _schema_manifest(connection: Connection) -> _SchemaManifest:
    return tuple(
        tuple(row)
        for row in connection.exec_driver_sql(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name, tbl_name, sql"
        )
    )


@lru_cache(maxsize=2)
def _expected_schema_manifest(*, legacy: bool) -> _SchemaManifest:
    metadata = MetaData()
    external_metadata = MetaData()
    tables = _define_tables(metadata, external_metadata)
    legacy_table = _define_legacy_run_drive_watch(metadata) if legacy else None
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            if legacy:
                excluded = {
                    tables.run_drive_watches,
                    tables.runtime_schema_migrations,
                    tables.runtime_schema_epoch,
                }
                metadata.create_all(
                    connection,
                    tables=tuple(
                        table
                        for table in metadata.sorted_tables
                        if table not in excluded
                    ),
                )
                assert legacy_table is not None
            else:
                metadata.create_all(connection)
            external_metadata.create_all(connection)
            return _schema_manifest(connection)
    finally:
        engine.dispose()


def _validate_database_integrity(connection: Connection) -> None:
    if tuple(connection.exec_driver_sql("PRAGMA integrity_check")) != (("ok",),):
        raise RuntimeError("runtime database integrity check failed")
    if tuple(connection.exec_driver_sql("PRAGMA foreign_key_check")):
        raise RuntimeError("runtime database foreign-key integrity check failed")


def _validate_v2_epoch(connection: Connection) -> None:
    rows = tuple(
        connection.exec_driver_sql(
            "SELECT singleton, epoch FROM runtime_schema_epoch ORDER BY singleton"
        )
    )
    if rows != ((1, 2),):
        raise RuntimeError("runtime schema epoch is not exact v2")


def _validate_watch_values(
    connection: Connection, *, table_name: str, legacy: bool
) -> None:
    sequence = "NULL" if legacy else "admission_seq"
    rows = connection.exec_driver_sql(
        f"SELECT {sequence}, public_run_id, input_blob_sha256, "
        f"input_blob_size, admitted_at FROM {table_name}"
    )
    for row in rows:
        admission_seq, public_run_id, digest, size, admitted_at = tuple(row)
        if not legacy and (type(admission_seq) is not int or admission_seq <= 0):
            raise RuntimeError("runtime drive watch contains invalid values")
        if type(public_run_id) is not str or not public_run_id:
            raise RuntimeError("runtime drive watch contains invalid values")
        if digest is None:
            if legacy or size is not None:
                raise RuntimeError("runtime drive watch contains invalid values")
        elif (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or type(size) is not int
            or size < 0
            or size > _MAX_RUN_INPUT_BLOB_BYTES
        ):
            raise RuntimeError("runtime drive watch contains invalid values")
        if type(admitted_at) is not str:
            raise RuntimeError("runtime drive watch timestamp is invalid")
        try:
            timestamp = datetime.fromisoformat(admitted_at)
        except ValueError as exc:
            raise RuntimeError("runtime drive watch timestamp is invalid") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise RuntimeError("runtime drive watch timestamp is invalid")


def _populate_v2_run_drive_schema(
    connection: Connection, tables: RuntimeTables
) -> None:
    connection.exec_driver_sql(
        "INSERT INTO run_drive_watches "
        "(public_run_id, input_blob_sha256, input_blob_size, admitted_at) "
        "SELECT public_run_id, input_blob_sha256, input_blob_size, admitted_at "
        f"FROM {_LEGACY_RUN_DRIVE_WATCH_TABLE} ORDER BY public_run_id"
    )
    connection.execute(
        tables.runtime_schema_epoch.insert().values(singleton=1, epoch=2)
    )
    connection.exec_driver_sql(f"DROP TABLE {_LEGACY_RUN_DRIVE_WATCH_TABLE}")


def _sqlite_family(path: Path) -> tuple[Path, ...]:
    return (
        path,
        Path(f"{path}-journal"),
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    )


def _verify_sqlite_family(path: Path) -> None:
    for candidate in _sqlite_family(path):
        if candidate.exists() or candidate.is_symlink():
            try:
                verify_owner_file(candidate)
            except FileNotFoundError:
                if candidate == path:
                    raise


def _seal_sqlite_family(path: Path) -> None:
    for candidate in _sqlite_family(path):
        if candidate.exists():
            try:
                seal_owner_file(candidate, writable=True)
            except FileNotFoundError:
                if candidate == path:
                    raise


def _validate_run_drive_watch_page_envelope(
    *,
    expected_after_public_run_id: str | None,
    classified: tuple[LegacyRunDriveClassification, ...],
    exhausted: bool,
) -> tuple[str, ...]:
    if expected_after_public_run_id is not None and (
        type(expected_after_public_run_id) is not str
        or not expected_after_public_run_id
    ):
        raise ValueError("expected_after_public_run_id must be a non-empty string")
    if type(classified) is not tuple:
        raise TypeError("classified must be a tuple")
    if any(
        not isinstance(record, LegacyRunDriveClassification) for record in classified
    ):
        raise TypeError(
            "classified must contain LegacyRunDriveClassification records"
        )
    if len(classified) > 128:
        raise ValueError("classified must contain at most 128 records")
    public_run_ids = tuple(record.public_run_id for record in classified)
    if public_run_ids != tuple(sorted(set(public_run_ids))):
        raise ValueError("classified public_run_ids must be sorted and unique")
    if (
        expected_after_public_run_id is not None
        and public_run_ids
        and public_run_ids[0] <= expected_after_public_run_id
    ):
        raise ValueError(
            "classified public_run_ids must be strictly after "
            "expected_after_public_run_id"
        )
    if type(exhausted) is not bool:
        raise TypeError("exhausted must be a boolean")
    return public_run_ids


def _validate_existing_run_drive_watch_migration(
    *,
    schema_version: int,
    stored_after_public_run_id: str | None,
    expected_after_public_run_id: str | None,
    completed_at: str | None,
) -> None:
    if schema_version != 2:
        raise RuntimeError("run-drive-watch migration schema version must be 2")
    if stored_after_public_run_id != expected_after_public_run_id:
        raise RuntimeError("run-drive-watch migration cursor mismatch")
    if completed_at is not None:
        raise RuntimeError("run-drive-watch migration is already completed")


class RuntimeSchemaMigrator:
    """Private owner-state schema migration boundary."""

    @classmethod
    def transition_legacy_to_v2(cls, path: Path) -> None:
        """Classify an existing store and atomically publish exact epoch 2."""

        path = Path(path)
        if not path.exists() and not path.is_symlink():
            return
        verify_owner_file(path)
        if path.stat().st_size == 0:
            return
        metadata = MetaData()
        external_metadata = MetaData()
        tables = _define_tables(metadata, external_metadata)
        engine = create_engine(
            f"sqlite+pysqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        with advisory_file_lock(path.parent / "runtime-schema.lock"):
            try:
                _verify_sqlite_family(path)
                with engine.connect() as connection:
                    try:
                        connection.exec_driver_sql("BEGIN EXCLUSIVE")
                        manifest = _schema_manifest(connection)
                        if manifest == _expected_schema_manifest(legacy=False):
                            _validate_database_integrity(connection)
                            _validate_v2_epoch(connection)
                            _validate_watch_values(
                                connection,
                                table_name="run_drive_watches",
                                legacy=False,
                            )
                            connection.rollback()
                            return
                        if manifest != _expected_schema_manifest(legacy=True):
                            raise RuntimeError(
                                "runtime database schema is neither legacy nor v2"
                            )
                        _validate_database_integrity(connection)
                        _validate_watch_values(
                            connection,
                            table_name=_LEGACY_RUN_DRIVE_WATCH_TABLE,
                            legacy=True,
                        )
                        metadata.create_all(
                            connection,
                            tables=(
                                tables.run_drive_watches,
                                tables.runtime_schema_migrations,
                                tables.runtime_schema_epoch,
                            ),
                        )
                        _populate_v2_run_drive_schema(connection, tables)
                        if _schema_manifest(connection) != (
                            _expected_schema_manifest(legacy=False)
                        ):
                            raise RuntimeError(
                                "runtime schema transition produced noncanonical v2"
                            )
                        _validate_database_integrity(connection)
                        _validate_v2_epoch(connection)
                        _validate_watch_values(
                            connection,
                            table_name="run_drive_watches",
                            legacy=False,
                        )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
            finally:
                engine.dispose()
                _seal_sqlite_family(path)

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def run_drive_watch_migration_state(self) -> MigrationProgress | None:
        """Read only the durable schema-upgrade cursor and completion fact."""

        table = self._store.tables.runtime_schema_migrations
        with self._store.read_connection() as connection:
            row = connection.execute(
                select(table).where(table.c.name == _RUN_DRIVE_WATCH_MIGRATION)
            ).first()
        if row is None:
            return None
        if row.schema_version != 2:
            raise RuntimeError("run-drive-watch migration schema version must be 2")
        return MigrationProgress(
            row.after_public_run_id,
            row.completed_at is not None,
            (),
            (),
        )

    def apply_run_drive_watch_page(
        self,
        *,
        expected_after_public_run_id: str | None,
        classified: tuple[LegacyRunDriveClassification, ...],
        exhausted: bool,
    ) -> MigrationProgress:
        public_run_ids = _validate_run_drive_watch_page_envelope(
            expected_after_public_run_id=expected_after_public_run_id,
            classified=classified,
            exhausted=exhausted,
        )
        with self._store._v2_write_transaction() as connection:
            progress = self._apply_validated_page_in_transaction(
                connection,
                expected_after_public_run_id=expected_after_public_run_id,
                classified=classified,
                public_run_ids=public_run_ids,
                exhausted=exhausted,
            )
        return progress

    def _apply_validated_page_in_transaction(
        self,
        connection: Connection,
        *,
        expected_after_public_run_id: str | None,
        classified: tuple[LegacyRunDriveClassification, ...],
        public_run_ids: tuple[str, ...],
        exhausted: bool,
    ) -> MigrationProgress:
        table = self._store.tables.runtime_schema_migrations
        watch_table = self._store.tables.run_drive_watches
        requested_public_run_ids = tuple(
            record.public_run_id
            for record in classified
            if record.disposition == "nonterminal"
        )
        malformed_public_run_ids = tuple(
            record.public_run_id
            for record in classified
            if record.disposition == "malformed"
        )
        existing = connection.execute(
            select(table).where(table.c.name == _RUN_DRIVE_WATCH_MIGRATION)
        ).first()
        timestamp = datetime.now(UTC).isoformat()
        if existing is None:
            if expected_after_public_run_id is not None:
                raise NotImplementedError(
                    "run-drive-watch migration initialization is staged in R2"
                )
            stored_after_public_run_id = None
            progress_write = table.insert().values(
                name=_RUN_DRIVE_WATCH_MIGRATION,
                schema_version=2,
            )
        else:
            _validate_existing_run_drive_watch_migration(
                schema_version=existing.schema_version,
                stored_after_public_run_id=existing.after_public_run_id,
                expected_after_public_run_id=expected_after_public_run_id,
                completed_at=existing.completed_at,
            )
            stored_after_public_run_id = existing.after_public_run_id
            progress_write = table.update().where(
                table.c.name == _RUN_DRIVE_WATCH_MIGRATION
            )
        after_public_run_id = (
            public_run_ids[-1]
            if public_run_ids
            else stored_after_public_run_id
        )
        existing_watch_ids = (
            frozenset(
                connection.execute(
                    select(watch_table.c.public_run_id).where(
                        watch_table.c.public_run_id.in_(requested_public_run_ids)
                    )
                ).scalars()
            )
            if requested_public_run_ids
            else frozenset()
        )
        inserted_public_run_ids = tuple(
            public_run_id
            for public_run_id in requested_public_run_ids
            if public_run_id not in existing_watch_ids
        )
        if inserted_public_run_ids:
            connection.execute(
                watch_table.insert(),
                [
                    {
                        "public_run_id": public_run_id,
                        "input_blob_sha256": None,
                        "input_blob_size": None,
                        "admitted_at": timestamp,
                    }
                    for public_run_id in inserted_public_run_ids
                ],
            )
        connection.execute(
            progress_write.values(
                after_public_run_id=after_public_run_id,
                completed_at=timestamp if exhausted else None,
                updated_at=timestamp,
            )
        )
        return MigrationProgress(
            after_public_run_id,
            exhausted,
            inserted_public_run_ids,
            malformed_public_run_ids,
        )
