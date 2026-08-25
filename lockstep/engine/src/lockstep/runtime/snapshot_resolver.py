"""Durable runtime-owned project snapshot inputs.

These facts are deliberately outside LangGraph state and the external-effect
ledger.  They bind exact native coordinates to immutable content-addressed
project snapshots before any authority-bearing runner port is consulted.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import and_, select

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.models import (
    DecisionDescriptor,
    DecisionResult,
    EffectDescriptor,
    RuntimeInputSelector,
)
from lockstep.runtime.native_models import NativeCoordinate, NativeInterrupt
from lockstep.runtime.project_snapshots import (
    ProjectSnapshotRef,
    ProjectSnapshotStore,
)
from lockstep.runtime.storage import SQLiteStore


class RuntimeSnapshotConflict(RuntimeError):
    """An immutable runtime input was rebound or failed lineage verification."""


_RUN_START = "run_start_project_snapshot"
_CURRENT = "current_project_snapshot"
_SUCCESSOR = "successor_project_snapshot"
_MAX_LINEAGE = 10_000


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _read_regular(path: Path, expected_sha256: str, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise RuntimeSnapshotConflict(f"project snapshot file is not admissible: {path}")
        chunks: list[bytes] = []
        observed = 0
        while chunk := os.read(descriptor, min(1024 * 1024, max_bytes + 1 - observed)):
            observed += len(chunk)
            if observed > max_bytes:
                raise RuntimeSnapshotConflict(f"project snapshot file exceeds limit: {path}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)
        if identity(before) != identity(after):
            raise RuntimeSnapshotConflict(f"project snapshot file changed while reading: {path}")
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise RuntimeSnapshotConflict(f"project snapshot file changed while reading: {path}")
        return data
    finally:
        os.close(descriptor)


def capture_authoritative_snapshot(
    project: Path,
    snapshots: ProjectSnapshotStore,
    blobs: BlobStore,
    binding: RunBinding,
    *,
    previous: ProjectSnapshotRef | None,
    purpose: str,
) -> ProjectSnapshotRef:
    """Capture one complete, symlink-free project image as a chain successor."""

    if purpose not in {"run-start", "manual", "publication", "effect"}:
        raise ValueError("unsupported authoritative snapshot purpose")
    root = Path(project)
    if root.resolve() != Path(binding.project_identity).resolve():
        raise RuntimeSnapshotConflict("snapshot project differs from immutable run binding")
    from lockstep.runtime.manifests import PathContractError, capture_project

    try:
        manifest = capture_project(root, limits=snapshots.limits)
    except (OSError, PathContractError) as exc:
        raise RuntimeSnapshotConflict(f"project snapshot capture failed: {exc}") from exc
    if any(item.kind == "symlink" for item in manifest.entries):
        raise RuntimeSnapshotConflict("authoritative project snapshots reject symlinks")
    file_entries = tuple(item for item in manifest.entries if item.kind == "file")
    stored = {}
    for item in file_entries:
        assert item.sha256 is not None
        data = _read_regular(root / item.path, item.sha256, snapshots.limits.max_file_bytes)
        stored[item.path] = blobs.put(data, expected_sha256=item.sha256)
    provenance = {
        "schema": "lockstep.run-project-snapshot/v1",
        "public_run_id": binding.public_run_id,
        "project_identity": binding.project_identity,
        "definition_digest": binding.recipe_digest,
        "purpose": purpose,
    }
    return snapshots.capture(
        stored,
        declared_paths=tuple(stored),
        provenance=provenance,
        previous=previous,
    )


def verify_bound_snapshot(
    ref: ProjectSnapshotRef,
    snapshots: ProjectSnapshotStore,
    binding: RunBinding,
):
    """Read one immutable snapshot and verify its exact run/project provenance."""

    snapshot = snapshots.read(ref)
    provenance = dict(snapshot.provenance)
    if (
        provenance.get("schema") != "lockstep.run-project-snapshot/v1"
        or provenance.get("public_run_id") != binding.public_run_id
        or provenance.get("project_identity") != binding.project_identity
        or provenance.get("definition_digest") != binding.recipe_digest
        or provenance.get("purpose") not in {"run-start", "manual", "publication", "effect"}
    ):
        raise RuntimeSnapshotConflict("runtime snapshot is foreign to the immutable run binding")
    return snapshot


def _chain(ref: ProjectSnapshotRef, snapshots: ProjectSnapshotStore) -> tuple[ProjectSnapshotRef, ...]:
    result: list[ProjectSnapshotRef] = []
    seen: set[ProjectSnapshotRef] = set()
    current: ProjectSnapshotRef | None = ref
    while current is not None:
        if current in seen:
            raise RuntimeSnapshotConflict("project snapshot lineage contains a cycle")
        if len(result) >= _MAX_LINEAGE:
            raise RuntimeSnapshotConflict("project snapshot lineage exceeds public bound")
        seen.add(current)
        result.append(current)
        current = snapshots.read(current).previous
    return tuple(result)


def resolve_lineage_snapshot(
    refs: Iterable[ProjectSnapshotRef], snapshots: ProjectSnapshotStore
) -> ProjectSnapshotRef:
    """Return the greatest common ancestor of exact immutable snapshot chains."""

    selected = tuple(dict.fromkeys(refs))
    if not selected:
        raise RuntimeSnapshotConflict("runtime snapshot lineage is empty")
    chains = tuple(_chain(ref, snapshots) for ref in selected)
    common = set(chains[0])
    for chain in chains[1:]:
        common.intersection_update(chain)
    for candidate in chains[0]:
        if candidate in common:
            return candidate
    raise RuntimeSnapshotConflict("runtime snapshot lineages have no common ancestor")


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
        with self._store.write_transaction() as connection:
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


class RuntimeSnapshotResolver:
    def __init__(
        self,
        facts: RuntimeSnapshotFacts,
        snapshots: ProjectSnapshotStore,
        blobs: BlobStore,
        runtime,
    ) -> None:
        self._facts = facts
        self._snapshots = snapshots
        self._blobs = blobs
        self._runtime = runtime

    def start_ref(self, binding: RunBinding) -> ProjectSnapshotRef:
        ref = self._facts.run_start(binding)
        snapshot = verify_bound_snapshot(ref, self._snapshots, binding)
        if snapshot.previous is not None or snapshot.provenance["purpose"] != "run-start":
            raise RuntimeSnapshotConflict("run-start snapshot is not a lineage root")
        return ref

    def _verify_chain_binding(
        self, ref: ProjectSnapshotRef, binding: RunBinding
    ) -> None:
        chain = _chain(ref, self._snapshots)
        if self.start_ref(binding) not in chain:
            raise RuntimeSnapshotConflict(
                "runtime snapshot chain does not descend from the exact run start"
            )
        snapshot = self._snapshots.read(ref)
        if snapshot.provenance.get("schema") == "lockstep.run-project-snapshot/v1":
            verify_bound_snapshot(ref, self._snapshots, binding)

    def _current_ref(
        self, binding: RunBinding, interrupt: NativeInterrupt
    ) -> ProjectSnapshotRef:
        candidates = []
        for fact in self._facts.list_successors(binding):
            self._verify_chain_binding(fact.snapshot_ref, binding)
            if self._runtime.checkpoint_is_ancestor(
                binding.public_run_id, fact.coordinate, interrupt
            ):
                candidates.append(fact.snapshot_ref)
        if not candidates:
            return self.start_ref(binding)
        chains = {ref: set(_chain(ref, self._snapshots)[1:]) for ref in candidates}
        tips = tuple(
            ref for ref in dict.fromkeys(candidates)
            if not any(ref in ancestors for other, ancestors in chains.items() if other != ref)
        )
        return resolve_lineage_snapshot(tips, self._snapshots)

    def inputs_for(
        self,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: EffectDescriptor | DecisionDescriptor,
        effect_id: str,
    ) -> dict[str, str]:
        selectors = tuple(
            (name, selector)
            for name, selector in descriptor.inputs
            if isinstance(selector, RuntimeInputSelector)
        )
        if not selectors:
            return {}
        try:
            bound = self._facts.get_effect(effect_id, _CURRENT)
        except KeyError:
            bound = None
        if bound is not None:
            if (
                bound.public_run_id != binding.public_run_id
                or bound.coordinate != interrupt.coordinate
                or bound.descriptor_digest != descriptor.digest
            ):
                raise RuntimeSnapshotConflict("effect runtime input belongs to foreign lineage")
            current = bound.snapshot_ref
            self._verify_chain_binding(current, binding)
        else:
            current = self._current_ref(binding, interrupt)
            self._facts.bind_effect(
                effect_id,
                _CURRENT,
                binding,
                interrupt.coordinate,
                descriptor.digest,
                current,
            )
        start = self.start_ref(binding)
        return {
            name: "snapshot:" + (start if selector.runtime_key == _RUN_START else current).digest
            for name, selector in selectors
        }

    def capture_successor(
        self,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: EffectDescriptor | object,
        effect_id: str,
        *,
        purpose: str,
    ) -> ProjectSnapshotRef:
        try:
            existing = self._facts.get_effect(effect_id, _SUCCESSOR)
        except KeyError:
            existing = None
        if existing is not None:
            if (
                existing.public_run_id != binding.public_run_id
                or existing.coordinate != interrupt.coordinate
                or existing.descriptor_digest != descriptor.digest
            ):
                raise RuntimeSnapshotConflict("effect successor belongs to foreign lineage")
            verify_bound_snapshot(existing.snapshot_ref, self._snapshots, binding)
            return existing.snapshot_ref
        previous = self._current_ref(binding, interrupt)
        ref = capture_authoritative_snapshot(
            Path(binding.project_identity),
            self._snapshots,
            self._blobs,
            binding,
            previous=previous,
            purpose=purpose,
        )
        return self._facts.bind_effect(
            effect_id,
            _SUCCESSOR,
            binding,
            interrupt.coordinate,
            descriptor.digest,
            ref,
        )

    def adopt_successor(
        self,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: object,
        effect_id: str,
        ref: ProjectSnapshotRef,
    ) -> ProjectSnapshotRef:
        """Bind a runner-produced immutable rollover after proving its input edge."""

        try:
            existing = self._facts.get_effect(effect_id, _SUCCESSOR)
        except KeyError:
            existing = None
        if existing is not None:
            if existing.snapshot_ref != ref:
                raise RuntimeSnapshotConflict(
                    "effect successor is already bound to another snapshot"
                )
            self._verify_chain_binding(ref, binding)
            return ref
        previous = self._current_ref(binding, interrupt)
        snapshot = self._snapshots.read(ref)
        if snapshot.previous != previous:
            raise RuntimeSnapshotConflict(
                "effect successor does not descend from its exact runtime input"
            )
        self._verify_chain_binding(ref, binding)
        return self._facts.bind_effect(
            effect_id,
            _SUCCESSOR,
            binding,
            interrupt.coordinate,
            descriptor.digest,
            ref,
        )

    def decide(
        self,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: DecisionDescriptor,
        effect_id: str,
    ) -> DecisionResult:
        inputs = self.inputs_for(binding, interrupt, descriptor, effect_id)
        start = self._snapshots.read(
            ProjectSnapshotRef(inputs["start_snapshot"].removeprefix("snapshot:"))
        )
        current = self._snapshots.read(
            ProjectSnapshotRef(inputs["current_snapshot"].removeprefix("snapshot:"))
        )
        before = {item.path: item.blob.sha256 for item in start.files}
        after = {item.path: item.blob.sha256 for item in current.files}
        changed = tuple(sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path)))
        value = descriptor.decision.default
        for case in descriptor.decision.cases:
            if any(
                fnmatch.fnmatchcase(path, pattern)
                or (pattern.endswith("/**") and path == pattern[:-3])
                for path in changed
                for pattern in case.paths
            ):
                value = case.label
                break
        return DecisionResult(
            "lockstep.decision-result/v1",
            effect_id,
            "PASS",
            descriptor.digest,
            value=value,
        )
