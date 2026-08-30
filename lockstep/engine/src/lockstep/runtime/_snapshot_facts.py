"""Append-only runtime snapshot input facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, select

from lockstep.runtime._snapshot_lineage import RuntimeSnapshotConflict
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.project_snapshots import ProjectSnapshotRef
from lockstep.runtime.storage import SQLiteStore


_RUN_START = "run_start_project_snapshot"
_CURRENT = "current_project_snapshot"
_SUCCESSOR = "successor_project_snapshot"


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class EffectRuntimeInput:
    effect_id: str
    runtime_key: str
    public_run_id: str
    coordinate: NativeCoordinate
    descriptor_digest: str
    snapshot_ref: ProjectSnapshotRef


class RuntimeSnapshotFacts:
    """Append-only access to neutral run/effect runtime input facts."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def bind_run_start_in_transaction(
        self, connection, binding: RunBinding, ref: ProjectSnapshotRef
    ) -> None:
        _digest(ref.digest, "run-start snapshot")
        table = self._store.tables.run_start_inputs
        expected = {
            "public_run_id": binding.public_run_id,
            "runtime_key": _RUN_START,
            "snapshot_ref": ref.digest,
            "project_identity": binding.project_identity,
            "definition_digest": binding.recipe_digest,
        }
        row = connection.execute(
            select(table).where(
                and_(
                    table.c.public_run_id == binding.public_run_id,
                    table.c.runtime_key == _RUN_START,
                )
            )
        ).first()
        if row is not None:
            if any(row._mapping[key] != value for key, value in expected.items()):
                raise RuntimeSnapshotConflict("run-start runtime input is already bound differently")
            return
        connection.execute(table.insert().values(**expected, created_at=_iso_now()))

    def run_start(self, binding: RunBinding) -> ProjectSnapshotRef:
        table = self._store.tables.run_start_inputs
        with self._store.read_connection() as connection:
            row = connection.execute(
                select(table).where(
                    and_(
                        table.c.public_run_id == binding.public_run_id,
                        table.c.runtime_key == _RUN_START,
                    )
                )
            ).first()
        if row is None:
            raise RuntimeSnapshotConflict("run-start runtime snapshot is missing")
        if (
            row.project_identity != binding.project_identity
            or row.definition_digest != binding.recipe_digest
        ):
            raise RuntimeSnapshotConflict("run-start runtime snapshot binding differs")
        return ProjectSnapshotRef(_digest(row.snapshot_ref, "run-start snapshot"))

    @staticmethod
    def _values(
        effect_id: str,
        runtime_key: str,
        binding: RunBinding,
        coordinate: NativeCoordinate,
        descriptor_digest: str,
        ref: ProjectSnapshotRef,
    ) -> dict[str, str]:
        if runtime_key not in {_CURRENT, _SUCCESSOR}:
            raise ValueError("unsupported effect runtime snapshot fact")
        return {
            "effect_id": effect_id,
            "runtime_key": runtime_key,
            "public_run_id": binding.public_run_id,
            "thread_id": coordinate.thread_id,
            "checkpoint_ns": coordinate.checkpoint_ns,
            "checkpoint_id": coordinate.checkpoint_id,
            "task_id": coordinate.task_id,
            "interrupt_id": coordinate.interrupt_id,
            "descriptor_digest": _digest(descriptor_digest, "descriptor digest"),
            "snapshot_ref": _digest(ref.digest, "effect snapshot"),
        }

    def bind_effect(
        self,
        effect_id: str,
        runtime_key: str,
        binding: RunBinding,
        coordinate: NativeCoordinate,
        descriptor_digest: str,
        ref: ProjectSnapshotRef,
    ) -> ProjectSnapshotRef:
        expected = self._values(
            effect_id, runtime_key, binding, coordinate, descriptor_digest, ref
        )
        table = self._store.tables.effect_runtime_inputs
        with self._store._v2_write_transaction() as connection:
            row = connection.execute(
                select(table).where(
                    and_(table.c.effect_id == effect_id, table.c.runtime_key == runtime_key)
                )
            ).first()
            if row is not None:
                if any(row._mapping[key] != value for key, value in expected.items()):
                    raise RuntimeSnapshotConflict("effect runtime input is already bound differently")
                return ref
            connection.execute(table.insert().values(**expected, created_at=_iso_now()))
        return ref

    def get_effect(self, effect_id: str, runtime_key: str) -> EffectRuntimeInput:
        table = self._store.tables.effect_runtime_inputs
        with self._store.read_connection() as connection:
            row = connection.execute(
                select(table).where(
                    and_(table.c.effect_id == effect_id, table.c.runtime_key == runtime_key)
                )
            ).first()
        if row is None:
            raise KeyError((effect_id, runtime_key))
        return EffectRuntimeInput(
            row.effect_id,
            row.runtime_key,
            row.public_run_id,
            NativeCoordinate(
                row.thread_id,
                row.checkpoint_id,
                row.checkpoint_ns,
                row.task_id,
                row.interrupt_id,
            ),
            row.descriptor_digest,
            ProjectSnapshotRef(row.snapshot_ref),
        )

    def list_successors(self, binding: RunBinding, *, limit: int = 10_000) -> tuple[EffectRuntimeInput, ...]:
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("runtime snapshot fact limit must be from 1 to 10000")
        table = self._store.tables.effect_runtime_inputs
        with self._store.read_connection() as connection:
            rows = connection.execute(
                select(table)
                .where(
                    and_(
                        table.c.public_run_id == binding.public_run_id,
                        table.c.thread_id == binding.thread_id,
                        table.c.runtime_key == _SUCCESSOR,
                    )
                )
                .order_by(table.c.created_at, table.c.effect_id)
                .limit(limit + 1)
            ).all()
        if len(rows) > limit:
            raise RuntimeSnapshotConflict("runtime snapshot facts exceed public bound")
        return tuple(
            EffectRuntimeInput(
                row.effect_id,
                row.runtime_key,
                row.public_run_id,
                NativeCoordinate(
                    row.thread_id,
                    row.checkpoint_id,
                    row.checkpoint_ns,
                    row.task_id,
                    row.interrupt_id,
                ),
                row.descriptor_digest,
                ProjectSnapshotRef(row.snapshot_ref),
            )
            for row in rows
        )
