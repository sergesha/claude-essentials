"""SQLAlchemy storage facade for Lockstep-owned runtime facts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

from sqlalchemy import MetaData, create_engine, inspect as sa_inspect, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url

from lockstep.runtime import _storage_migration
from lockstep.runtime._storage_migration import (
    LegacyRunDriveClassification as LegacyRunDriveClassification,
    MigrationProgress as MigrationProgress,
    RuntimeSchemaMigrator as RuntimeSchemaMigrator,
    _seal_sqlite_family,
    _sqlite_family,
    _verify_sqlite_family,
)
from lockstep.runtime._storage_schema import (
    RuntimeTables as RuntimeTables,
    _define_tables,
)
from lockstep.runtime.advisory_lock import advisory_file_lock
from lockstep.runtime.owner_state import initialize_owner_state


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
        return _sqlite_family(self.database_path)

    def _verify_sqlite_files(self) -> None:
        if self.database_path is not None:
            _verify_sqlite_family(self.database_path)

    def _seal_sqlite_files(self) -> None:
        if self.database_path is not None:
            _seal_sqlite_family(self.database_path)

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
        """Write only under the shared schema fence and exact v2 epoch."""

        fence = (
            nullcontext()
            if self.database_path is None
            else advisory_file_lock(self.database_path.parent / "runtime-schema.lock")
        )
        with fence:
            with self.write_transaction() as connection:
                epoch = connection.execute(
                    select(self.tables.runtime_schema_epoch.c.epoch).where(
                        self.tables.runtime_schema_epoch.c.singleton == 1
                    )
                ).scalar_one_or_none()
                if type(epoch) is not int or epoch != 2:
                    raise RuntimeError(
                        "runtime schema epoch 2 is required for v2 writes"
                    )
                yield connection

    def close(self) -> None:
        self.engine.dispose()
        self._seal_sqlite_files()


# Common spelling retained as an import alias, not a second implementation.
SqliteStore = SQLiteStore

# Preserve runtime annotation introspection for the public reexport without
# introducing an import cycle from the private migration module back here.
_storage_migration.SQLiteStore = SQLiteStore
