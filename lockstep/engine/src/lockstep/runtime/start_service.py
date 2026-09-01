"""Authorized run-start use case over explicit command-side dependencies."""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from lockstep.recipe import profile
from lockstep.recipe.authority import AuthorizedRecipe
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.owner_policy import (
    OwnerRuntimeAuthority,
    RuntimeAdmissionDecision,
    RuntimeRequirementIndex,
    _RuntimeAdmissionChanged,
)
from lockstep.runtime.effects.owner_provisioning import (
    capture_runtime_snapshot_bindings,
)
from lockstep.runtime.effects.owner_snapshot_store import open_runtime_snapshot
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.recipe_bundles import RecipeBundleStore
from lockstep.runtime.snapshot_resolver import capture_authoritative_snapshot
from lockstep.runtime.status import ScenarioStatus, project_status
from lockstep.runtime.runtime_execution import RuntimeExecutionAdmission


@dataclass(frozen=True)
class AuthorizedStartPlan:
    """Write-free proof that one exact bundle may enter start persistence."""

    authorized: AuthorizedRecipe
    project_root: Path
    compiler_provenance: profile.CompilerProvenance | None
    runtime_admission: RuntimeAdmissionDecision | None
    runtime_execution: RuntimeExecutionAdmission | None = None


class _ExclusiveLock(Protocol):
    def acquire(self, blocking: bool = True) -> bool: ...

    def release(self) -> None: ...

    def __enter__(self) -> object: ...

    def __exit__(self, *args: object) -> object: ...


@dataclass(frozen=True)
class _WritableCoreActivation:
    """Serialize cold preparation, one start, then unrelated activation work."""

    lock: _ExclusiveLock
    admission_lock: _ExclusiveLock
    is_active: Callable[[], bool]
    is_closed: Callable[[], bool]
    prepare: Callable[[], None]
    finish: Callable[[], None]
    rollback: Callable[[], None]
    record_degraded: Callable[[BaseException], None]

    def _prepare_locked(self) -> bool:
        if self.is_active():
            return False
        if self.is_closed():
            raise LockstepError("command service is closed")
        try:
            self.prepare()
        except BaseException:
            self.rollback()
            raise
        return True

    def activate(self) -> None:
        """Preserve ordinary command activation without an admission guard."""

        with self.lock:
            prepared = False
            try:
                prepared = self._prepare_locked()
                if prepared:
                    self.finish()
            except BaseException:
                if prepared:
                    self.rollback()
                raise

    def admit(
        self,
        state_dir: Path,
        decision: RuntimeAdmissionDecision,
        persist: Callable[[], dict[str, Any]],
        configure: Callable[[], None] | None = None,
        complete_activation: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Linearize currentness and this park before recovery or pump startup."""

        while True:
            prepared = False
            acquired = False
            persisted = False
            try:
                self.admission_lock.acquire()
                try:
                    with decision.assert_current(state_dir):
                        acquired = self.lock.acquire(blocking=False)
                        if acquired:
                            if configure is not None:
                                configure()
                            prepared = self._prepare_locked()
                            result = persist()
                            persisted = True
                finally:
                    self.admission_lock.release()
                if not acquired:
                    with self.lock:
                        pass
                    continue
                if prepared:
                    try:
                        (complete_activation or self.finish)()
                    except BaseException as exc:
                        self.rollback()
                        self.record_degraded(exc)
                return result
            except _RuntimeAdmissionChanged as exc:
                raise LockstepError(str(exc)) from exc
            except BaseException:
                if prepared and not persisted:
                    self.rollback()
                raise
            finally:
                if acquired:
                    self.lock.release()

    def start(
        self,
        state_dir: Path,
        decision: RuntimeAdmissionDecision | None,
        persist: Callable[[], dict[str, Any]],
        configure: Callable[[], None] | None = None,
        complete_activation: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Choose ordinary activation or owner-linearized static admission."""

        if decision is None:
            self.activate()
            return persist()
        return self.admit(
            state_dir,
            decision,
            persist,
            configure,
            complete_activation,
        )


def _preflight_runtime_requirements(
    state_dir: Path,
    index: RuntimeRequirementIndex,
) -> RuntimeAdmissionDecision:
    """Open, capture, bind, and authorize one complete static inventory."""

    try:
        snapshot_digest, snapshot = open_runtime_snapshot(state_dir)
        codex_binding, pinned_binding = capture_runtime_snapshot_bindings(
            snapshot,
            project=Path(index.project_identity),
        )
        return OwnerRuntimeAuthority(
            snapshot_digest=snapshot_digest,
            snapshot=snapshot,
            codex_binding=codex_binding,
            pinned_binding=pinned_binding,
        ).preflight(index)
    except FileNotFoundError as exc:
        raise LockstepError("runtime execution policy is unavailable") from exc
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise LockstepError(str(exc)) from exc


def plan_authorized_start(
    *,
    state_dir: Path,
    authorized: AuthorizedRecipe,
    project: str,
    compiler_provenance: profile.CompilerProvenance | None,
    require_runtime_policy: Callable[
        [RuntimeRequirementIndex],
        RuntimeAdmissionDecision | RuntimeExecutionAdmission | None,
    ],
) -> AuthorizedStartPlan:
    provenance = compiler_provenance or authorized.canonical_match_proof
    project_root = Path(project).resolve()
    if state_dir == project_root or project_root in state_dir.parents:
        raise LockstepError("owner state must be outside the writable project")
    if (
        provenance is not None
        and authorized.source_bundle_sha256 != provenance.source_bundle_sha256
    ):
        raise LockstepError("compiler provenance does not bind the exact source bundle")
    with tempfile.TemporaryDirectory(prefix="lockstep-start-profile-") as raw:
        staged = Path(raw)
        for item in authorized.files:
            target = staged / item.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.bytes)
        errors, _warnings = profile.check_recipe_full(
            staged / authorized.root,
            provenance=provenance,
        )
    if errors:
        raise LockstepError("recipe failed Lockstep profile: " + "; ".join(errors))
    try:
        index = RuntimeRequirementIndex.for_authorized_closure(
            authorized,
            project_identity=str(project_root),
        )
    except ValueError as exc:
        raise LockstepError(str(exc)) from exc
    runtime_admission = None
    runtime_execution = None
    if index.requirements:
        policy = require_runtime_policy(index)
        if isinstance(policy, RuntimeExecutionAdmission):
            runtime_execution = policy
            runtime_admission = policy.decision
        else:
            runtime_admission = policy
    return AuthorizedStartPlan(
        authorized,
        project_root,
        provenance,
        runtime_admission,
        runtime_execution,
    )


