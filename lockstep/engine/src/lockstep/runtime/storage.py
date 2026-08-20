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

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url

from lockstep.runtime.owner_state import (
    initialize_owner_state,
    seal_owner_file,
    verify_owner_file,
)


@dataclass(frozen=True)
class RuntimeTables:
    runs: Table
    leases: Table


def _define_tables(metadata: MetaData) -> RuntimeTables:
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
    return RuntimeTables(runs=runs, leases=leases)


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
        self.metadata = MetaData()
        self.tables = _define_tables(self.metadata)
        self.metadata.create_all(self.engine)
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

    def close(self) -> None:
        self.engine.dispose()
        self._seal_sqlite_files()


# Common spelling retained as an import alias, not a second implementation.
SqliteStore = SQLiteStore
