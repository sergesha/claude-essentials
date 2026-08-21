"""Public scenario application service over native checkpoints."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Mapping
from contextlib import contextmanager
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
from lockstep.runtime import config, sessions
from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.effects.authority import (
    EffectAuthorityGate,
    EffectAuthorityUnavailable,
)
from lockstep.runtime.effects.coordinator import EffectCoordinator
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_descriptor,
)
from lockstep.runtime.effects.ledger import EffectLedger
from lockstep.runtime.effects.models import EffectDescriptor, ScopeDescriptor
from lockstep.runtime.graph_runtime import (
    GraphRuntime,
    NativeCoordinateRejected,
    NativeHistoryLimitExceeded,
)
from lockstep.runtime.invocation_lock import InvocationLockStore
from lockstep.runtime.leases import LeaseStore
from lockstep.runtime.owner_state import ensure_owner_directory, initialize_owner_state
from lockstep.runtime.payload_limits import PayloadLimitExceeded, bounded_json
from lockstep.runtime.providers.base import RunnerAdapter
from lockstep.runtime.providers.manual import (
    ManualProvider,
    ManualProviderError,
    ManualSubmission,
)
from lockstep.runtime.recipe_bundles import RecipeBundleStore
from lockstep.runtime.status import ScenarioStatus, project_status
from lockstep.runtime.storage import SQLiteStore

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RESERVED_START_KEYS = frozenset({"namespace"})


class LockstepError(RuntimeError):
    pass


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
        candidate = StrictRecipeIngress(source.parent).inspect(source.name)
        if (
            compiler_provenance is not None
            and candidate.source_bundle_sha256
            != compiler_provenance.source_bundle_sha256
        ):
            raise RecipeAuthorityError(
                "compiler provenance does not bind the exact source bundle"
            )
        authorized = candidate.authorize(
            authority_policy or RecipeAuthorityPolicy()
        )
        with tempfile.TemporaryDirectory(prefix="lockstep-preflight-") as raw:
            store = RecipeBundleStore(Path(raw) / "owner-state")
            materialized = authorized.capture(store).materialize(store)
            if compiler_provenance is None:
                errors, _warnings = profile.check_recipe_full(
                    materialized.source_path
                )
            else:
                errors, _warnings = profile.check_recipe_full(
                    materialized.source_path, provenance=compiler_provenance
                )
    except (OSError, ValueError, RecipeError, RecipeAuthorityError) as exc:
        raise LockstepError(str(exc)) from exc
    if errors:
        raise LockstepError("recipe failed Lockstep profile: " + "; ".join(errors))
    return authorized


class LockstepService:
    _MAX_ENGINE_PROGRESS_DECISIONS = 32
    _MAX_ACTIVE_EFFECT_RUNS = 128

    def __init__(
        self,
        state_dir: Path,
        recipes_dir: Path,
        *,
        authority_policy: RecipeAuthorityPolicy | None = None,
        runners: Mapping[str, RunnerAdapter] | None = None,
        effect_authority: EffectAuthorityGate | None = None,
    ) -> None:
        self.state_dir = initialize_owner_state(Path(state_dir).resolve())
        self.recipes_dir = Path(recipes_dir).resolve()
        self.authority_policy = authority_policy or RecipeAuthorityPolicy()
        self.store = SQLiteStore(self.state_dir / "runtime.sqlite")
        self.catalog = RunCatalog(self.store)
        self.bundle_store = RecipeBundleStore(self.state_dir)
        self.leases = LeaseStore(self.store)
        self.effects = EffectLedger(self.store)
        self.blobs = BlobStore(self.state_dir)
        self.manual = ManualProvider(self.state_dir, self.blobs)
        checkpoints = ensure_owner_directory(self.state_dir, "checkpoints")
        self.checkpoint_path = checkpoints / "native.sqlite"
        self.runtime = GraphRuntime(
            bundle_store=self.bundle_store,
            leases=self.leases,
            invocations=InvocationLockStore(self.state_dir, timeout=60.0),
            checkpoint_path=self.checkpoint_path,
            app_factory=open_native_app,
        )
        self.coordinator = EffectCoordinator(
            runtime=self.runtime,
            catalog=self.catalog,
            ledger=self.effects,
            leases=self.leases,
            runners={} if runners is None else runners,
            authority=effect_authority or _UnavailableEffectAuthority(),
            manual=self.manual,
        )
        self._wait_clock = time.monotonic
        self._wait_sleep = time.sleep
        self._pump_stop = threading.Event()
        self._pump_wakeup = threading.Event()
        self._active_effect_runs: set[str] = set()
        self._queued_effect_runs: set[str] = set()
        self._active_effect_queue: deque[str] = deque()
        self._active_effect_lock = threading.Lock()
        self._recovery_thread_cursor: str | None = None
        self._pump_thread: threading.Thread | None = None
        self._pump_failure: BaseException | None = None
        self._closed = False
        self._recover_engine_effects()
        self._pump_thread = threading.Thread(
            target=self._completion_pump,
            name="lockstep-effect-completion",
            daemon=True,
        )
        self._pump_thread.start()

    def _recover_engine_effects(self) -> None:
        """Adopt durable protected work without a scheduler or status side effect."""

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
            binding = self.catalog.get(watch.public_run_id)
            if not self._reserve_effect_run(binding.public_run_id):
                return
            try:
                encoded = self.blobs.read(watch.input_blob)
                values = validate_start_input(json.loads(encoded))
                if self._canonical_start_input(values) != encoded:
                    raise LockstepError("start admission input is not canonical")
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
                self._recover_start_admissions()
                self._recover_effect_batch()
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
        project_root = Path(project).resolve()
        if self.state_dir == project_root or project_root in self.state_dir.parents:
            raise LockstepError("owner state must be outside the writable project")
        if (
            compiler_provenance is not None
            and authorized.source_bundle_sha256
            != compiler_provenance.source_bundle_sha256
        ):
            raise LockstepError(
                "compiler provenance does not bind the exact source bundle"
            )
        with tempfile.TemporaryDirectory(prefix="lockstep-start-profile-") as raw:
            staged = Path(raw)
            for item in authorized.files:
                target = staged / item.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item.bytes)
            errors, _warnings = profile.check_recipe_full(
                staged / authorized.root,
                provenance=compiler_provenance,
            )
        if errors:
            raise LockstepError("recipe failed Lockstep profile: " + "; ".join(errors))
        input_blob = self.blobs.put(self._canonical_start_input(values))
        admitted = authorized.capture(self.bundle_store)
        materialized = admitted.materialize(self.bundle_store)

        run_id = f"{recipe}-{uuid.uuid4().hex}"
        binding = RunBinding(
            public_run_id=run_id,
            thread_id=f"thread-{uuid.uuid4().hex}",
            recipe_digest=admitted.definition_sha256,
            recipe_snapshot_ref=admitted.bundle.digest,
            project_identity=str(project_root),
        )
        try:
            binding, _admission = self.effects.admit_start(
                self.catalog, binding, input_blob
            )
            # Catalog admission canonicalizes immutable lineage (including
            # created_at). Bind only that admitted value so the coordinator
            # never observes a pre-admission lookalike.
            self.runtime.bind(binding)
            if not self._reserve_effect_run(run_id):
                snapshot = self.runtime.snapshot(run_id, subgraphs=True)
                self.runtime.unbind(run_id)
                return project_status(
                    binding, snapshot, self.leases, self.effects
                ).to_dict()
            snapshot = self.runtime.ensure_started(run_id, values)
        except BaseException:
            self._deactivate_effect_run(run_id)
            self.runtime.unbind(run_id)
            raise
        return self._drive_engine_owned(
            binding.public_run_id, binding=binding, snapshot=snapshot
        ).to_dict()

    def _drive_engine_owned(
        self,
        run_id: str,
        *,
        binding: RunBinding | None = None,
        snapshot=None,
    ) -> ScenarioStatus:
        """Advance only coordinator-owned effects through monotonic decisions."""

        current_binding = binding or self.catalog.get(run_id)
        current_snapshot = snapshot or self.runtime.snapshot(run_id, subgraphs=True)
        for _decision in range(self._MAX_ENGINE_PROGRESS_DECISIONS):
            status = project_status(
                current_binding, current_snapshot, self.leases, self.effects
            )
            protected = tuple(
                (interrupt, descriptor)
                for interrupt in current_snapshot.pending
                if (descriptor := self._protected_interrupt_descriptor(interrupt))
                is not None
            )
            if not protected or (
                status.status == "awaiting" and status.owner == "worker"
            ):
                self._ack_start_if_observable(
                    current_binding, current_snapshot, protected
                )
                self._deactivate_effect_run(run_id)
                return status
            if any(
                isinstance(descriptor, EffectDescriptor)
                and descriptor.runner is not None
                for _interrupt, descriptor in protected
            ) and not self._reserve_effect_run(run_id):
                return status
            report = self.coordinator.reconcile(run_id)
            if report.action == "awaiting_delivery":
                self.coordinator.deliver_ready(run_id)
                delivered_snapshot = self.runtime.snapshot(run_id, subgraphs=True)
                source_coordinates = {
                    interrupt.coordinate for interrupt, _descriptor in protected
                }
                if any(
                    interrupt.coordinate in source_coordinates
                    for interrupt in delivered_snapshot.pending
                ):
                    self._activate_effect_run(run_id)
                    return project_status(
                        current_binding,
                        delivered_snapshot,
                        self.leases,
                        self.effects,
                    )
                current_snapshot = delivered_snapshot
            else:
                current_snapshot = self.runtime.snapshot(run_id, subgraphs=True)
            status = project_status(
                current_binding, current_snapshot, self.leases, self.effects
            )
            if status.status == "awaiting" and status.owner == "worker":
                current_protected = tuple(
                    (interrupt, descriptor)
                    for interrupt in current_snapshot.pending
                    if (descriptor := self._protected_interrupt_descriptor(interrupt))
                    is not None
                )
                self._ack_start_if_observable(
                    current_binding, current_snapshot, current_protected
                )
                self._deactivate_effect_run(run_id)
                return status
            if report.action not in {
                "prepared",
                "launch_claimed",
                "sealed",
                "delivered",
                "awaiting_delivery",
            }:
                if report.action in {"running", "quiescence_pending", "busy"}:
                    self._activate_effect_run(run_id)
                else:
                    self._deactivate_effect_run(run_id)
                current_protected = tuple(
                    (interrupt, descriptor)
                    for interrupt in current_snapshot.pending
                    if (descriptor := self._protected_interrupt_descriptor(interrupt))
                    is not None
                )
                self._ack_start_if_observable(
                    current_binding, current_snapshot, current_protected
                )
                return status
        raise LockstepError(
            "engine-owned progress exceeded its bounded decision budget"
        )

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

    def status(self, run_id: str, project: str) -> dict[str, Any]:
        _binding, status = self._snapshot_status(run_id, project)
        return status.to_dict()

    def scenario_status(self, run_id: str, project: str) -> dict[str, Any]:
        """Explicit public name for the read-only native status projection."""

        return self.status(run_id, project)

    @staticmethod
    def _status_revision(value: Mapping[str, Any]) -> str:
        try:
            admitted = bounded_json(value, label="scenario wait observation")
            encoded = json.dumps(
                admitted,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (PayloadLimitExceeded, TypeError, ValueError) as exc:
            raise LockstepError("scenario wait observation is invalid") from exc
        return "revision:" + hashlib.sha256(encoded).hexdigest()

    def scenario_wait(
        self, run_id: str, timeout_seconds: int, project: str
    ) -> dict[str, Any]:
        """Observe status changes without invoking any mutation/recovery port."""

        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise LockstepError("scenario wait timeout must be an integer from 1 to 60")
        initial = self.scenario_status(run_id, project)
        initial_revision = self._status_revision(initial)
        deadline = self._wait_clock() + timeout_seconds
        current = initial
        while True:
            remaining = deadline - self._wait_clock()
            if remaining <= 0:
                return {
                    **current,
                    "changed": False,
                    "revision": initial_revision,
                }
            self._wait_sleep(min(0.1, remaining))
            current = self.scenario_status(run_id, project)
            revision = self._status_revision(current)
            if revision != initial_revision:
                return {**current, "changed": True, "revision": revision}

    def history(self, run_id: str, project: str) -> list[dict[str, Any]]:
        binding = self._bind_existing(run_id, project)
        try:
            return [
                {
                    "checkpoint_id": item.checkpoint_id,
                    "checkpoint_ns": item.checkpoint_ns,
                    "created_at": item.created_at,
                    "status": project_status(binding, item, (), ()).status,
                }
                for item in self.runtime.history(run_id)
            ]
        except NativeHistoryLimitExceeded as exc:
            raise LockstepError(str(exc)) from exc

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
        descriptor = LockstepService._protected_interrupt_descriptor(interrupt)
        return descriptor if isinstance(descriptor, EffectDescriptor) else None

    def require_session(
        self, run_id: str, session_id: str | None, project: str
    ) -> None:
        """Fail closed at an external mutation edge; resume rechecks it too."""
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
        self._bind_existing(run_id, project)
        try:
            with sessions.locked_owner(
                self.state_dir,
                run_id,
                session_id,
                config.session_stale_minutes(),
            ):
                binding, interrupt = self._worker_interrupt(run_id, step, project)
                descriptor = self._protected_descriptor(interrupt)
                if descriptor is not None:
                    if descriptor.kind != "manual" or manual_submission is None:
                        raise LockstepError(
                            "worker submission cannot target an engine-owned effect"
                        )
                    effect_id = derive_effect_id(
                        interrupt.coordinate, descriptor.digest
                    )
                    assert session_id is not None
                    session_lease = self.leases.acquire(
                        "session",
                        effect_id,
                        session_id,
                        config.session_stale_minutes() * 60,
                    )
                    try:
                        self.coordinator.submit_manual(
                            run_id, interrupt.coordinate, manual_submission
                        )
                    finally:
                        self.leases.release(session_lease)
                    return self._drive_engine_owned(run_id, binding=binding).to_dict()
                snapshot = self.runtime.resume(
                    run_id,
                    interrupt.coordinate,
                    {interrupt.coordinate.interrupt_id: dict(result)},
                )
        except PermissionError as exc:
            raise LockstepError(str(exc)) from exc
        except (NativeCoordinateRejected, NativeHistoryLimitExceeded) as exc:
            raise LockstepError(str(exc)) from exc
        return project_status(binding, snapshot, (), ()).to_dict()

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

    def list_runs(self, project: str) -> list[dict[str, Any]]:
        bindings = self.catalog.list(str(Path(project).resolve()))
        return [self.status(item.public_run_id, project) for item in bindings]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pump_stop.set()
        self._pump_wakeup.set()
        if self._pump_thread is not None:
            self._pump_thread.join()
        try:
            self.runtime.close()
        finally:
            self.store.close()
