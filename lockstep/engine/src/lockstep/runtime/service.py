"""Public scenario application service over native checkpoints."""

from __future__ import annotations

import re
import tempfile
import uuid
from collections.abc import Mapping
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
from lockstep.runtime import sessions
from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.graph_runtime import (
    GraphRuntime,
    NativeCoordinateRejected,
    NativeHistoryLimitExceeded,
)
from lockstep.runtime.invocation_lock import InvocationLockStore
from lockstep.runtime.leases import LeaseStore
from lockstep.runtime.owner_state import ensure_owner_directory, initialize_owner_state
from lockstep.runtime.payload_limits import PayloadLimitExceeded, bounded_json
from lockstep.runtime.recipe_bundles import RecipeBundleStore
from lockstep.runtime.status import ScenarioStatus, project_status
from lockstep.runtime.storage import SQLiteStore

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RESERVED_START_KEYS = frozenset({"namespace"})


class LockstepError(RuntimeError):
    pass


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
    try:
        value = bounded_json(evidence, label="scenario evidence")
    except PayloadLimitExceeded as exc:
        raise LockstepError(str(exc)) from exc
    if not isinstance(value, dict):
        raise LockstepError("scenario evidence must be a JSON object")
    if any(key.startswith("_") for key in value):
        raise LockstepError("reserved evidence keys are forbidden")
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
) -> AuthorizedRecipe:
    """Pure admission/profile boundary: no persistent Lockstep state exists yet."""
    if not _NAME_RE.fullmatch(name or ""):
        raise LockstepError(f"invalid recipe name {name!r}")
    try:
        source = RecipeLoader(Path(recipes_dir).resolve()).resolve(name).path
        authorized = (
            StrictRecipeIngress(source.parent)
            .inspect(source.name)
            .authorize(authority_policy or RecipeAuthorityPolicy())
        )
        with tempfile.TemporaryDirectory(prefix="lockstep-preflight-") as raw:
            store = RecipeBundleStore(Path(raw) / "owner-state")
            materialized = authorized.capture(store).materialize(store)
            errors, _warnings = profile.check_recipe_full(materialized.source_path)
    except (OSError, ValueError, RecipeError, RecipeAuthorityError) as exc:
        raise LockstepError(str(exc)) from exc
    if errors:
        raise LockstepError("recipe failed Lockstep profile: " + "; ".join(errors))
    return authorized


class LockstepService:
    def __init__(
        self,
        state_dir: Path,
        recipes_dir: Path,
        *,
        authority_policy: RecipeAuthorityPolicy | None = None,
    ) -> None:
        self.state_dir = initialize_owner_state(Path(state_dir).resolve())
        self.recipes_dir = Path(recipes_dir).resolve()
        self.authority_policy = authority_policy or RecipeAuthorityPolicy()
        self.store = SQLiteStore(self.state_dir / "runtime.sqlite")
        self.catalog = RunCatalog(self.store)
        self.bundle_store = RecipeBundleStore(self.state_dir)
        self.leases = LeaseStore(self.store)
        checkpoints = ensure_owner_directory(self.state_dir, "checkpoints")
        self.checkpoint_path = checkpoints / "native.sqlite"
        self.runtime = GraphRuntime(
            bundle_store=self.bundle_store,
            leases=self.leases,
            invocations=InvocationLockStore(self.state_dir, timeout=60.0),
            checkpoint_path=self.checkpoint_path,
            app_factory=open_native_app,
        )
        self._closed = False

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
            raise LockstepError(f"run {run_id}: native binding integrity failure") from exc
        return binding

    def start(self, recipe: str, input: dict | None, project: str) -> dict[str, Any]:
        values = validate_start_input(input)
        authorized = preflight_recipe(
            self.recipes_dir, recipe, authority_policy=self.authority_policy
        )
        return self.start_authorized(recipe, authorized, values, project)

    def start_authorized(
        self,
        recipe: str,
        authorized: AuthorizedRecipe,
        input: Mapping[str, Any],
        project: str,
    ) -> dict[str, Any]:
        values = validate_start_input(input)
        project_root = Path(project).resolve()
        if self.state_dir == project_root or project_root in self.state_dir.parents:
            raise LockstepError("owner state must be outside the writable project")
        admitted = authorized.capture(self.bundle_store)
        materialized = admitted.materialize(self.bundle_store)
        errors, _warnings = profile.check_recipe_full(materialized.source_path)
        if errors:
            raise LockstepError("recipe failed Lockstep profile: " + "; ".join(errors))

        run_id = f"{recipe}-{uuid.uuid4().hex}"
        binding = RunBinding(
            public_run_id=run_id,
            thread_id=f"thread-{uuid.uuid4().hex}",
            recipe_digest=admitted.definition_sha256,
            recipe_snapshot_ref=admitted.bundle.digest,
            project_identity=str(project_root),
        )
        try:
            self.runtime.bind(binding)
            binding = self.catalog.create(binding)
            snapshot = self.runtime.start(run_id, values)
        except BaseException:
            self.runtime.unbind(run_id)
            raise
        return project_status(binding, snapshot, (), ()).to_dict()

    def _snapshot_status(
        self, run_id: str, project: str
    ) -> tuple[RunBinding, ScenarioStatus]:
        binding = self._bind_existing(run_id, project)
        snapshot = self.runtime.snapshot(run_id, subgraphs=True)
        return binding, project_status(binding, snapshot, (), ())

    def status(self, run_id: str, project: str) -> dict[str, Any]:
        _binding, status = self._snapshot_status(run_id, project)
        return status.to_dict()

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
            if step is None or observed_step is None or observed_step == step:
                matches.append(interrupt)
        if len(matches) != 1:
            raise LockstepError("worker step does not identify exactly one pending interrupt")
        if (
            step is not None
            and isinstance(matches[0].value, dict)
            and matches[0].value.get("step") != step
        ):
            raise LockstepError(f"run {run_id} is parked on another step")
        return binding, matches[0]

    def _require_session(self, run_id: str, session_id: str | None) -> None:
        binding = sessions.read_binding(self.state_dir, run_id)
        if (
            binding is None
            or not isinstance(session_id, str)
            or not session_id
            or binding["session_id"] != session_id
        ):
            raise LockstepError("worker session binding mismatch")

    def require_session(
        self, run_id: str, session_id: str | None, project: str
    ) -> None:
        """Fail closed at an external mutation edge; resume rechecks it too."""
        self._bind_existing(run_id, project)
        self._require_session(run_id, session_id)

    def _resume_worker(
        self,
        run_id: str,
        step: str | None,
        result: Mapping[str, Any],
        *,
        session_id: str | None,
        project: str,
    ) -> dict[str, Any]:
        binding, interrupt = self._worker_interrupt(run_id, step, project)
        try:
            with sessions.locked_owner(self.state_dir, run_id, session_id):
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

    def abort(
        self, run_id: str, *, session_id: str | None = None, project: str
    ):
        return self.scenario_abort(run_id, session_id=session_id, project=project)

    def list_runs(self, project: str) -> list[dict[str, Any]]:
        bindings = self.catalog.list(str(Path(project).resolve()))
        return [self.status(item.public_run_id, project) for item in bindings]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.runtime.close()
        finally:
            self.store.close()
