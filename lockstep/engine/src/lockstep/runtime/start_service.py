"""Authorized run-start use case over explicit command-side dependencies."""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lockstep.recipe import profile
from lockstep.recipe.authority import AuthorizedRecipe
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.snapshot_resolver import capture_authoritative_snapshot
from lockstep.runtime.status import project_status


@dataclass(frozen=True)
class AuthorizedStartPlan:
    """Write-free proof that one exact bundle may enter start persistence."""

    authorized: AuthorizedRecipe
    project_root: Path
    compiler_provenance: profile.CompilerProvenance | None


def plan_authorized_start(
    *,
    state_dir: Path,
    authorized: AuthorizedRecipe,
    project: str,
    compiler_provenance: profile.CompilerProvenance | None,
    require_runtime_policy: Callable[[tuple[object, ...]], None],
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
        requirements = RuntimeRequirementIndex.for_authorized_closure(
            authorized,
            project_identity=str(project_root),
        ).requirements
    except ValueError as exc:
        raise LockstepError(str(exc)) from exc
    require_runtime_policy(requirements)
    return AuthorizedStartPlan(authorized, project_root, provenance)


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
        deactivate_effect_run: Callable[[str], None],
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
        self._deactivate_effect_run = deactivate_effect_run
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
                self._runtime.bind(binding)
                if not self._reserve_effect_run(run_id):
                    snapshot = self._runtime.snapshot(run_id, subgraphs=True)
                    self._runtime.unbind(run_id)
                    return project_status(
                        binding, snapshot, self._leases, self._effects
                    ).to_dict()
                snapshot = self._runtime.ensure_started(run_id, values)
            except BaseException:
                self._deactivate_effect_run(run_id)
                self._runtime.unbind(run_id)
                raise
            return self._drive_engine_owned(
                binding.public_run_id, binding=binding, snapshot=snapshot
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
        return self._admit_and_drive(
            binding, input_blob, dict(values), start_snapshot_ref
        )
