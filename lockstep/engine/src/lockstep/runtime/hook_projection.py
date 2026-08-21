"""Verified read-only native status projection for hooks and doctor."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from lockstep.recipe.authority import AuthorizedMaterialization
from lockstep.recipe.yamlgraph_adapter import open_native_app_readonly
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.owner_state import verify_owner_directory, verify_owner_file
from lockstep.runtime.recipe_bundles import (
    RecipeBundleRef,
    RecipeBundleStore,
    ValidatedDependencyDAG,
)
from lockstep.runtime.status import ScenarioStatus, project_status


class HookProjectionError(RuntimeError):
    """Trusted native state could not be projected safely."""


@dataclass(frozen=True)
class _ProjectedEffect:
    coordinate: NativeCoordinate
    descriptor_digest: str
    effect_kind: str
    phase: str


class _ProjectedEffects:
    """Minimal immutable effect view needed by the status projector."""

    def __init__(self, values: dict[str, _ProjectedEffect]) -> None:
        self._values = values

    def get(self, effect_id: str) -> _ProjectedEffect:
        try:
            return self._values[effect_id]
        except KeyError as exc:
            raise KeyError(effect_id) from exc


def _verify_sqlite_family(database: Path) -> None:
    verify_owner_file(database)
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(f"{database}{suffix}")
        if sidecar.exists() or sidecar.is_symlink():
            verify_owner_file(sidecar)


def _bindings(state_dir: Path) -> tuple[RunBinding, ...]:
    database = state_dir / "runtime.sqlite"
    if not database.exists() and not database.is_symlink():
        return ()
    _verify_sqlite_family(database)
    # Effect preparation can still be present in the verified WAL after the
    # service closes.  `immutable=1` deliberately ignores that WAL and would
    # project a prepared manual child as engine-owned.  Owner/seal checks above
    # make the ordinary read-only SQLite view the correct trusted boundary.
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT public_run_id, thread_id, recipe_digest, recipe_snapshot_ref, "
            "project_identity, created_at FROM runs ORDER BY created_at, public_run_id"
        ).fetchall()
    finally:
        connection.close()
    return tuple(RunBinding(*row) for row in rows)


def _effects(database: Path) -> _ProjectedEffects:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT effect_id, thread_id, checkpoint_ns, checkpoint_id, task_id, "
            "interrupt_id, descriptor_digest, effect_kind, phase FROM effects"
        ).fetchall()
    finally:
        connection.close()
    return _ProjectedEffects(
        {
            row[0]: _ProjectedEffect(
                NativeCoordinate(row[1], row[3], row[2], row[4], row[5]),
                row[6],
                row[7],
                row[8],
            )
            for row in rows
        }
    )


def _materialization(
    store: RecipeBundleStore, binding: RunBinding
) -> AuthorizedMaterialization:
    ref = RecipeBundleRef(binding.recipe_snapshot_ref)
    materialized = store.read_materialization(ref)
    manifest = store.read_manifest(ref)
    dag = ValidatedDependencyDAG(
        manifest.root, tuple(entry.path for entry in manifest.files)
    )
    return AuthorizedMaterialization(
        bundle=ref,
        definition_sha256=binding.recipe_digest,
        dependency_dag=dag,
        source_path=materialized.source_path,
        directory=materialized.directory,
    )


def read_only_statuses(
    state_dir: Path,
) -> tuple[tuple[RunBinding, ScenarioStatus], ...]:
    """Never creates storage, checkpoints, materializations, or transitions."""
    state_dir = Path(state_dir)
    if not state_dir.exists() and not state_dir.is_symlink():
        return ()
    database = state_dir / "runtime.sqlite"
    if not database.exists() and not database.is_symlink():
        return ()
    try:
        verify_owner_directory(state_dir)
        bindings = _bindings(state_dir)
        if not bindings:
            return ()
        checkpoints = state_dir / "checkpoints"
        verify_owner_directory(checkpoints)
        checkpoint = checkpoints / "native.sqlite"
        _verify_sqlite_family(checkpoint)
        effects = _effects(database)
        bundle_store = RecipeBundleStore.open_readonly(state_dir)

        projected = []
        for binding in bindings:
            app = open_native_app_readonly(
                _materialization(bundle_store, binding), checkpoint
            )
            try:
                snapshot = app.snapshot(thread_id=binding.thread_id, subgraphs=True)
            finally:
                app.close()
            projected.append((binding, project_status(binding, snapshot, (), effects)))
        return tuple(projected)
    except Exception as exc:
        raise HookProjectionError(
            "trusted native state failed read-only verification"
        ) from exc
