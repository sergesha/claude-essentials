"""Private command-side owner of bounded run-drive recovery policy."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Callable, Iterator

from lockstep.runtime.blobs import (
    BlobRef,
    BlobStorageError,
    BlobStore,
    DigestMismatch,
)
from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.effects.coordinator import (
    CoordinatorLineageError,
    EffectCoordinator,
    ProviderContractViolation,
)
from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.effects.ledger import EffectLedger, RunDriveWatch
from lockstep.runtime.effects.models import (
    AcceptDescriptor,
    DecisionDescriptor,
    EffectDescriptor,
    PublishDescriptor,
    ScopeDescriptor,
)
from lockstep.runtime.graph_runtime import (
    GraphRuntime,
    NativeCoordinateRejected,
    RuntimeBindingConflict,
)
from lockstep.runtime.native_models import NativeHistoryLimitExceeded, NativeSnapshot
from lockstep.runtime.owner_state import StorageLimitExceeded
from lockstep.runtime.project_snapshots import SnapshotStorageError
from lockstep.runtime.recipe_bundles import MaterializationError
from lockstep.runtime.snapshot_resolver import (
    RuntimeSnapshotConflict,
    RuntimeSnapshotResolver,
)
from lockstep.runtime.start_input import decode_canonical_start_input
from lockstep.runtime.storage import (
    LegacyRunDriveClassification,
    RuntimeSchemaMigrator,
)

_BINDING_INTEGRITY_ERRORS = (
    KeyError,
    ValueError,
    DigestMismatch,
    MaterializationError,
    StorageLimitExceeded,
)
_RUN_DRIVE_INTEGRITY_ERRORS = _BINDING_INTEGRITY_ERRORS + (
    BlobStorageError,
    CoordinatorLineageError,
    NativeCoordinateRejected,
    NativeHistoryLimitExceeded,
    ProviderContractViolation,
    RuntimeBindingConflict,
    RuntimeSnapshotConflict,
    SnapshotStorageError,
)
_LOG = logging.getLogger(__name__)


@contextmanager
def _bound_runtime(
    runtime: GraphRuntime, binding: RunBinding
) -> Iterator[bool]:
    """Bind one recovered app temporarily without disturbing an existing bind."""

    owned = False
    try:
        current = runtime.binding(binding.public_run_id)
    except KeyError:
        try:
            owned = runtime.bind(binding)
        except _BINDING_INTEGRITY_ERRORS:
            yield False
            return
    else:
        if current != binding:
            yield False
            return
    try:
        yield True
    finally:
        if owned:
            runtime.unbind(binding.public_run_id)


def _classify_snapshot(
    run_id: str, snapshot: NativeSnapshot
) -> LegacyRunDriveClassification:
    if not snapshot.checkpoint_id:
        disposition = "malformed"
    elif snapshot.pending or snapshot.next:
        disposition = "nonterminal"
    else:
        disposition = "terminal"
    return LegacyRunDriveClassification(run_id, disposition)


class _RunDriveBackfill:
    PAGE_SIZE = 128

    def __init__(
        self,
        *,
        catalog: RunCatalog,
        runtime: GraphRuntime,
        migrator: RuntimeSchemaMigrator,
    ) -> None:
        self._catalog = catalog
        self._runtime = runtime
        self._migrator = migrator
        self._progress = migrator.run_drive_watch_migration_state()

    def _classify(self, binding: RunBinding) -> LegacyRunDriveClassification:
        with _bound_runtime(self._runtime, binding) as available:
            if not available:
                return LegacyRunDriveClassification(
                    binding.public_run_id, "malformed"
                )
            snapshot = self._runtime.snapshot(binding.public_run_id, subgraphs=True)
        return _classify_snapshot(binding.public_run_id, snapshot)

    def apply_next_page(self) -> tuple[str, ...]:
        progress = self._progress
        if progress is not None and progress.completed:
            return ()
        cursor = None if progress is None else progress.after_public_run_id
        candidates = self._catalog.list_after_public_run_id(
            cursor, limit=self.PAGE_SIZE + 1
        )
        if not candidates and progress is None:
            return ()
        page = candidates[: self.PAGE_SIZE]
        classified = tuple(self._classify(binding) for binding in page)
        self._progress = self._migrator.apply_run_drive_watch_page(
            expected_after_public_run_id=cursor,
            classified=classified,
            exhausted=len(candidates) <= self.PAGE_SIZE,
        )
        return self._progress.inserted_public_run_ids


class RecoveryDriver:
    """Bound migration work and one fixed watch population per recovery sweep."""

    def __init__(
        self,
        *,
        catalog: RunCatalog,
        runtime: GraphRuntime,
        effects: EffectLedger,
        blobs: BlobStore,
        migrator: RuntimeSchemaMigrator,
        coordinator: EffectCoordinator,
        snapshot_resolver: RuntimeSnapshotResolver,
        exclude_run_drive: Callable[[str], bool],
        drive_recovered_run: Callable[[str], bool],
    ) -> None:
        self._catalog = catalog
        self._runtime = runtime
        self._effects = effects
        self._blobs = blobs
        self._coordinator = coordinator
        self._snapshot_resolver = snapshot_resolver
        self._exclude_run_drive = exclude_run_drive
        self._drive_recovered_run = drive_recovered_run
        self._backfill = _RunDriveBackfill(
            catalog=catalog,
            runtime=runtime,
            migrator=migrator,
        )

    def _sweep_run_drive_watches(
        self,
        *,
        project_identity: str | None,
        limit: int,
    ) -> tuple[str, ...]:
        high_water = self._effects.max_run_drive_admission_seq()
        inserted_public_run_ids = self._backfill.apply_next_page()
        if limit < 1:
            return ()
        recovered: list[str] = []
        if high_water is not None:
            for watches in self._watch_pages(
                high_water=high_water, page_size=128
            ):
                recovered.extend(
                    self._accepted_from(
                        watches,
                        project_identity=project_identity,
                        limit=limit - len(recovered),
                    )
                )
                if len(recovered) == limit:
                    return tuple(recovered)
        if inserted_public_run_ids:
            watches = self._effects.list_run_drive_watches_by_public_run_ids(
                inserted_public_run_ids
            )
            recovered.extend(
                self._accepted_from(
                    watches,
                    project_identity=project_identity,
                    limit=limit - len(recovered),
                )
            )
        return tuple(recovered)

    def _accepted_from(
        self,
        watches: tuple[RunDriveWatch, ...],
        *,
        project_identity: str | None,
        limit: int,
    ) -> tuple[str, ...]:
        accepted = []
        for watch in watches:
            if self._try_drive_run_watch(watch, project_identity):
                accepted.append(watch.public_run_id)
                if len(accepted) == limit:
                    break
        return tuple(accepted)

    def _try_drive_run_watch(
        self, watch: RunDriveWatch, project_identity: str | None
    ) -> bool:
        try:
            return (
                not self._exclude_run_drive(watch.public_run_id)
                and self._matches_project(watch, project_identity)
                and self._drive_run_watch(watch)
            )
        except _RUN_DRIVE_INTEGRITY_ERRORS as exc:
            _LOG.warning(
                "run-drive recovery skipped %s after %s",
                watch.public_run_id,
                type(exc).__name__,
            )
            return False

    def _watch_pages(
        self, *, high_water: int, page_size: int
    ) -> Iterator[tuple[RunDriveWatch, ...]]:
        cursor = 0
        while cursor < high_water:
            watches = self._effects.list_run_drive_watches(
                after_admission_seq=cursor,
                high_water=high_water,
                limit=page_size,
            )
            if not watches:
                return
            yield watches
            cursor = watches[-1].admission_seq

    def _matches_project(
        self, watch: RunDriveWatch, project_identity: str | None
    ) -> bool:
        if project_identity is None:
            return True
        binding = self._catalog.get(watch.public_run_id)
        return binding.project_identity == project_identity

    def _settle_terminal_watch(self, run_id: str) -> None:
        reports = self._coordinator.reconcile_consumed(run_id)
        if any(report.action != "delivered" for report in reports):
            return
        self._effects.acknowledge_run_drive_watch(run_id)

    def _settle_after_accepted_drive(self, binding: RunBinding) -> None:
        with _bound_runtime(self._runtime, binding) as available:
            if not available:
                return
            snapshot = self._runtime.snapshot(
                binding.public_run_id, subgraphs=True
            )
            if not snapshot.pending and not snapshot.next:
                self._settle_terminal_watch(binding.public_run_id)

    def _drive_run_watch(self, watch: RunDriveWatch) -> bool:
        binding = self._catalog.get(watch.public_run_id)
        delegate = False
        with _bound_runtime(self._runtime, binding) as available:
            if not available:
                return False
            snapshot = self._runtime.snapshot(watch.public_run_id, subgraphs=True)
            if not snapshot.checkpoint_id:
                if (
                    watch.input_blob_sha256 is None
                    or watch.input_blob_size is None
                ):
                    return False
                self._snapshot_resolver.start_ref(binding)
                encoded = self._blobs.read(
                    BlobRef(watch.input_blob_sha256, watch.input_blob_size)
                )
                snapshot = self._runtime.ensure_started(
                    watch.public_run_id,
                    decode_canonical_start_input(encoded),
                )
            if not snapshot.pending and not snapshot.next:
                self._settle_terminal_watch(watch.public_run_id)
                return False
            if len(snapshot.pending) != 1:
                return False
            interrupt = snapshot.pending[0]
            raw = (
                interrupt.value.get("lockstep_effect")
                if isinstance(interrupt.value, dict)
                else None
            )
            try:
                descriptor = parse_effect_descriptor(raw)
            except (TypeError, ValueError):
                return False
            if isinstance(descriptor, DecisionDescriptor):
                report = self._coordinator.reconcile_one(
                    watch.public_run_id,
                    interrupt.coordinate,
                    expected_descriptor_digest=descriptor.digest,
                )
                accepted = report.action == "delivered"
            elif isinstance(
                descriptor,
                (
                    EffectDescriptor,
                    ScopeDescriptor,
                    AcceptDescriptor,
                    PublishDescriptor,
                ),
            ):
                delegate = True
            else:
                return False
            if not delegate:
                if accepted:
                    self._settle_after_accepted_drive(binding)
                return accepted
        accepted = self._drive_recovered_run(watch.public_run_id)
        if accepted:
            self._settle_after_accepted_drive(binding)
        return accepted
