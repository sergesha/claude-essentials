"""Legacy run-drive binding and bounded backfill support."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from lockstep.runtime._recovery_watch_errors import _BINDING_INTEGRITY_ERRORS
from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.graph_runtime import GraphRuntime
from lockstep.runtime.native_models import NativeSnapshot
from lockstep.runtime.storage import (
    LegacyRunDriveClassification,
    RuntimeSchemaMigrator,
)


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
