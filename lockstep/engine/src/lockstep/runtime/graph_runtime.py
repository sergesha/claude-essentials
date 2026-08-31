"""Thin lifecycle and coordinate guard over native checkpointed applications."""

from __future__ import annotations

from pathlib import Path
from threading import RLock, local

from lockstep.runtime._graph_runtime_guard import _GraphRuntimeGuard
from lockstep.runtime._graph_runtime_lifecycle import _GraphRuntimeLifecycle
from lockstep.runtime._graph_runtime_lineage import _GraphRuntimeLineage
from lockstep.runtime._graph_runtime_values import (
    MAX_HISTORY_INTERRUPTS,
    MAX_HISTORY_SNAPSHOTS,
    NativeCommitment,
    NativeCoordinateRejected,
    RuntimeBindingConflict,
)
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.invocation_lock import InvocationLockStore
from lockstep.runtime.leases import LeaseStore
from lockstep.runtime.native_models import (
    NativeAppFactory,
    NativeAppPort,
    NativeHistoryLimitExceeded,
)
from lockstep.runtime.recipe_bundles import RecipeBundleStore

__all__ = (
    "MAX_HISTORY_INTERRUPTS",
    "MAX_HISTORY_SNAPSHOTS",
    "GraphRuntime",
    "NativeCommitment",
    "NativeCoordinateRejected",
    "NativeHistoryLimitExceeded",
    "RuntimeBindingConflict",
)


class GraphRuntime(
    _GraphRuntimeLifecycle,
    _GraphRuntimeGuard,
    _GraphRuntimeLineage,
):
    """Keep native apps alive while leaving all workflow state in checkpoints."""

    def __init__(
        self,
        *,
        bundle_store: RecipeBundleStore,
        leases: LeaseStore,
        invocations: InvocationLockStore,
        checkpoint_path: Path,
        app_factory: NativeAppFactory,
        lease_ttl: float = 60.0,
    ) -> None:
        self._bundles = bundle_store
        self._leases = leases
        self._checkpoint_path = Path(checkpoint_path)
        self._app_factory = app_factory
        self._lease_ttl = lease_ttl
        self._invocations = invocations
        self._bindings: dict[str, RunBinding] = {}
        self._apps: dict[str, NativeAppPort] = {}
        self._lock = RLock()
        self._guard_local = local()
        self._closed = False
        self._closing = False
