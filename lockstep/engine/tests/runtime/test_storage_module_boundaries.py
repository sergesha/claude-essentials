"""Structural ownership checks for the runtime storage boundary."""

from __future__ import annotations

from typing import get_type_hints


def test_storage_reexports_private_schema_and_migration_owners() -> None:
    from lockstep.runtime import storage
    from lockstep.runtime._storage_migration import (
        LegacyRunDriveClassification,
        MigrationProgress,
        RuntimeSchemaMigrator,
    )
    from lockstep.runtime._storage_schema import RuntimeTables

    assert storage.RuntimeTables is RuntimeTables
    assert storage.LegacyRunDriveClassification is LegacyRunDriveClassification
    assert storage.MigrationProgress is MigrationProgress
    assert storage.RuntimeSchemaMigrator is RuntimeSchemaMigrator
    assert storage.SQLiteStore.__module__ == "lockstep.runtime.storage"
    assert get_type_hints(RuntimeSchemaMigrator.__init__)["store"] is storage.SQLiteStore
