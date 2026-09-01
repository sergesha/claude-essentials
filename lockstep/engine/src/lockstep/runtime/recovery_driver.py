"""Private command-side owner of bounded run-drive recovery policy."""

from __future__ import annotations

from typing import Callable  # noqa: UP035 - preserve the established type-hint identity

from lockstep.runtime._recovery_backfill import (
    _bound_runtime,
    _classify_snapshot,
    _RunDriveBackfill,
)
from lockstep.runtime._recovery_watch_admission import _RecoveryWatchAdmission
from lockstep.runtime._recovery_watch_drive import _RecoveryWatchDrive
from lockstep.runtime._recovery_watch_enumeration import _RecoveryWatchEnumeration
from lockstep.runtime._recovery_watch_errors import (
    _BINDING_INTEGRITY_ERRORS,
    _RUN_DRIVE_INTEGRITY_ERRORS,
)
from lockstep.runtime._recovery_watch_inspection import _RecoveryWatchInspection
from lockstep.runtime._recovery_watch_settlement import _RecoveryWatchSettlement
from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.catalog import RunCatalog
from lockstep.runtime.effects.coordinator import EffectCoordinator
from lockstep.runtime.effects.ledger import EffectLedger
from lockstep.runtime.graph_runtime import GraphRuntime
from lockstep.runtime.snapshot_resolver import RuntimeSnapshotResolver
from lockstep.runtime.storage import RuntimeSchemaMigrator

__all__ = [
    "_BINDING_INTEGRITY_ERRORS",
    "_RUN_DRIVE_INTEGRITY_ERRORS",
    "RecoveryDriver",
    "_RunDriveBackfill",
    "_bound_runtime",
    "_classify_snapshot",
]


class RecoveryDriver(
    _RecoveryWatchEnumeration,
    _RecoveryWatchAdmission,
    _RecoveryWatchDrive,
    _RecoveryWatchInspection,
    _RecoveryWatchSettlement,
):
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
