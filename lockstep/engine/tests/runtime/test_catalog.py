from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


@pytest.fixture
def sqlite_store(tmp_path):
    from lockstep.runtime.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "runtime.db")
    yield store
    store.close()


def _binding(run_id: str = "run-1", thread_id: str = "thread-1"):
    from lockstep.runtime.catalog import RunBinding

    return RunBinding(
        public_run_id=run_id,
        thread_id=thread_id,
        recipe_digest="a" * 64,
        recipe_snapshot_ref="bundle:" + "b" * 64,
        project_identity="project-identity",
    )


def test_run_catalog_has_no_workflow_state(sqlite_store):
    assert set(sqlite_store.tables.runs.c.keys()) == {
        "public_run_id",
        "thread_id",
        "recipe_digest",
        "recipe_snapshot_ref",
        "project_identity",
        "created_at",
    }


def test_catalog_creates_and_discovers_an_immutable_binding(sqlite_store):
    from lockstep.runtime.catalog import RunCatalog

    catalog = RunCatalog(sqlite_store)
    created = catalog.create(_binding())

    assert catalog.get("run-1") == created
    assert catalog.list("project-identity") == [created]
    assert not hasattr(catalog, "update")


def test_catalog_reuses_an_identical_binding_but_rejects_conflicts(sqlite_store):
    from lockstep.runtime.catalog import ImmutableBindingConflict, RunCatalog

    catalog = RunCatalog(sqlite_store)
    original = catalog.create(_binding())
    assert catalog.create(_binding()) == original

    with pytest.raises(ImmutableBindingConflict):
        catalog.create(_binding(thread_id="different-thread"))
    with pytest.raises(ImmutableBindingConflict):
        catalog.create(_binding(run_id="different-run"))


def test_catalog_concurrent_identical_create_publishes_one_binding(sqlite_store):
    from lockstep.runtime.catalog import RunCatalog

    catalog = RunCatalog(sqlite_store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: catalog.create(_binding()), range(16)))

    assert all(result == results[0] for result in results)
    assert catalog.list("project-identity") == [results[0]]


def test_catalog_canonicalizes_and_validates_created_at(sqlite_store):
    from lockstep.runtime.catalog import RunCatalog

    catalog = RunCatalog(sqlite_store)
    created = catalog.create(
        _binding().__class__(
            **{
                **_binding().__dict__,
                "created_at": "2026-08-20T12:34:56.123456+02:00",
            }
        )
    )

    assert created.created_at == "2026-08-20T10:34:56.123456+00:00"
    with pytest.raises(ValueError, match="created_at"):
        catalog.create(
            _binding("run-naive", "thread-naive").__class__(
                **{
                    **_binding("run-naive", "thread-naive").__dict__,
                    "created_at": "2026-08-20T10:34:56",
                }
            )
        )


def test_catalog_pages_legacy_bindings_in_public_run_id_order(sqlite_store) -> None:
    from lockstep.runtime.catalog import RunCatalog

    catalog = RunCatalog(sqlite_store)
    for public_run_id, project in (
        ("run-c", "project-b"),
        ("run-a", "project-a"),
        ("run-b", "project-a"),
    ):
        requested = _binding(public_run_id, f"thread-{public_run_id}")
        catalog.create(
            requested.__class__(
                **{**requested.__dict__, "project_identity": project}
            )
        )

    first = catalog.list_after_public_run_id(None, limit=2)
    second = catalog.list_after_public_run_id("run-b", limit=2)

    assert {
        "first": tuple(binding.public_run_id for binding in first),
        "second": tuple(binding.public_run_id for binding in second),
    } == {
        "first": ("run-a", "run-b"),
        "second": ("run-c",),
    }


def test_sqlite_filesystem_path_starting_with_sqlite_is_not_parsed_as_url(
    tmp_path, monkeypatch
):
    from lockstep.runtime.storage import SQLiteStore

    monkeypatch.chdir(tmp_path)
    path = Path("sqlite-runtime.db")
    store = SQLiteStore(path)
    try:
        assert path.is_file()
        assert store.database_path == path
        assert path.stat().st_mode & 0o077 == 0
    finally:
        store.close()


def test_sqlite_store_rejects_non_sqlite_urls_without_creating_a_path(
    tmp_path, monkeypatch
):
    from lockstep.runtime.storage import SQLiteStore

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="SQLite"):
        SQLiteStore("postgresql://localhost/lockstep")
    assert not (tmp_path / "postgresql:").exists()


def test_sqlite_rejects_insecure_existing_database_and_sidecar(tmp_path):
    from lockstep.runtime.owner_state import InsecureStatePath
    from lockstep.runtime.storage import SQLiteStore

    database = tmp_path / "runtime-insecure.db"
    database.write_bytes(b"")
    database.chmod(0o644)

    with pytest.raises(InsecureStatePath, match="owner-only"):
        SQLiteStore(database)

    database.chmod(0o600)
    sidecar = tmp_path / "runtime-insecure.db-wal"
    sidecar.write_bytes(b"")
    sidecar.chmod(0o644)
    with pytest.raises(InsecureStatePath, match="owner-only"):
        SQLiteStore(database)


def test_sqlite_rechecks_owner_only_mode_before_each_read(tmp_path):
    from lockstep.runtime.catalog import RunCatalog
    from lockstep.runtime.owner_state import InsecureStatePath
    from lockstep.runtime.storage import SQLiteStore

    database = tmp_path / "runtime-mode-change.db"
    storage = SQLiteStore(database)
    catalog = RunCatalog(storage)
    catalog.create(_binding())
    database.chmod(0o644)
    try:
        with pytest.raises(InsecureStatePath, match="owner-only"):
            catalog.get("run-1")
    finally:
        database.chmod(0o600)
        storage.close()
