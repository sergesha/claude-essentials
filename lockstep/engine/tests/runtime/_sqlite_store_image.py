"""Deterministic logical and byte image of one SQLite database family."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoreImage:
    logical_rows: tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]
    sqlite_family: tuple[tuple[str, bool, bytes | None], ...]

    @classmethod
    def capture(cls, database: Path) -> StoreImage:
        return cls(_logical_rows(database), _sqlite_family(database))


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_rows(connection: sqlite3.Connection, name: str):
    columns = tuple(
        row[1]
        for row in connection.execute(f"PRAGMA table_info({_quoted(name)})")
    )
    order = ", ".join(_quoted(column) for column in columns)
    return tuple(
        connection.execute(
            f"SELECT * FROM {_quoted(name)} ORDER BY {order}"
        ).fetchall()
    )


def _logical_rows(
    database: Path,
) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        names = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
            )
        )
        return tuple((name, _table_rows(connection, name)) for name in names)
    finally:
        connection.close()


def _sqlite_family(database: Path) -> tuple[tuple[str, bool, bytes | None], ...]:
    paths = (
        database,
        Path(f"{database}-journal"),
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
    )
    return tuple(
        (path.name, path.exists(), path.read_bytes() if path.exists() else None)
        for path in paths
    )
