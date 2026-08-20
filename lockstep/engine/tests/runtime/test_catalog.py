from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

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
