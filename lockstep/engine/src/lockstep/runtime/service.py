"""Public scenario application service over native checkpoints."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
from collections import deque
from collections.abc import Mapping
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
    EffectAuthorityUnavailable,
)
from lockstep.runtime.effects.coordinator import EffectCoordinator
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_descriptor,
)
from lockstep.runtime.effects.ledger import EffectLedger
from lockstep.runtime.effects.models import EffectDescriptor, ScopeDescriptor
from lockstep.runtime.effects.models import AcceptDescriptor
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.engine_drive_service import EngineDriveService
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
    ManualProviderError,
    ManualSubmission,
)
from lockstep.runtime.project_snapshots import ProjectSnapshotStore
from lockstep.runtime.snapshot_resolver import (
    RuntimeSnapshotFacts,
    RuntimeSnapshotResolver,
)
from lockstep.runtime.start_service import (
    AuthorizedStartService,
    _preflight_runtime_requirements,
    plan_authorized_start,
)
from lockstep.runtime.worker_submission_service import WorkerSubmissionService
from lockstep.runtime.publication import ProjectPublisher
from lockstep.runtime.recipe_bundles import RecipeBundleStore
from lockstep.runtime.status import ScenarioStatus, project_status
from lockstep.runtime.storage import SQLiteStore

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RESERVED_START_KEYS = frozenset({"namespace"})


class _UnavailableEffectAuthority:
    """Production default: process effects require an owner composition root."""

    def resolve(self, _intent):
        raise EffectAuthorityUnavailable("no process effect authority is configured")

    @contextmanager
    def commitment(self, _grant, _request, _launch):
        raise EffectAuthorityUnavailable("no process effect authority is configured")
        yield  # pragma: no cover


def validate_start_input(input: Mapping[object, object] | None) -> dict[str, Any]:
    try:
        values = bounded_json({} if input is None else input, label="scenario input")
    except PayloadLimitExceeded as exc:
        raise LockstepError(str(exc)) from exc
    if not isinstance(values, dict):
        raise LockstepError("scenario input must be a JSON object")
    forbidden = sorted(
        str(key)
        for key in values
        if not isinstance(key, str)
        or key.startswith(("_", "lockstep_"))
        or key in _RESERVED_START_KEYS
    )
    if forbidden:
        raise LockstepError(f"reserved scenario input keys are forbidden: {forbidden}")
    return values


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
        self._configured_runners: dict[str, object] = {}
        self._configured_effect_authority = None
        self._activation_lock = threading.RLock()
        self._writable_core_active = False
        self._closed = False
        self._pump_stop = threading.Event()
        self._pump_wakeup = threading.Event()
        self._active_effect_runs: set[str] = set()
        self._static_prelaunch_parks: set[str] = set()
        self._queued_effect_runs: set[str] = set()
        self._active_effect_queue: deque[str] = deque()
        self._active_effect_lock = threading.Lock()
        # A newly durable dispatch watch must be adopted by exactly one drive.
        # Serialize foreground admission with recovery enumeration so the pump
        # cannot finish and unbind a run between two foreground app uses.
        self._admission_recovery_lock = threading.RLock()
        self._recovery_thread_cursor: str | None = None
        # Ephemeral scan progress only; effect phase and native state remain the
        # durable authorities.  Project cursors prevent bounded operator sweeps
        # from repeatedly selecting the same foreign/earlier active threads.
        self._scenario_recovery_cursors: dict[str, str] = {}
        self._pump_thread: threading.Thread | None = None
        self._pump_failure: BaseException | None = None

    def _open_writable_stores(self) -> None:
        self.state_dir = initialize_owner_state(self.state_dir)
        self.store = SQLiteStore(self.state_dir / "runtime.sqlite")
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

    def _open_effect_coordinator(self) -> None:
        self.authority = OwnerConsentAuthority(
            self.store,
            delegate=(
                self._configured_effect_authority
                or _UnavailableEffectAuthority()
            ),
        )
        self.coordinator = EffectCoordinator(
            runtime=self.runtime,
            catalog=self.catalog,
            ledger=self.effects,
            leases=self.leases,
            runners=self._configured_runners,
            authority=self.authority,
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

    def _activate_writable_core(self) -> None:
        """Privately create command resources at the first writable intent."""

        with self._activation_lock:
            if self._writable_core_active:
                return
            if self._closed:
                raise LockstepError("command service is closed")
            try:
                self._open_writable_stores()
                self._open_graph_runtime()
                self._open_effect_coordinator()
                self._recover_engine_effects()
                self._pump_thread = threading.Thread(
                    target=self._completion_pump,
                    name="lockstep-effect-completion",
                    daemon=True,
                )
                self._pump_thread.start()
            except BaseException:
                self._rollback_writable_core_activation()
                raise
            self._writable_core_active = True

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
            self._queued_effect_runs.clear()
            self._active_effect_queue.clear()
        self._recovery_thread_cursor = None
        self._pump_thread = None
        self._pump_failure = None
        self._pump_stop.clear()
        self._pump_wakeup.clear()
        self._writable_core_active = False

    def _require_owner_runtime_policy(self, index):
        """Use the production owner snapshot boundary for static admission."""

        return _preflight_runtime_requirements(self.state_dir, index)

    def _recover_engine_effects(self) -> None:
        """Adopt durable protected work without a scheduler or status side effect."""

        with self._admission_recovery_lock:
            self._recover_start_admissions()
            self._recover_effect_batch()

    def _recover_effect_batch(self) -> None:
        thread_ids = self.effects.list_recovery_threads(
            limit=self._MAX_ACTIVE_EFFECT_RUNS,
            after_thread_id=self._recovery_thread_cursor,
        )
        if not thread_ids:
            self._recovery_thread_cursor = None
            return
        for thread_id in thread_ids:
            binding = self.catalog.find_by_thread(thread_id)
            run_id = binding.public_run_id
            if not self._reserve_effect_run(run_id):
                return
            self.runtime.bind(binding)
            self._drive_engine_owned(run_id)
            self._recovery_thread_cursor = thread_id
            with self._active_effect_lock:
                active = run_id in self._active_effect_runs
            if not active:
                self.runtime.unbind(run_id)

    def _recover_start_admissions(self) -> None:
        for watch in self.effects.list_dispatch_watches(
            limit=self._MAX_ACTIVE_EFFECT_RUNS
        ):
            if watch.public_run_id in getattr(self, "_static_prelaunch_parks", ()):
                continue
            binding = self.catalog.get(watch.public_run_id)
            if not self._reserve_effect_run(binding.public_run_id):
                return
            try:
                encoded = self.blobs.read(watch.input_blob)
                values = validate_start_input(json.loads(encoded))
                if self._canonical_start_input(values) != encoded:
                    raise LockstepError("start admission input is not canonical")
                resolver = getattr(self, "snapshot_resolver", None)
                if resolver is not None:
                    resolver.start_ref(binding)
                self.runtime.bind(binding)
                snapshot = self.runtime.ensure_started(binding.public_run_id, values)
                self._drive_engine_owned(
                    binding.public_run_id, binding=binding, snapshot=snapshot
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self._deactivate_effect_run(binding.public_run_id)
                self.runtime.unbind(binding.public_run_id)
                raise LockstepError("start admission input integrity failure") from exc
            except BaseException:
                self._deactivate_effect_run(binding.public_run_id)
                self.runtime.unbind(binding.public_run_id)
                raise
            with self._active_effect_lock:
                active = binding.public_run_id in self._active_effect_runs
            if not active:
                self.runtime.unbind(binding.public_run_id)

    def _reserve_effect_run(self, run_id: str) -> bool:
        with self._active_effect_lock:
            if run_id in self._active_effect_runs:
                return True
            if len(self._active_effect_runs) >= self._MAX_ACTIVE_EFFECT_RUNS:
                return False
            self._active_effect_runs.add(run_id)
            return True

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

    @staticmethod
    def _canonical_start_input(values: Mapping[str, Any]) -> bytes:
        try:
            admitted = validate_start_input(values)
            return json.dumps(
                admitted,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise LockstepError("scenario input is not canonically encodable") from exc

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
        self._activate_writable_core()
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
            park_prelaunch=self._static_prelaunch_parks.add,
            drive_engine_owned=self._drive_engine_owned,
        ).start(
            recipe,
            plan,
            values,
            canonical_input=self._canonical_start_input(values),
        )

    def _drive_engine_owned(
        self,
        run_id: str,
        *,
        binding: RunBinding | None = None,
        snapshot=None,
    ) -> ScenarioStatus:
        """Advance only coordinator-owned effects through monotonic decisions."""
        return EngineDriveService(
            runtime=self.runtime,
            catalog=getattr(self, "catalog", None),
            leases=self.leases,
            effects=self.effects,
            coordinator=self.coordinator,
            max_decisions=self._MAX_ENGINE_PROGRESS_DECISIONS,
            protected_descriptor=self._protected_interrupt_descriptor,
            reserve_effect_run=self._reserve_effect_run,
            activate_effect_run=self._activate_effect_run,
            deactivate_effect_run=self._deactivate_effect_run,
            acknowledge_start=self._ack_start_if_observable,
        ).drive(run_id, binding=binding, snapshot=snapshot)

    def _ack_start_if_observable(
        self,
        binding: RunBinding,
        snapshot,
        protected: tuple[tuple[object, EffectDescriptor | ScopeDescriptor], ...],
    ) -> None:
        if not snapshot.pending and not snapshot.next:
            self.effects.acknowledge_dispatch_watch(binding.public_run_id)
            return
        if snapshot.pending and not protected:
            self.effects.acknowledge_dispatch_watch(binding.public_run_id)
            return
        for interrupt, descriptor in protected:
            effect_id = derive_effect_id(interrupt.coordinate, descriptor.digest)
            try:
                record = self.effects.get(effect_id)
            except KeyError:
                return
            if (
                record.coordinate != interrupt.coordinate
                or record.descriptor_digest != descriptor.digest
            ):
                raise LockstepError("start admission effect binding mismatch")
            if isinstance(descriptor, EffectDescriptor) and descriptor.kind == "manual":
                try:
                    handoff = self.manual.lookup(effect_id)
                except (KeyError, ValueError, ManualProviderError) as exc:
                    raise LockstepError(
                        "start admission manual handoff integrity failure"
                    ) from exc
                if (
                    handoff.coordinate != interrupt.coordinate
                    or handoff.descriptor_digest != descriptor.digest
                ):
                    raise LockstepError(
                        "start admission manual handoff binding mismatch"
                    )
        if protected:
            self.effects.acknowledge_dispatch_watch(binding.public_run_id)

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
        with self._admission_recovery_lock:
            cursor = self._scenario_recovery_cursors.get(project_identity)
            thread_ids = self.effects.list_recovery_threads(
                limit=limit, after_thread_id=cursor
            )
            if not thread_ids:
                self._scenario_recovery_cursors.pop(project_identity, None)
            for thread_id in thread_ids:
                binding = self.catalog.find_by_thread(thread_id)
                if binding.project_identity != project_identity:
                    self._scenario_recovery_cursors[project_identity] = thread_id
                    continue
                if not self._reserve_effect_run(binding.public_run_id):
                    break
                self.runtime.bind(binding)
                self._drive_engine_owned(binding.public_run_id, binding=binding)
                recovered.append(binding.public_run_id)
                self._scenario_recovery_cursors[project_identity] = thread_id
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
    ) -> EffectDescriptor | ScopeDescriptor | None:
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
        self._activate_writable_core()
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
