"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from lockstep.runtime.artifacts import (
    ArtifactRegistry,
)
from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.effects import _coordinator_validation as _validation_module
from lockstep.runtime.effects._coordinator_admission import _EffectCoordinatorAdmission
from lockstep.runtime.effects._coordinator_authority_recovery import (
    _EffectCoordinatorAuthorityRecovery,
)
from lockstep.runtime.effects._coordinator_context import _EffectCoordinatorContext
from lockstep.runtime.effects._coordinator_context_input import (
    _EffectCoordinatorContextInput,
)
from lockstep.runtime.effects._coordinator_delivery import _EffectCoordinatorDelivery
from lockstep.runtime.effects._coordinator_foundation import (
    _EffectCoordinatorFoundation,
)
from lockstep.runtime.effects._coordinator_lineage import _EffectCoordinatorLineage
from lockstep.runtime.effects._coordinator_orchestration import (
    _EffectCoordinatorOrchestration,
)
from lockstep.runtime.effects._coordinator_publication import (
    _EffectCoordinatorPublication,
)
from lockstep.runtime.effects._coordinator_publication_commitment import (
    _EffectCoordinatorPublicationCommitment,
)
from lockstep.runtime.effects._coordinator_publication_existing import (
    _EffectCoordinatorPublicationExisting,
)
from lockstep.runtime.effects._coordinator_publication_planning import (
    _EffectCoordinatorPublicationPlanning,
)
from lockstep.runtime.effects._coordinator_publication_preparation import (
    _EffectCoordinatorPublicationPreparation,
)
from lockstep.runtime.effects._coordinator_publication_recovery import (
    _EffectCoordinatorPublicationRecovery,
)
from lockstep.runtime.effects._coordinator_publication_recovery_policy import (
    _EffectCoordinatorPublicationRecoveryPolicy,
)
from lockstep.runtime.effects._coordinator_publication_recovery_transaction import (
    _EffectCoordinatorPublicationRecoveryTransaction,
)
from lockstep.runtime.effects._coordinator_publication_transition import (
    _EffectCoordinatorPublicationTransition,
)
from lockstep.runtime.effects._coordinator_reconciliation import (
    _EffectCoordinatorReconciliation,
)
from lockstep.runtime.effects._coordinator_runner import _EffectCoordinatorRunner
from lockstep.runtime.effects._coordinator_runner_resolution import (
    _EffectCoordinatorRunnerResolution,
)
from lockstep.runtime.effects._coordinator_validation import (
    _EffectCoordinatorValidation,
)
from lockstep.runtime.effects._coordinator_values import (
    CoordinatorLineageError,
    ProviderContractViolation,
    ReconcileReport,
    _Context,
    _PublicationItemContext,
)
from lockstep.runtime.effects.authority import (
    EffectAuthorityGate,
)
from lockstep.runtime.effects.ledger import (
    EffectLedger,
)
from lockstep.runtime.graph_runtime import GraphRuntime
from lockstep.runtime.leases import LeaseStore
from lockstep.runtime.providers.base import (
    RunnerAdapter,
)
from lockstep.runtime.providers.manual import (
    ManualProvider,
)
from lockstep.runtime.publication import (
    ProjectPublisher,
)
from lockstep.runtime.snapshot_resolver import RuntimeSnapshotResolver

__all__ = (
    "CoordinatorLineageError",
    "EffectCoordinator",
    "ProviderContractViolation",
    "ReconcileReport",
    "_Context",
    "_PublicationItemContext",
)


class EffectCoordinator(
    _EffectCoordinatorValidation,
    _EffectCoordinatorContext,
    _EffectCoordinatorLineage,
    _EffectCoordinatorContextInput,
    _EffectCoordinatorRunnerResolution,
    _EffectCoordinatorPublication,
    _EffectCoordinatorPublicationPlanning,
    _EffectCoordinatorPublicationRecovery,
    _EffectCoordinatorPublicationRecoveryPolicy,
    _EffectCoordinatorPublicationRecoveryTransaction,
    _EffectCoordinatorPublicationExisting,
    _EffectCoordinatorPublicationPreparation,
    _EffectCoordinatorPublicationCommitment,
    _EffectCoordinatorPublicationTransition,
    _EffectCoordinatorFoundation,
    _EffectCoordinatorDelivery,
    _EffectCoordinatorRunner,
    _EffectCoordinatorAuthorityRecovery,
    _EffectCoordinatorOrchestration,
    _EffectCoordinatorReconciliation,
    _EffectCoordinatorAdmission,
):
    """Make one monotonic external-effect reconciliation decision per call."""

    MAX_DUE_PER_SCAN = 128

    def __init__(
        self,
        *,
        runtime: GraphRuntime,
        catalog: RunCatalog,
        ledger: EffectLedger,
        leases: LeaseStore,
        runners: Mapping[str, RunnerAdapter],
        authority: EffectAuthorityGate,
        artifacts: ArtifactRegistry | None = None,
        publisher: ProjectPublisher | None = None,
        publisher_for: Callable[[RunBinding], ProjectPublisher] | None = None,
        manual: ManualProvider | None = None,
        snapshot_resolver: RuntimeSnapshotResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        owner_factory: Callable[[], str] | None = None,
        lease_ttl: float = 30.0,
    ) -> None:
        self._runtime = runtime
        self._catalog = catalog
        self._ledger = ledger
        self._leases = leases
        self._runners = dict(runners)
        self._runner_bindings = {
            runner.binding_digest: runner for runner in self._runners.values()
        }
        self._authority = authority
        self._artifacts = artifacts
        self._publisher = publisher
        self._publisher_resolver = publisher_for
        self._manual = manual
        self._snapshot_resolver = snapshot_resolver
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner_factory = owner_factory or (lambda: secrets.token_hex(16))
        self._lease_ttl = lease_ttl


_validation_module.EffectCoordinator = EffectCoordinator
