"""Capability owner extracted from the command-service facade."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

from lockstep.recipe.yamlgraph_adapter import open_native_app
from lockstep.runtime.artifacts import ArtifactRegistry
from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.catalog import RunCatalog
from lockstep.runtime.effects.authority import (
    EffectAuthorityGate,
    EffectAuthorityUnavailable,
)
from lockstep.runtime.effects.coordinator import EffectCoordinator
from lockstep.runtime.effects.ledger import EffectLedger
from lockstep.runtime.effects.owner_consent import (
    OwnerConsentAuthority,
)
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.graph_runtime import GraphRuntime
from lockstep.runtime.invocation_lock import InvocationLockStore
from lockstep.runtime.leases import LeaseStore
from lockstep.runtime.owner_state import ensure_owner_directory, initialize_owner_state
from lockstep.runtime.project_snapshots import ProjectSnapshotStore
from lockstep.runtime.providers.base import RunnerAdapter
from lockstep.runtime.providers.manual import ManualProvider
from lockstep.runtime.publication import ProjectPublisher
from lockstep.runtime.recipe_bundles import RecipeBundleStore
from lockstep.runtime.runtime_execution import (
    RuntimeExecutionContext,
    capture_runtime_execution_admission,
)
from lockstep.runtime.runtime_execution_recovery import RuntimeExecutionRecovery
from lockstep.runtime.snapshot_resolver import (
    RuntimeSnapshotFacts,
    RuntimeSnapshotResolver,
)
from lockstep.runtime.storage import RuntimeSchemaMigrator

_SERVICE_FACADE = None

class _UnavailableEffectAuthority:
    """Production default: process effects require an owner composition root."""

    def resolve(self, _intent):
        raise EffectAuthorityUnavailable("no process effect authority is configured")

    @contextmanager
    def commitment(self, _grant, _request, _launch):
        raise EffectAuthorityUnavailable("no process effect authority is configured")
        yield

class _ServiceComposition:
    def _open_writable_stores(self) -> None:
        self.state_dir = initialize_owner_state(self.state_dir)
        database = self.state_dir / "runtime.sqlite"
        RuntimeSchemaMigrator.transition_legacy_to_v2(database)
        self.store = _SERVICE_FACADE.SQLiteStore(database)
        self.catalog = RunCatalog(self.store)
        self.bundle_store = RecipeBundleStore(self.state_dir)
        self.leases = LeaseStore(self.store)
        self.effects = EffectLedger(self.store)
        self.blobs = BlobStore(self.state_dir)
        self.snapshots = ProjectSnapshotStore(self.state_dir, self.blobs)
        self.artifacts = ArtifactRegistry(
            self.state_dir, self.blobs, self.snapshots
        )
        self.manual = ManualProvider(self.state_dir, self.blobs)

    def _open_graph_runtime(self) -> None:
        checkpoints = ensure_owner_directory(self.state_dir, "checkpoints")
        self.checkpoint_path = checkpoints / "native.sqlite"
        self.runtime = GraphRuntime(
            bundle_store=self.bundle_store,
            leases=self.leases,
            invocations=InvocationLockStore(self.state_dir, timeout=60.0),
            checkpoint_path=self.checkpoint_path,
            app_factory=open_native_app,
        )
        self.runtime_snapshot_facts = RuntimeSnapshotFacts(self.store)
        self.snapshot_resolver = RuntimeSnapshotResolver(
            self.runtime_snapshot_facts,
            self.snapshots,
            self.blobs,
            self.runtime,
        )

    def _effect_coordinator_for(
        self,
        runners: Mapping[str, RunnerAdapter],
        delegate: EffectAuthorityGate,
    ) -> tuple[OwnerConsentAuthority, EffectCoordinator]:
        authority = OwnerConsentAuthority(
            self.store,
            delegate=delegate,
        )
        coordinator = EffectCoordinator(
            runtime=self.runtime,
            catalog=self.catalog,
            ledger=self.effects,
            leases=self.leases,
            runners=runners,
            authority=authority,
            artifacts=self.artifacts,
            publisher_for=lambda binding: ProjectPublisher(
                self.state_dir,
                Path(binding.project_identity),
                self.artifacts,
                self.blobs,
            ),
            manual=self.manual,
            snapshot_resolver=self.snapshot_resolver,
        )
        return authority, coordinator

    def _install_runtime_execution(
        self, context: RuntimeExecutionContext
    ) -> None:
        composition = _SERVICE_FACADE.build_runtime_execution_composition(
            state_dir=self.state_dir,
            context=context,
            catalog=self.catalog,
            bundles=self.bundle_store,
            blobs=self.blobs,
            snapshots=self.snapshots,
        )
        released = composition.runners
        runners = {
            selector: runner
            for selector, runner in (("codex", released.codex), ("pinned", released.pinned))
            if runner is not None
        }
        authority, coordinator = self._effect_coordinator_for(
            runners, composition.authority
        )
        self._runtime_execution_composition = composition
        self._runtime_execution_context = context
        self.authority, self.coordinator = authority, coordinator

    def _open_effect_coordinator(self) -> None:
        context = self._runtime_execution_context
        if context is not None:
            self._install_runtime_execution(context)
            return
        self.authority, self.coordinator = self._effect_coordinator_for(
            {}, _UnavailableEffectAuthority()
        )

    def _reconstruct_runtime_execution_context(
        self,
        *,
        after_thread_id: str | None = None,
        limit: int | None = None,
    ) -> RuntimeExecutionContext | None:
        return RuntimeExecutionRecovery(
            state_dir=self.state_dir,
            catalog=self.catalog,
            bundles=self.bundle_store,
            effects=self.effects,
        ).reconstruct(
            limit=self._MAX_ACTIVE_EFFECT_RUNS if limit is None else limit,
            after_thread_id=after_thread_id,
        )

    def _install_recovered_runtime_execution(
        self,
        *,
        after_thread_id: str | None = None,
        limit: int | None = None,
    ) -> None:
        """Serialize cold reconstruction with foreground runtime admission."""

        with self._activation_lock:
            context = self._reconstruct_runtime_execution_context(
                after_thread_id=after_thread_id,
                limit=limit,
            )
            if context is None:
                return
            current = self._runtime_execution_context
            if current is None:
                self._install_runtime_execution(context)
            elif current != context:
                raise LockstepError("recovered runtime execution snapshot changed")

    def _require_owner_runtime_policy(self, index):
        """Use the production owner snapshot boundary for static admission."""

        try:
            return capture_runtime_execution_admission(self.state_dir, index)
        except FileNotFoundError as exc:
            raise LockstepError("runtime execution policy is unavailable") from exc
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise LockstepError(str(exc)) from exc