class AuthorizedStartService:
    """Own profile admission, immutable binding, and atomic native start."""

    def __init__(
        self,
        *,
        blobs: object,
        bundle_store: object,
        snapshots: object,
        effects: object,
        catalog: object,
        runtime: object,
        runtime_snapshot_facts: object,
        leases: object,
        admission_lock: object,
        reserve_effect_run: Callable[[str], bool],
        release_failed_start_reservation: Callable[[str], None],
        finish_owned_binding: Callable[[str, bool], None],
        drive_engine_owned: Callable[..., object],
    ) -> None:
        self._blobs = blobs
        self._bundle_store = bundle_store
        self._snapshots = snapshots
        self._effects = effects
        self._catalog = catalog
        self._runtime = runtime
        self._runtime_snapshot_facts = runtime_snapshot_facts
        self._leases = leases
        self._admission_lock = admission_lock
        self._reserve_effect_run = reserve_effect_run
        self._release_failed_start_reservation = release_failed_start_reservation
        self._finish_owned_binding = finish_owned_binding
        self._drive_engine_owned = drive_engine_owned

    @staticmethod
    def _new_binding(
        recipe: str,
        definition_sha256: str,
        bundle_digest: str,
        project_root: Path,
    ) -> RunBinding:
        run_id = f"{recipe}-{uuid.uuid4().hex}"
        return RunBinding(
            public_run_id=run_id,
            thread_id=f"thread-{uuid.uuid4().hex}",
            recipe_digest=definition_sha256,
            recipe_snapshot_ref=bundle_digest,
            project_identity=str(project_root),
        )

    def _admit_and_drive(
        self,
        binding: RunBinding,
        input_blob: object,
        values: dict[str, Any],
        start_snapshot_ref: object,
    ) -> dict[str, Any]:
        run_id = binding.public_run_id
        owns_binding = False
        with self._admission_lock:
            try:
                binding, _admission = self._effects.admit_start(
                    self._catalog,
                    binding,
                    input_blob,
                    on_admit=lambda connection, admitted: (
                        self._runtime_snapshot_facts.bind_run_start_in_transaction(
                            connection, admitted, start_snapshot_ref
                        )
                    ),
                )
                owns_binding = self._runtime.bind(binding)
                if not self._reserve_effect_run(run_id):
                    snapshot = self._runtime.snapshot(run_id, subgraphs=True)
                    return project_status(
                        binding, snapshot, self._leases, self._effects
                    ).to_dict()
                snapshot = self._runtime.ensure_started(run_id, values)
                return self._drive_engine_owned(
                    binding.public_run_id, binding=binding, snapshot=snapshot
                ).to_dict()
            except BaseException:
                self._release_failed_start_reservation(run_id)
                raise
            finally:
                self._finish_owned_binding(run_id, owns_binding)

    def _admit_and_park(
        self,
        binding: RunBinding,
        input_blob: object,
        start_snapshot_ref: object,
    ) -> dict[str, Any]:
        """Persist admission while deliberately stopping before native start."""

        with self._admission_lock:
            binding, _admission = self._effects.admit_start(
                self._catalog,
                binding,
                input_blob,
                on_admit=lambda connection, admitted: (
                    self._runtime_snapshot_facts.bind_run_start_in_transaction(
                        connection, admitted, start_snapshot_ref
                    )
                ),
            )
        return ScenarioStatus(
            "starting",
            binding.public_run_id,
            "engine",
            "scenario_wait",
        ).to_dict()

    def start(
        self,
        recipe: str,
        plan: AuthorizedStartPlan,
        values: Mapping[str, Any],
        *,
        canonical_input: bytes,
    ) -> dict[str, Any]:
        input_blob = self._blobs.put(canonical_input)
        admitted = plan.authorized.capture(self._bundle_store)
        admitted.materialize(self._bundle_store)
        binding = self._new_binding(
            recipe,
            admitted.definition_sha256,
            admitted.bundle.digest,
            plan.project_root,
        )
        start_snapshot_ref = capture_authoritative_snapshot(
            plan.project_root,
            self._snapshots,
            self._blobs,
            binding,
            previous=None,
            purpose="run-start",
        )
        if plan.runtime_admission is not None:
            return self._admit_and_park(binding, input_blob, start_snapshot_ref)
        return self._admit_and_drive(
            binding,
            input_blob,
            dict(values),
            start_snapshot_ref,
        )
