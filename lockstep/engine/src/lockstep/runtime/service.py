"""Public scenario application service over native checkpoints."""

from __future__ import annotations

import hashlib
import re
import tempfile
import threading
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

from lockstep.recipe import profile
from lockstep.recipe.authority import (
    AuthorizedRecipe,
    RecipeAuthorityError,
    RecipeAuthorityPolicy,
    StrictRecipeIngress,
)
from lockstep.recipe.loader import RecipeError, RecipeLoader
from lockstep.recipe.yamlgraph_adapter import open_native_app
from lockstep.authoring import AuthoringError, classify_generated_recipe
from lockstep.runtime import config, sessions
from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.artifacts import ArtifactRegistry
from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.effects.authority import (
    EffectAuthorityDenied,
    EffectAuthorityGate,
    EffectAuthorityUnavailable,
)
from lockstep.runtime.effects.coordinator import EffectCoordinator
from lockstep.runtime.effects.descriptors import (
    parse_effect_descriptor,
)
from lockstep.runtime.effects.ledger import EffectLedger
from lockstep.runtime.effects.models import EffectDescriptor
from lockstep.runtime.effects.models import AcceptDescriptor
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.engine_drive_service import (
    EngineDriveService,
    ProtectedDescriptor,
)
from lockstep.runtime.effects.owner_consent import (
    IssuedPublicationConsent,
    OwnerConsentAuthority,
)
from lockstep.runtime.graph_runtime import (
    GraphRuntime,
)
from lockstep.runtime.invocation_lock import InvocationLockStore
from lockstep.runtime.leases import LeaseStore
from lockstep.runtime.owner_state import ensure_owner_directory, initialize_owner_state
from lockstep.runtime.payload_limits import PayloadLimitExceeded, bounded_json
from lockstep.runtime.providers.manual import (
    ManualProvider,
    ManualSubmission,
)
from lockstep.runtime.providers.base import RunnerAdapter
from lockstep.runtime.project_snapshots import ProjectSnapshotStore
from lockstep.runtime.snapshot_resolver import (
    RuntimeSnapshotFacts,
    RuntimeSnapshotResolver,
)
from lockstep.runtime.start_service import (
    AuthorizedStartService,
    _WritableCoreActivation,
    plan_authorized_start,
)
from lockstep.runtime.worker_submission_service import WorkerSubmissionService
from lockstep.runtime.publication import ProjectPublisher
from lockstep.runtime.recipe_bundles import RecipeBundleStore
from lockstep.runtime.runtime_execution import (
    RuntimeExecutionAdmission,
    RuntimeExecutionContext,
    build_runtime_execution_composition,
    capture_runtime_execution_admission,
)
from lockstep.runtime.runtime_execution_recovery import RuntimeExecutionRecovery
from lockstep.runtime.recovery_driver import RecoveryDriver as _RecoveryDriver
from lockstep.runtime.status import ScenarioStatus, project_status
from lockstep.runtime.storage import RuntimeSchemaMigrator, SQLiteStore
from lockstep.runtime.start_input import (
    canonical_start_input,
    validate_start_input,
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class _UnavailableEffectAuthority:
    """Production default: process effects require an owner composition root."""

    def resolve(self, _intent):
        raise EffectAuthorityUnavailable("no process effect authority is configured")

    @contextmanager
    def commitment(self, _grant, _request, _launch):
        raise EffectAuthorityUnavailable("no process effect authority is configured")
        yield  # pragma: no cover


def validate_evidence_payload(evidence: object) -> dict[str, Any]:
    value = validate_evidence_shape(evidence)
    if any(key.startswith("_") for key in value):
        raise LockstepError("reserved evidence keys are forbidden")
    return value


def validate_evidence_shape(evidence: object) -> dict[str, Any]:
    try:
        value = bounded_json(evidence, label="scenario evidence")
    except PayloadLimitExceeded as exc:
        raise LockstepError(str(exc)) from exc
    if not isinstance(value, dict):
        raise LockstepError("scenario evidence must be a JSON object")
    return value


def validate_reason_payload(reason: object) -> str:
    try:
        value = bounded_json(reason, label="scenario reason")
    except PayloadLimitExceeded as exc:
        raise LockstepError(str(exc)) from exc
    if not isinstance(value, str):
        raise LockstepError("scenario reason must be a string")
    return value


def preflight_recipe(
    recipes_dir: Path,
    name: str,
    *,
    authority_policy: RecipeAuthorityPolicy | None = None,
    compiler_provenance: profile.CompilerProvenance | None = None,
) -> AuthorizedRecipe:
    """Pure admission/profile boundary: no persistent Lockstep state exists yet."""
    if not _NAME_RE.fullmatch(name or ""):
        raise LockstepError(f"invalid recipe name {name!r}")
    try:
        source = RecipeLoader(Path(recipes_dir).resolve()).resolve(name).path
        # A trusted same-process compiler capability already binds the exact
        # executable bundle (used by embedders/tests before files are checked
        # into the conventional project layout).  Public file ingress has no
        # such capability and must mint canonical-match from source instead.
        canonical_proof = (
            None
            if compiler_provenance is not None
            else classify_generated_recipe(Path(recipes_dir).resolve(), name, source)
        )
        if compiler_provenance is not None and canonical_proof is not None:
            if compiler_provenance.source_bundle_sha256 != canonical_proof.source_bundle_sha256:
                raise RecipeAuthorityError(
                    "supplied compiler provenance does not match canonical source"
                )
        effective_provenance = canonical_proof or compiler_provenance
        candidate = StrictRecipeIngress(source.parent).inspect(source.name)
        if (
            effective_provenance is not None
            and candidate.source_bundle_sha256
            != effective_provenance.source_bundle_sha256
        ):
            raise RecipeAuthorityError(
                "compiler provenance does not bind the exact source bundle"
            )
        authorized = candidate.authorize(
            authority_policy or RecipeAuthorityPolicy()
        )
        authorized = replace(
            authorized, canonical_match_proof=canonical_proof
        )
        with tempfile.TemporaryDirectory(prefix="lockstep-preflight-") as raw:
            store = RecipeBundleStore(Path(raw) / "owner-state")
            materialized = authorized.capture(store).materialize(store)
            if effective_provenance is None:
                errors, _warnings = profile.check_recipe_full(
                    materialized.source_path
                )
            else:
                errors, _warnings = profile.check_recipe_full(
                    materialized.source_path, provenance=effective_provenance
                )
    except (OSError, ValueError, AuthoringError, RecipeError, RecipeAuthorityError) as exc:
        raise LockstepError(str(exc)) from exc
    if errors:
        raise LockstepError("recipe failed Lockstep profile: " + "; ".join(errors))
    return authorized


class LockstepCommandService:
    _MAX_ENGINE_PROGRESS_DECISIONS = 32
    _MAX_ACTIVE_EFFECT_RUNS = 128

    def __init__(
        self,
        state_dir: Path,
        recipes_dir: Path,
        *,
        authority_policy: RecipeAuthorityPolicy | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.recipes_dir = Path(recipes_dir).resolve()
        self.authority_policy = authority_policy or RecipeAuthorityPolicy()
        self._runtime_execution_context: RuntimeExecutionContext | None = None
        self._runtime_execution_composition = None
        self._recovery_driver: _RecoveryDriver | None = None
        self._activation_lock = threading.RLock()
        self._writable_core_active = False
        self._closed = False
        self._pump_stop = threading.Event()
        self._pump_wakeup = threading.Event()
        self._active_effect_runs: set[str] = set()
        self._owned_effect_bindings: set[str] = set()
        self._initial_recovery_exclusion: str | None = None
        self._queued_effect_runs: set[str] = set()
        self._active_effect_queue: deque[str] = deque()
        self._active_effect_lock = threading.Lock()
        # A newly durable dispatch watch must be adopted by exactly one drive.
        # Serialize foreground admission with recovery enumeration so the pump
        # cannot finish and unbind a run between two foreground app uses.
        self._admission_recovery_lock = threading.RLock()
        self._pump_thread: threading.Thread | None = None
        self._pump_failure: BaseException | None = None
        self._start_activation = _WritableCoreActivation(
            lock=self._activation_lock,
            admission_lock=self._admission_recovery_lock,
            is_active=lambda: self._writable_core_active,
            is_closed=lambda: self._closed,
            prepare=self._prepare_writable_core,
            finish=self._finish_writable_core_activation,
            rollback=self._rollback_writable_core_activation,
            record_degraded=lambda exc: setattr(self, "_pump_failure", exc),
        )

    def _open_writable_stores(self) -> None:
        self.state_dir = initialize_owner_state(self.state_dir)
        database = self.state_dir / "runtime.sqlite"
        RuntimeSchemaMigrator.transition_legacy_to_v2(database)
        self.store = SQLiteStore(database)
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
        composition = build_runtime_execution_composition(
            state_dir=self.state_dir,
            context=context,
            catalog=self.catalog,
            bundles=self.bundle_store,
            blobs=self.blobs,
            snapshots=self.snapshots,
        )
        released = composition.runners
        runners = {"codex": released.codex, "pinned": released.pinned}
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

    def _prepare_writable_core(self) -> None:
        """Open complete writable resources without recovery or background work."""

        self._open_writable_stores()
        self._open_graph_runtime()
        if self._runtime_execution_context is None:
            self._runtime_execution_context = (
                self._reconstruct_runtime_execution_context()
            )
        self._open_effect_coordinator()
        self._recovery_driver = _RecoveryDriver(
            catalog=self.catalog,
            runtime=self.runtime,
            effects=self.effects,
            blobs=self.blobs,
            migrator=RuntimeSchemaMigrator(self.store),
            coordinator=self.coordinator,
            snapshot_resolver=self.snapshot_resolver,
            exclude_run_drive=lambda run_id: (
                run_id == self._initial_recovery_exclusion
            ),
            drive_recovered_run=self._drive_recovered_run,
        )

    def _finish_writable_core_activation(
        self, deferred_start_run_id: str | None = None
    ) -> None:
        """Recover old work and publish one fully active writable core."""

        self._initial_recovery_exclusion = deferred_start_run_id
        try:
            self._recover_engine_effects()
        finally:
            self._initial_recovery_exclusion = None
        self._pump_failure = None
        self._pump_thread = threading.Thread(
            target=self._completion_pump,
            name="lockstep-effect-completion",
            daemon=True,
        )
        self._pump_thread.start()
        self._writable_core_active = True

    def _activate_writable_core(self) -> None:
        """Privately create command resources at the first writable intent."""

        with self._activation_lock:
            if self._writable_core_active:
                return
        self._start_activation.activate()

    def _rollback_writable_core_activation(self) -> None:
        """Return a failed first activation to a clean, retryable state."""

        runtime = getattr(self, "runtime", None)
        store = getattr(self, "store", None)
        if runtime is not None:
            with suppress(Exception):
                runtime.close()
        if store is not None:
            with suppress(Exception):
                store.close()
        with self._active_effect_lock:
            self._active_effect_runs.clear()
            self._owned_effect_bindings.clear()
            self._queued_effect_runs.clear()
            self._active_effect_queue.clear()
        self._pump_thread = None
        self._pump_failure = None
        self._pump_stop.clear()
        self._pump_wakeup.clear()
        self._initial_recovery_exclusion = None
        self._runtime_execution_composition = None
        self._runtime_execution_context = None
        self._recovery_driver = None
        self._writable_core_active = False

    def _require_owner_runtime_policy(self, index):
        """Use the production owner snapshot boundary for static admission."""

        try:
            return capture_runtime_execution_admission(self.state_dir, index)
        except FileNotFoundError as exc:
            raise LockstepError("runtime execution policy is unavailable") from exc
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise LockstepError(str(exc)) from exc

    def _configure_runtime_execution(
        self, admission: RuntimeExecutionAdmission | None
    ) -> None:
        if admission is None:
            return
        context = admission.context
        current = self._runtime_execution_context
        if current is not None and current != context:
            raise LockstepError("command runtime execution snapshot changed")
        if current is not None:
            return
        if self._writable_core_active:
            self._install_runtime_execution(context)
        else:
            self._runtime_execution_context = context

    def _recover_engine_effects(self) -> None:
        """Adopt durable protected work without a scheduler or status side effect."""

        self._install_recovered_runtime_execution()
        with self._admission_recovery_lock:
            self._recovery_driver._sweep_run_drive_watches(
                project_identity=None,
                limit=self._MAX_ACTIVE_EFFECT_RUNS,
            )

    def _reserve_effect_run(self, run_id: str) -> bool:
        available, _owned = self._reserve_effect_run_owned(run_id)
        return available

    def _reserve_effect_run_owned(self, run_id: str) -> tuple[bool, bool]:
        with self._active_effect_lock:
            if run_id in self._active_effect_runs:
                return True, False
            if len(self._active_effect_runs) >= self._MAX_ACTIVE_EFFECT_RUNS:
                return False, False
            self._active_effect_runs.add(run_id)
            return True, True

    def _activate_effect_run(self, run_id: str) -> None:
        if not self._reserve_effect_run(run_id):
            return
        with self._active_effect_lock:
            if run_id not in self._queued_effect_runs:
                self._queued_effect_runs.add(run_id)
                self._active_effect_queue.append(run_id)
        self._pump_wakeup.set()

    def _deactivate_effect_run(self, run_id: str) -> None:
        with self._active_effect_lock:
            self._active_effect_runs.discard(run_id)
            self._queued_effect_runs.discard(run_id)

    def _finish_owned_effect_binding(self, run_id: str, owned: bool) -> None:
        if not owned:
            return
        release = False
        with self._active_effect_lock:
            if run_id in self._active_effect_runs:
                self._owned_effect_bindings.add(run_id)
            else:
                release = True
        if release:
            self.runtime.unbind(run_id)

    def _release_inactive_effect_binding(self, run_id: str) -> None:
        release = False
        with self._active_effect_lock:
            if (
                run_id not in self._active_effect_runs
                and run_id in self._owned_effect_bindings
            ):
                self._owned_effect_bindings.discard(run_id)
                release = True
        if release:
            self.runtime.unbind(run_id)

    def _take_active_effect_runs(self, limit: int = 128) -> tuple[str, ...]:
        selected = []
        with self._active_effect_lock:
            while self._active_effect_queue and len(selected) < limit:
                run_id = self._active_effect_queue.popleft()
                if run_id not in self._queued_effect_runs:
                    continue
                self._queued_effect_runs.discard(run_id)
                selected.append(run_id)
        return tuple(selected)

    def _completion_pump(self) -> None:
        """Adopt terminal runner observations through the same coordinator."""

        while not self._pump_stop.is_set():
            self._pump_wakeup.wait(0.25)
            self._pump_wakeup.clear()
            if self._pump_stop.is_set():
                return
            try:
                with self._admission_recovery_lock:
                    for run_id in self._take_active_effect_runs():
                        binding = self.catalog.get(run_id)
                        self.runtime.bind(binding)
                        self._drive_engine_owned(run_id, binding=binding)
                self._recover_engine_effects()
            except Exception as exc:  # noqa: BLE001 - retain cross-provider failure
                self._pump_failure = exc
                return

    def _check_completion_pump(self) -> None:
        if self._pump_failure is not None:
            raise LockstepError(
                "engine-owned completion pump failed"
            ) from self._pump_failure

    def _recipe_path(self, name: str) -> Path:
        if not _NAME_RE.fullmatch(name or ""):
            raise LockstepError(f"invalid recipe name {name!r}")
        try:
            return RecipeLoader(self.recipes_dir).resolve(name).path
        except RecipeError as exc:
            raise LockstepError(str(exc)) from exc

    def recipe_path(self, name: str) -> Path:
        return self._recipe_path(name)

    def _bind_existing(self, run_id: str, project: str) -> RunBinding:
        try:
            binding = self.catalog.get(run_id)
        except KeyError as exc:
            raise LockstepError(f"unknown run {run_id!r}") from exc
        if Path(binding.project_identity).resolve() != Path(project).resolve():
            raise LockstepError(f"unknown run {run_id!r}")
        try:
            self.runtime.bind(binding)
        except Exception as exc:  # immutable binding cannot be reconstructed
            raise LockstepError(
                f"run {run_id}: native binding integrity failure"
            ) from exc
        return binding

    def start(
        self,
        recipe: str,
        input: dict | None,
        project: str,
        *,
        compiler_provenance: profile.CompilerProvenance | None = None,
    ) -> dict[str, Any]:
        values = validate_start_input(input)
        authorized = preflight_recipe(
            self.recipes_dir,
            recipe,
            authority_policy=self.authority_policy,
            compiler_provenance=compiler_provenance,
        )
        return self.start_authorized(
            recipe,
            authorized,
            values,
            project,
            compiler_provenance=compiler_provenance,
        )

    def start_authorized(
        self,
        recipe: str,
        authorized: AuthorizedRecipe,
        input: Mapping[str, Any],
        project: str,
        *,
        compiler_provenance: profile.CompilerProvenance | None = None,
    ) -> dict[str, Any]:
        values = validate_start_input(input)
        plan = plan_authorized_start(
            state_dir=self.state_dir,
            authorized=authorized,
            project=project,
            compiler_provenance=compiler_provenance,
            require_runtime_policy=self._require_owner_runtime_policy,
        )
        deferred_start_run_id: str | None = None

        def persist() -> dict[str, Any]:
            nonlocal deferred_start_run_id
            result = self._authorized_start_service().start(
                recipe,
                plan,
                values,
                canonical_input=canonical_start_input(values),
            )
            run_id = result.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise LockstepError("durable start did not return its run identity")
            deferred_start_run_id = run_id
            return result

        return self._start_activation.start(
            self.state_dir,
            plan.runtime_admission,
            persist,
            lambda: self._configure_runtime_execution(plan.runtime_execution),
            lambda: self._finish_writable_core_activation(deferred_start_run_id),
        )

    def _authorized_start_service(self) -> AuthorizedStartService:
        """Bind the prepared writable core to the focused start use case."""

        return AuthorizedStartService(
            blobs=self.blobs,
            bundle_store=self.bundle_store,
            snapshots=self.snapshots,
            effects=self.effects,
            catalog=self.catalog,
            runtime=self.runtime,
            runtime_snapshot_facts=self.runtime_snapshot_facts,
            leases=self.leases,
            admission_lock=self._admission_recovery_lock,
            reserve_effect_run=self._reserve_effect_run,
            deactivate_effect_run=self._deactivate_effect_run,
            drive_engine_owned=self._drive_engine_owned,
        )

    def _drive_engine_owned(
        self,
        run_id: str,
        *,
        binding: RunBinding | None = None,
        snapshot=None,
    ) -> ScenarioStatus:
        """Advance only coordinator-owned effects through monotonic decisions."""
        try:
            return self._engine_drive_service().drive(
                run_id, binding=binding, snapshot=snapshot
            )
        finally:
            self._release_inactive_effect_binding(run_id)

    def _drive_recovered_run(self, run_id: str) -> bool:
        """Try one recovered run through the normal authoritative drive owner."""

        binding = self.catalog.get(run_id)
        owns_binding = self.runtime.bind(binding)
        owns_reservation = False

        def reserve_effect_run(recovered_run_id: str) -> bool:
            nonlocal owns_reservation
            available, owned = self._reserve_effect_run_owned(recovered_run_id)
            owns_reservation = owns_reservation or owned
            return available

        try:
            try:
                return self._engine_drive_service(
                    reserve_effect_run=reserve_effect_run
                ).drive_recovered(run_id)
            except BaseException:
                if owns_reservation:
                    self._deactivate_effect_run(run_id)
                raise
        finally:
            self._release_inactive_effect_binding(run_id)
            self._finish_owned_effect_binding(run_id, owns_binding)

    def _engine_drive_service(
        self,
        *,
        reserve_effect_run: Callable[[str], bool] | None = None,
    ) -> EngineDriveService:
        return EngineDriveService(
            runtime=self.runtime,
            catalog=getattr(self, "catalog", None),
            leases=self.leases,
            effects=self.effects,
            coordinator=self.coordinator,
            max_decisions=self._MAX_ENGINE_PROGRESS_DECISIONS,
            protected_descriptor=self._protected_interrupt_descriptor,
            reserve_effect_run=reserve_effect_run or self._reserve_effect_run,
            activate_effect_run=self._activate_effect_run,
            deactivate_effect_run=self._deactivate_effect_run,
        )

    def _snapshot_status(
        self, run_id: str, project: str
    ) -> tuple[RunBinding, ScenarioStatus]:
        self._check_completion_pump()
        binding = self._bind_existing(run_id, project)
        snapshot = self.runtime.snapshot(run_id, subgraphs=True)
        status = project_status(binding, snapshot, self.leases, self.effects)
        if status.status == "awaiting" and status.owner == "worker":
            session_binding = sessions.read_binding(self.state_dir, run_id)
            if not sessions.is_live(session_binding, config.session_stale_minutes()):
                status = replace(
                    status,
                    annotations=status.annotations
                    + (("binding_integrity", "missing_or_stale"),),
                )
        return binding, status

    def scenario_recover(
        self, project: str, *, limit: int = 128
    ) -> dict[str, Any]:
        """Explicitly run one bounded recovery sweep; status/wait/history never do."""

        if type(limit) is not int or not 1 <= limit <= self._MAX_ACTIVE_EFFECT_RUNS:
            raise LockstepError("scenario recover limit must be an integer from 1 to 128")
        project_identity = str(Path(project).resolve())
        self._activate_writable_core()
        recovered: list[str] = []
        with self._activation_lock, self._admission_recovery_lock:
            self._install_recovered_runtime_execution(
                limit=limit
            )
            recovered.extend(
                self._recovery_driver._sweep_run_drive_watches(
                    project_identity=project_identity,
                    limit=limit,
                )
            )
        return {"recovered": recovered, "count": len(recovered), "limit": limit}

    def _worker_interrupt(self, run_id: str, step: str | None, project: str):
        binding, status = self._snapshot_status(run_id, project)
        if status.status != "awaiting" or status.owner != "worker":
            raise LockstepError(f"run {run_id} is not awaiting worker input")
        snapshot = self.runtime.snapshot(run_id, subgraphs=True)
        matches = []
        for interrupt in snapshot.pending:
            value = interrupt.value
            observed_step = value.get("step") if isinstance(value, dict) else None
            descriptor = self._protected_interrupt_descriptor(interrupt)
            protected = descriptor is not None
            selected_step = (
                observed_step
                if observed_step is not None
                else descriptor.logical_id
                if descriptor is not None
                else None
            )
            if (
                step is None
                or selected_step == step
                or (observed_step is None and not protected)
            ):
                matches.append(interrupt)
        if len(matches) != 1:
            raise LockstepError(
                "worker step does not identify exactly one pending interrupt"
            )
        matched = matches[0]
        matched_descriptor = self._protected_descriptor(matched)
        matched_step = (
            matched.value.get("step") if isinstance(matched.value, dict) else None
        ) or (matched_descriptor.logical_id if matched_descriptor is not None else None)
        if step is not None and matched_step is not None and matched_step != step:
            raise LockstepError(f"run {run_id} is parked on another step")
        return binding, matched

    @staticmethod
    def _protected_interrupt_descriptor(
        interrupt,
    ) -> ProtectedDescriptor | None:
        value = interrupt.value
        if not isinstance(value, dict):
            return None
        raw = value.get("lockstep_effect")
        if not isinstance(raw, dict) or raw.get("schema") != "lockstep.effect/v1":
            return None
        try:
            descriptor = parse_effect_descriptor(raw)
        except (TypeError, ValueError) as exc:
            raise LockstepError("invalid protected worker interrupt") from exc
        return descriptor

    @staticmethod
    def _protected_descriptor(interrupt) -> EffectDescriptor | None:
        descriptor = LockstepCommandService._protected_interrupt_descriptor(interrupt)
        return descriptor if isinstance(descriptor, EffectDescriptor) else None

    def require_session(
        self, run_id: str, session_id: str | None, project: str
    ) -> None:
        """Fail closed at an external mutation edge; resume rechecks it too."""
        self._activate_writable_core()
        self._bind_existing(run_id, project)
        try:
            with sessions.locked_owner(
                self.state_dir,
                run_id,
                session_id,
                config.session_stale_minutes(),
            ):
                pass
        except PermissionError as exc:
            raise LockstepError(str(exc)) from exc

    def _resume_worker(
        self,
        run_id: str,
        step: str | None,
        result: Mapping[str, Any],
        *,
        manual_submission: ManualSubmission | None = None,
        session_id: str | None,
        project: str,
    ) -> dict[str, Any]:
        self._activate_writable_core()
        return WorkerSubmissionService(
            state_dir=self.state_dir,
            runtime=self.runtime,
            manual_effect_resources=lambda: (self.leases, self.coordinator),
            admission_lock=self._admission_recovery_lock,
            bind_existing=self._bind_existing,
            select_interrupt=self._worker_interrupt,
            protected_descriptor=self._protected_descriptor,
            drive_engine_owned=self._drive_engine_owned,
        ).resume(
            run_id,
            step,
            result,
            manual_submission=manual_submission,
            session_id=session_id,
            project=project,
        )

    def scenario_done(
        self,
        run_id: str,
        step: str,
        evidence: dict,
        *,
        session_id: str | None,
        project: str,
    ) -> dict[str, Any]:
        checked_evidence = validate_evidence_payload(evidence)
        return self._resume_worker(
            run_id,
            step,
            {
                "schema": "lockstep.worker-result/v1",
                "outcome": "PASS",
                "evidence": checked_evidence,
            },
            manual_submission=ManualSubmission.build("PASS", evidence=checked_evidence),
            session_id=session_id,
            project=project,
        )

    def _pending_acceptance(
        self,
        run_id: str,
        step: str,
        *,
        project: str,
    ):
        if not isinstance(step, str) or not step:
            raise LockstepError("acceptance step must be non-empty text")
        binding = self._bind_existing(run_id, project)
        snapshot = self.runtime.snapshot(run_id, subgraphs=True)
        matches = []
        for interrupt in snapshot.pending:
            descriptor = self._protected_interrupt_descriptor(interrupt)
            observed_step = (
                interrupt.value.get("step")
                if isinstance(interrupt.value, dict)
                else None
            )
            if isinstance(descriptor, AcceptDescriptor) and (
                descriptor.logical_id == step or observed_step == step
            ):
                matches.append(interrupt)
        if len(matches) != 1:
            raise LockstepError(
                "acceptance step does not identify exactly one pending interrupt"
            )
        return binding, matches[0]

    def preview_publication_consent(
        self, run_id: str, step: str, *, project: str
    ) -> dict[str, Any]:
        self._activate_writable_core()
        _binding, interrupt = self._pending_acceptance(
            run_id, step, project=project
        )
        return self.coordinator.preview_acceptance(
            run_id, interrupt.coordinate
        ).to_dict()

    def issue_publication_consent(
        self,
        run_id: str,
        step: str,
        expected_commitment_digest: str,
        *,
        project: str,
    ) -> IssuedPublicationConsent:
        self._activate_writable_core()
        with self._admission_recovery_lock:
            _binding, interrupt = self._pending_acceptance(
                run_id, step, project=project
            )
            return self.coordinator.issue_acceptance_consent(
                run_id,
                interrupt.coordinate,
                expected_commitment_digest,
            )

    def scenario_accept_artifact(
        self, token: str, *, project: str
    ) -> dict[str, Any]:
        """Redeem one owner bearer token within the ambient host project."""

        project_identity = str(Path(project).resolve())
        self._activate_writable_core()
        try:
            stored = self.authority.inspect_token(token)
        except (EffectAuthorityDenied, TypeError, ValueError) as exc:
            raise LockstepError("invalid or stale publication consent") from exc
        commitment = stored.commitment
        if commitment.project_identity != project_identity:
            raise LockstepError("invalid or stale publication consent")
        with self._admission_recovery_lock:
            try:
                binding = self._bind_existing(
                    commitment.public_run_id, project_identity
                )
                if (
                    binding.public_run_id != commitment.public_run_id
                    or binding.project_identity != commitment.project_identity
                    or binding.recipe_digest != commitment.definition_digest
                ):
                    raise LockstepError("invalid or stale publication consent")
                self.coordinator.submit_acceptance(
                    commitment.public_run_id,
                    commitment.source,
                    token,
                )
            except EffectAuthorityDenied as exc:
                raise LockstepError("invalid or stale publication consent") from exc
            return self._drive_engine_owned(
                commitment.public_run_id, binding=binding
            ).to_dict()

    def revoke_publication_consents(self, *, project: str) -> int:
        self._activate_writable_core()
        return self.authority.revoke(str(Path(project).resolve()))

    def scenario_escalate(
        self,
        run_id: str,
        reason: str,
        *,
        session_id: str | None,
        project: str,
    ) -> dict[str, Any]:
        checked_reason = validate_reason_payload(reason)
        return self._resume_worker(
            run_id,
            None,
            {
                "schema": "lockstep.worker-result/v1",
                "outcome": "FAIL",
                "reason": checked_reason,
            },
            manual_submission=ManualSubmission.build("FAIL", reason=checked_reason),
            session_id=session_id,
            project=project,
        )

    def scenario_abort(
        self, run_id: str, *, session_id: str | None, project: str
    ) -> dict[str, Any]:
        return self._resume_worker(
            run_id,
            None,
            {"schema": "lockstep.worker-result/v1", "outcome": "ABORTED"},
            manual_submission=ManualSubmission.build("ABORTED"),
            session_id=session_id,
            project=project,
        )

    # Compatibility vocabulary only; no state or transitions live here.
    def done(
        self,
        run_id: str,
        step: str,
        evidence: dict,
        *,
        session_id: str | None = None,
        project: str,
    ):
        return self.scenario_done(
            run_id, step, evidence, session_id=session_id, project=project
        )

    def escalate(
        self,
        run_id: str,
        reason: str,
        *,
        session_id: str | None = None,
        project: str,
    ):
        return self.scenario_escalate(
            run_id, reason, session_id=session_id, project=project
        )

    def abort(self, run_id: str, *, session_id: str | None = None, project: str):
        return self.scenario_abort(run_id, session_id=session_id, project=project)

    def close(self) -> None:
        with self._activation_lock:
            if self._closed:
                return
            self._closed = True
            if not self._writable_core_active:
                return
            self._writable_core_active = False
            self._pump_stop.set()
            self._pump_wakeup.set()
            pump_thread = self._pump_thread
            runtime = self.runtime
            store = self.store
        if pump_thread is not None:
            pump_thread.join()
        try:
            runtime.close()
        finally:
            store.close()
