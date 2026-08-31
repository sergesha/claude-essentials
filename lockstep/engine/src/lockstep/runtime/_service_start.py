"""Capability owner extracted from the command-service facade."""

from __future__ import annotations

from lockstep.authoring_publisher import observe_authoring_project
from lockstep.runtime._service_preflight import AuthoringError
from lockstep.runtime._service_values import (
    Any,
    AuthorizedRecipe,
    AuthorizedStartPlan,
    LockstepError,
    Mapping,
    Path,
    profile,
)
from lockstep.runtime.start_input import canonical_start_input, validate_start_input
from lockstep.runtime.start_service import AuthorizedStartService

_SERVICE_FACADE = None

class _ServiceStart:
    def start(
        self,
        recipe: str,
        input: dict | None,
        project: str,
        *,
        compiler_provenance: profile.CompilerProvenance | None = None,
    ) -> dict[str, Any]:
        values = validate_start_input(input)
        plan = self._canonical_start_plan(recipe, project, compiler_provenance)
        return self._start_planned(recipe, plan, values)

    def _plan_start(
        self,
        recipe: str,
        project: str,
        compiler_provenance: profile.CompilerProvenance | None,
    ) -> AuthorizedStartPlan:
        authorized = _SERVICE_FACADE.preflight_recipe(
            self.recipes_dir,
            recipe,
            authority_policy=self.authority_policy,
            compiler_provenance=compiler_provenance,
        )
        return _SERVICE_FACADE.plan_authorized_start(
            state_dir=self.state_dir,
            authorized=authorized,
            project=project,
            compiler_provenance=compiler_provenance,
            require_runtime_policy=self._require_owner_runtime_policy,
        )

    def _canonical_start_plan(
        self,
        recipe: str,
        project: str,
        compiler_provenance: profile.CompilerProvenance | None,
    ) -> AuthorizedStartPlan:
        try:
            return observe_authoring_project(
                self.state_dir,
                Path(project),
                lambda: self._plan_start(recipe, project, compiler_provenance),
            )
        except (AuthoringError, OSError, ValueError) as exc:
            raise LockstepError(str(exc)) from exc

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
        plan = _SERVICE_FACADE.plan_authorized_start(
            state_dir=self.state_dir,
            authorized=authorized,
            project=project,
            compiler_provenance=compiler_provenance,
            require_runtime_policy=self._require_owner_runtime_policy,
        )
        return self._start_planned(recipe, plan, values)

    def _start_planned(
        self,
        recipe: str,
        plan: AuthorizedStartPlan,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
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
            release_failed_start_reservation=self._release_failed_start_reservation,
            finish_owned_binding=self._finish_owned_effect_binding,
            drive_engine_owned=self._drive_engine_owned,
        )
