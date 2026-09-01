from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import lockstep.recipe.yamlgraph_adapter as adapter
import pytest
from lockstep.recipe.authority import AuthorizedMaterialization
from lockstep.runtime.recipe_bundles import RecipeBundleRef, ValidatedDependencyDAG


FIXTURES = Path(__file__).parents[1] / "fixtures" / "native"


def _materialization() -> AuthorizedMaterialization:
    return AuthorizedMaterialization(
        bundle=RecipeBundleRef("0" * 64),
        definition_sha256="0" * 64,
        dependency_dag=ValidatedDependencyDAG(
            "parent_direct.recipe.yaml",
            ("parent_direct.recipe.yaml", "child_interrupt.recipe.yaml"),
        ),
        source_path=FIXTURES / "parent_direct.recipe.yaml",
        directory=FIXTURES,
    )


def test_readonly_native_open_rejects_raw_copy_checkpoint_corruption(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A committed WAL epoch stays visible and atomic through checkpointing."""

    database = tmp_path / "native.sqlite"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.executescript(
        "CREATE TABLE checkpoints (value BLOB);"
        "CREATE TABLE writes (value BLOB);"
        "CREATE TABLE epoch_first (value INTEGER NOT NULL);"
        "INSERT INTO epoch_first VALUES (0);"
        "CREATE TABLE padding (value BLOB);"
        "INSERT INTO padding VALUES (zeroblob(4194304));"
        "CREATE TABLE epoch_last (value INTEGER NOT NULL);"
        "INSERT INTO epoch_last VALUES (0);"
    )
    writer.commit()
    assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

    page_size = writer.execute("PRAGMA page_size").fetchone()[0]
    roots = dict(
        writer.execute(
            "SELECT name, rootpage FROM sqlite_master "
            "WHERE name IN ('epoch_first', 'epoch_last')"
        )
    )
    assert roots["epoch_first"] < roots["epoch_last"]
    split_at = (roots["epoch_last"] - 1) * page_size

    writer.execute("UPDATE epoch_first SET value = 1")
    writer.execute("UPDATE epoch_last SET value = 1")
    writer.commit()
    wal = Path(f"{database}-wal")
    assert wal.stat().st_size > 0
    wal_before = wal.read_bytes()

    real_copyfile = shutil.copyfile
    def copy_with_checkpoint_between_database_pages(source, destination, *args, **kwargs):
        if Path(source) != database:
            return real_copyfile(source, destination, *args, **kwargs)
        with Path(source).open("rb") as incoming, Path(destination).open("wb") as outgoing:
            outgoing.write(incoming.read(split_at))
            checkpointer = sqlite3.connect(database)
            try:
                assert checkpointer.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()[0] == 0
            finally:
                checkpointer.close()
            shutil.copyfileobj(incoming, outgoing)
        return destination

    monkeypatch.setattr(shutil, "copyfile", copy_with_checkpoint_between_database_pages)
    app = adapter.open_native_app_readonly(_materialization(), database)
    try:
        observed = tuple(
            app._connection.execute(  # noqa: SLF001 - exercise the opened snapshot
                "SELECT "
                "(SELECT value FROM epoch_first), "
                "(SELECT value FROM epoch_last)"
            ).fetchone()
        )
    finally:
        app.close()
        wal_after = wal.read_bytes()
        writer.close()

    assert observed == (1, 1)
    assert wal_after == wal_before


def test_readonly_native_transaction_stays_pinned_across_writer_checkpoint(
    tmp_path: Path,
) -> None:
    database = tmp_path / "native.sqlite"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.executescript(
        "CREATE TABLE checkpoints (value BLOB);"
        "CREATE TABLE writes (value BLOB);"
        "CREATE TABLE epoch (value INTEGER NOT NULL);"
        "INSERT INTO epoch VALUES (0);"
    )
    writer.commit()
    assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    writer.execute("UPDATE epoch SET value = 1")
    writer.commit()

    first = adapter.open_native_app_readonly(_materialization(), database)
    try:
        writer.execute("UPDATE epoch SET value = 2")
        writer.commit()
        assert writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[0] == 0
        assert first._connection.execute(  # noqa: SLF001 - pinned snapshot
            "SELECT value FROM epoch"
        ).fetchone()[0] == 1
    finally:
        first.close()

    second = adapter.open_native_app_readonly(_materialization(), database)
    try:
        assert second._connection.execute(  # noqa: SLF001 - fresh snapshot
            "SELECT value FROM epoch"
        ).fetchone()[0] == 2
    finally:
        second.close()
        writer.close()


def test_readonly_native_open_sees_wal_created_immediately_before_connect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "native.sqlite"
    initial = sqlite3.connect(database)
    initial.executescript(
        "CREATE TABLE checkpoints (value BLOB);"
        "CREATE TABLE writes (value BLOB);"
        "CREATE TABLE epoch (value INTEGER NOT NULL);"
        "INSERT INTO epoch VALUES (0);"
    )
    initial.commit()
    initial.close()
    assert not Path(f"{database}-wal").exists()

    real_connect = sqlite3.connect
    writers: list[sqlite3.Connection] = []

    def connect_after_wal_commit(*args, **kwargs):
        writer = real_connect(database)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE epoch SET value = 1")
        writer.commit()
        writers.append(writer)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(adapter.sqlite3, "connect", connect_after_wal_commit)
    app = adapter.open_native_app_readonly(_materialization(), database)
    try:
        observed = app._connection.execute(  # noqa: SLF001 - opened snapshot
            "SELECT value FROM epoch"
        ).fetchone()[0]
    finally:
        app.close()
        for writer in writers:
            writer.close()

    assert observed == 1


@pytest.mark.parametrize("path_component", ["question?mark", "hash#mark"])
def test_readonly_native_open_escapes_sqlite_uri_path(
    tmp_path: Path,
    path_component: str,
) -> None:
    directory = tmp_path / path_component
    directory.mkdir()
    database = directory / "native.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        "CREATE TABLE checkpoints (value BLOB);"
        "CREATE TABLE writes (value BLOB);"
        "CREATE TABLE epoch (value INTEGER NOT NULL);"
        "INSERT INTO epoch VALUES (7);"
    )
    connection.commit()
    connection.close()

    app = adapter.open_native_app_readonly(_materialization(), database)
    try:
        assert app._connection.execute(  # noqa: SLF001 - opened snapshot
            "SELECT value FROM epoch"
        ).fetchone()[0] == 7
    finally:
        app.close()
