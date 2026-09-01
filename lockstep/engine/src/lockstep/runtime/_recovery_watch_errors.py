"""Closed integrity-error sets for run-drive recovery."""

from lockstep.runtime.blobs import BlobStorageError, DigestMismatch
from lockstep.runtime.effects.coordinator import (
    CoordinatorLineageError,
    ProviderContractViolation,
)
from lockstep.runtime.graph_runtime import (
    NativeCoordinateRejected,
    RuntimeBindingConflict,
)
from lockstep.runtime.native_models import NativeHistoryLimitExceeded
from lockstep.runtime.owner_state import StorageLimitExceeded
from lockstep.runtime.project_snapshots import SnapshotStorageError
from lockstep.runtime.recipe_bundles import MaterializationError
from lockstep.runtime.snapshot_resolver import RuntimeSnapshotConflict

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
