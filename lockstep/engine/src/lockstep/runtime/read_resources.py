"""Verified, non-materializing resources for passive runtime observations."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lockstep.recipe.authority import (
    AuthorizedMaterialization,
    recipe_definition_sha256,
)
from lockstep.recipe.yamlgraph_adapter import NativeApp, open_native_app_readonly
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.native_models import (
    NativeCoordinate,
    NativeHistoryLimitExceeded,
    NativeSnapshot,
)
from lockstep.runtime.owner_state import (
    sqlite_readonly_uri,
    verify_owner_directory,
    verify_owner_file,
)
from lockstep.runtime.recipe_bundles import (
    RecipeBundleRef,
    RecipeBundleStore,
    ValidatedDependencyDAG,
)
from lockstep.runtime.sessions import MAX_SESSION_BINDING_BYTES


@dataclass(frozen=True)
class ProjectedEffect:
    effect_id: str
    coordinate: NativeCoordinate
    descriptor_digest: str
    effect_kind: str
    phase: str
    deadline_at: datetime | None
    updated_at: datetime


class ProjectedEffects:
    """Immutable effect view accepted by the pure status projector."""

    def __init__(self, values: tuple[ProjectedEffect, ...]) -> None:
        self._values = values
        self._by_id = {value.effect_id: value for value in values}

    def get(self, effect_id: str) -> ProjectedEffect:
        try:
            return self._by_id[effect_id]
        except KeyError as exc:
            raise KeyError(effect_id) from exc

    def list_for_thread(self, thread_id: str) -> tuple[ProjectedEffect, ...]:
        return tuple(
            value for value in self._values if value.coordinate.thread_id == thread_id
        )


def _verify_sqlite_family(database: Path) -> None:
    verify_owner_file(database)
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(f"{database}{suffix}")
        if sidecar.exists() or sidecar.is_symlink():
            verify_owner_file(sidecar)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("trusted effect timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _materialization(
    store: RecipeBundleStore, binding: RunBinding
) -> AuthorizedMaterialization:
    ref = RecipeBundleRef(binding.recipe_snapshot_ref)
    materialized = store.read_materialization(ref)
    manifest = store.read_manifest(ref)
    observed_definition = recipe_definition_sha256(
        manifest.root,
        ((entry.path, entry.sha256, entry.size) for entry in manifest.files),
    )
    if observed_definition != binding.recipe_digest:
        raise ValueError("catalog recipe digest does not match admitted bundle")
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


class RuntimeReadResources:
    """Open only verified existing facts; never initializes owner state."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir).absolute()

    @property
    def database(self) -> Path:
        return self.state_dir / "runtime.sqlite"

    def _binding_rows(
        self,
        *,
        where: str = "",
        parameters: tuple[object, ...] = (),
        limit: int = 10_000,
    ) -> tuple[RunBinding, ...]:
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("run catalog limit must be from 1 to 10000")
        if not self.state_dir.exists() and not self.state_dir.is_symlink():
            return ()
        verify_owner_directory(self.state_dir)
        if not self.database.exists() and not self.database.is_symlink():
            return ()
        _verify_sqlite_family(self.database)
        connection = sqlite3.connect(sqlite_readonly_uri(self.database), uri=True)
        try:
            rows = connection.execute(
                "SELECT public_run_id, thread_id, recipe_digest, "
                "recipe_snapshot_ref, project_identity, created_at FROM runs "
                f"{where} ORDER BY created_at, public_run_id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        finally:
            connection.close()
        if len(rows) > limit:
            raise ValueError("run catalog exceeds public projection limit")
        return tuple(RunBinding(*row) for row in rows)

    def bindings(self, *, limit: int = 10_000) -> tuple[RunBinding, ...]:
        return self._binding_rows(limit=limit)

    def bindings_for_project(
        self, project_identity: str, *, limit: int = 10_000
    ) -> tuple[RunBinding, ...]:
        if not project_identity:
            raise ValueError("project identity must not be empty")
        return self._binding_rows(
            where="WHERE project_identity = ?",
            parameters=(project_identity,),
            limit=limit,
        )

    def binding_for(
        self, public_run_id: str, project_identity: str
    ) -> RunBinding | None:
        if not public_run_id or not project_identity:
            raise ValueError("run and project identities must not be empty")
        values = self._binding_rows(
            where="WHERE public_run_id = ? AND project_identity = ?",
            parameters=(public_run_id, project_identity),
            limit=1,
        )
        return values[0] if values else None

    def session_binding(self, public_run_id: str) -> dict[str, object] | None:
        """Read one existing hook binding through the passive trust boundary."""

        if not public_run_id:
            raise ValueError("session binding run identity must not be empty")
        bindings = self.state_dir / "bindings"
        if not bindings.exists() and not bindings.is_symlink():
            return None
        verify_owner_directory(self.state_dir)
        verify_owner_directory(bindings)
        path = bindings / f"{public_run_id}.json"
        if not path.exists() and not path.is_symlink():
            return None
        verify_owner_file(path)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(MAX_SESSION_BINDING_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(data) > MAX_SESSION_BINDING_BYTES:
            raise ValueError("session binding exceeds passive projection limit")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("session binding is not valid UTF-8 JSON") from exc
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("session_id"), str)
            or not value["session_id"]
        ):
            raise ValueError("session binding has an invalid schema")
        return value

    def effects_for_thread(
        self, thread_id: str, *, limit: int = 10_000
    ) -> ProjectedEffects:
        if not thread_id:
            raise ValueError("effect thread_id must not be empty")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("effect observation limit must be from 1 to 10000")
        _verify_sqlite_family(self.database)
        connection = sqlite3.connect(sqlite_readonly_uri(self.database), uri=True)
        try:
            rows = connection.execute(
                "SELECT effect_id, thread_id, checkpoint_ns, checkpoint_id, "
                "task_id, interrupt_id, descriptor_digest, effect_kind, phase, "
                "deadline_at, updated_at FROM effects WHERE thread_id = ? "
                "ORDER BY created_at, effect_id LIMIT ?",
                (thread_id, limit + 1),
            ).fetchall()
        finally:
            connection.close()
        if len(rows) > limit:
            raise ValueError("effect observations exceed public bound")
        return ProjectedEffects(
            tuple(
                ProjectedEffect(
                    effect_id=row[0],
                    coordinate=NativeCoordinate(row[1], row[3], row[2], row[4], row[5]),
                    descriptor_digest=row[6],
                    effect_kind=row[7],
                    phase=row[8],
                    deadline_at=(
                        None if row[9] is None else _timestamp(row[9])
                    ),
                    updated_at=_timestamp(row[10]),
                )
                for row in rows
            )
        )

    @contextmanager
    def native_app(self, binding: RunBinding) -> Iterator[NativeApp]:
        checkpoints = self.state_dir / "checkpoints"
        verify_owner_directory(checkpoints)
        checkpoint = checkpoints / "native.sqlite"
        _verify_sqlite_family(checkpoint)
        store = RecipeBundleStore.open_readonly(self.state_dir)
        app = open_native_app_readonly(_materialization(store, binding), checkpoint)
        try:
            yield app
        finally:
            app.close()

    def history(self, binding: RunBinding) -> tuple[NativeSnapshot, ...]:
        with self.native_app(binding) as app:
            values: list[NativeSnapshot] = []
            history = iter(app.history(thread_id=binding.thread_id))
            try:
                for index, snapshot in enumerate(history):
                    if index >= 1024:
                        raise NativeHistoryLimitExceeded(
                            "native history exceeds public projection limit"
                        )
                    values.append(snapshot)
            finally:
                close = getattr(history, "close", None)
                if close is not None:
                    close()
        return tuple(values)
