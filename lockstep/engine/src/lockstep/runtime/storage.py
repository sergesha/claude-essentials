"""SQLAlchemy Core schema for Lockstep-owned runtime facts.

This module is the only place runtime SQL tables are declared.  In particular,
the run catalog is deliberately limited to immutable discovery data; native
LangGraph state remains in the configured saver.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from sqlalchemy import Column, Integer, MetaData, String, Table, UniqueConstraint, create_engine
from sqlalchemy.engine import Connection, Engine


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
        if raw == ":memory:":
            url = "sqlite+pysqlite:///:memory:"
        elif raw.startswith("sqlite"):
            url = raw
        else:
            db_path = Path(path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+pysqlite:///{db_path}"
        self.engine: Engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        self.metadata = MetaData()
        self.tables = _define_tables(self.metadata)
        self.metadata.create_all(self.engine)

    @contextmanager
    def write_transaction(self) -> Iterator[Connection]:
        """Serialize SQLite read/compare/write operations with BEGIN IMMEDIATE."""

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

    def close(self) -> None:
        self.engine.dispose()


# Common spelling retained as an import alias, not a second implementation.
SqliteStore = SQLiteStore
