"""Closed owner-bound runtime composition for protected public commands."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.authority import EffectGrant
from lockstep.runtime.effects.owner_policy import (
    OwnerRuntimeAuthority,
    OwnerRuntimeGrant,
    OwnerRuntimeSnapshot,
    RuntimeAdmissionDecision,
    RuntimeRequirement,
    RuntimeRequirementIndex,
)
from lockstep.runtime.effects.owner_provisioning import (
    CapturedRuntimeBindings,
    capture_runtime_execution_bindings,
)
from lockstep.runtime.effects.owner_snapshot_store import (
    hold_runtime_snapshot_current,
    open_runtime_snapshot,
)
from lockstep.runtime.providers.base import EffectRequest, PreparedLaunch
from lockstep.runtime.providers.codex import (
    CodexLaunchDecisionGate,
    CodexRunnerAdapter,
    CodexSandboxAttestor,
)
from lockstep.runtime.providers.composition import ReleasedRunnerComposition
from lockstep.runtime.providers.pinned import (
    PinnedRunnerAdapter,
    pinned_runner_binding_digest,
)
from lockstep.runtime.providers.workspaces import LocalGitWorkspaceProvider
from lockstep.runtime.recipe_bundles import RecipeBundleRef, RecipeBundleStore
from lockstep.recipe.authority import recipe_definition_sha256


@dataclass(frozen=True, slots=True)
class RuntimeExecutionContext:
    """Reusable owner snapshot and installations captured in one pass."""

    snapshot_digest: str
    snapshot: OwnerRuntimeSnapshot
    bindings: CapturedRuntimeBindings


@dataclass(frozen=True, slots=True)
class RuntimeExecutionAdmission:
    """One recipe decision bound to a reusable owner runtime context."""

    decision: RuntimeAdmissionDecision
    context: RuntimeExecutionContext


class RuntimeBundleRequirementResolver:
    """Reconstruct definition authority only from an admitted immutable bundle."""

    def __init__(self, bundles: RecipeBundleStore) -> None:
        self._bundles = bundles

    def index(self, binding: RunBinding) -> RuntimeRequirementIndex:
        ref = RecipeBundleRef(binding.recipe_snapshot_ref)
        manifest = self._bundles.read_manifest(ref)
        materialized = self._bundles.read_materialization(ref)
        definition = recipe_definition_sha256(
            manifest.root,
            ((item.path, item.sha256, item.size) for item in manifest.files),
        )
        if definition != binding.recipe_digest:
            raise ValueError("catalog definition differs from immutable recipe bundle")
        documents = tuple(
            (item.path, (materialized.directory / item.path).read_bytes())
            for item in manifest.files
        )
        return RuntimeRequirementIndex._for_recipe_documents(
            documents,
            definition_digest=binding.recipe_digest,
            project_identity=binding.project_identity,
        )

    def resolve(self, binding: RunBinding, intent: EffectRequest) -> RuntimeRequirement:
        if (
            intent.public_run_id != binding.public_run_id
            or intent.project_identity != binding.project_identity
            or intent.definition_digest != binding.recipe_digest
        ):
            raise ValueError("effect intent differs from immutable run binding")
        matches = tuple(
            item
            for item in self.index(binding).requirements
            if item.protected_descriptor_digest == intent.descriptor_digest
            and item.runner_selector == intent.runner_selector
            and item.required_capabilities
            == tuple(sorted(intent.required_capabilities))
        )
        if len(matches) != 1:
            raise ValueError("effect intent has no exact immutable runtime requirement")
        return matches[0]


class OwnerRuntimeEffectAuthority:
    """Resolve and recommit one exact dynamic effect from immutable owner facts."""

    def __init__(
        self,
        *,
        state_dir: Path,
        context: RuntimeExecutionContext,
        catalog: object,
        resolver: RuntimeBundleRequirementResolver,
        workspaces: LocalGitWorkspaceProvider,
    ) -> None:
        self._state_dir = state_dir
        self._context = context
        self._catalog = catalog
        self._resolver = resolver
        self._workspaces = workspaces

    def _owner_facts(
        self, intent: EffectRequest
    ) -> tuple[RuntimeRequirement, str, OwnerRuntimeGrant]:
        binding = self._catalog.get(intent.public_run_id)
        requirement = self._resolver.resolve(binding, intent)
        snapshot = self._context.snapshot
        bound = dict(
            (item.grant_selection_key, digest)
            for item, digest in RuntimeRequirementIndex(
                binding.project_identity, (requirement,)
            )
            .bind(snapshot)
            .entries
        )
        digest = bound[requirement.grant_selection_key]
        grants = {item.grant_selection_key: item for item in snapshot.grants}
        grant = grants.get(requirement.grant_selection_key)
        if grant is None or grant.requirement_digest != digest:
            raise ValueError("current owner runtime grant is unavailable")
        expected_binding = (
            snapshot.codex
            if requirement.runner_selector == "codex"
            else snapshot.pinned
        )
        if (
            expected_binding is None
            or intent.runner_binding_digest != expected_binding.binding_digest
        ):
            raise ValueError("effect intent uses a different owner runner binding")
        return requirement, digest, grant

    def resolve(self, intent: EffectRequest) -> EffectGrant:
        with hold_runtime_snapshot_current(
            self._state_dir,
            expected_digest=self._context.snapshot_digest,
            expected_snapshot=self._context.snapshot,
        ):
            return self._resolve_current(intent)

    def _resolve_current(self, intent: EffectRequest) -> EffectGrant:
        requirement, digest, owner_grant = self._owner_facts(intent)
        if intent.deadline_at is None:
            raise ValueError("owner-authorized process effect requires a deadline")
        return EffectGrant.build(
            intent,
            actor_binding_digest=digest,
            required_authorities=requirement.required_authorities,
            workspace_ref=self._workspaces.workspace_ref_for(
                intent.effect_id, intent.intent_digest
            ),
            parent_capability_generation=owner_grant.grant_generation,
            grant_generation=owner_grant.grant_generation,
            policy_epoch=owner_grant.policy_generation,
            config_epoch=owner_grant.config_generation,
            approval_generation=None,
            expires_at=intent.deadline_at,
        )

    @staticmethod
    def _intent(request: EffectRequest) -> EffectRequest:
        return replace(
            request,
            grant_digest=None,
            workspace_ref=None,
            request_digest=request.intent_digest,
        )

    @contextmanager
    def commitment(
        self,
        grant: EffectGrant,
        request: EffectRequest,
        _launch: PreparedLaunch,
    ):
        with hold_runtime_snapshot_current(
            self._state_dir,
            expected_digest=self._context.snapshot_digest,
            expected_snapshot=self._context.snapshot,
        ):
            intent = self._intent(request)
            expected_grant = self._resolve_current(intent)
            if expected_grant != grant or intent.bind_grant(grant) != request:
                raise ValueError(
                    "effect commitment differs from current owner authority"
                )
            yield


@dataclass(frozen=True, slots=True)
class RuntimeExecutionComposition:
    runners: ReleasedRunnerComposition
    authority: OwnerRuntimeEffectAuthority


def capture_runtime_execution_admission(
    state_dir: Path,
    index: RuntimeRequirementIndex,
) -> RuntimeExecutionAdmission:
    digest, snapshot = open_runtime_snapshot(state_dir)
    bindings = capture_runtime_execution_bindings(
        snapshot, project=Path(index.project_identity)
    )
    decision = OwnerRuntimeAuthority(
        snapshot_digest=digest,
        snapshot=snapshot,
        codex_binding=bindings.codex_facts,
        pinned_binding=bindings.pinned_facts,
    ).preflight(index)
    return RuntimeExecutionAdmission(
        decision,
        RuntimeExecutionContext(digest, snapshot, bindings),
    )


def build_runtime_execution_composition(
    *,
    state_dir: Path,
    context: RuntimeExecutionContext,
    catalog: object,
    bundles: RecipeBundleStore,
    blobs: object,
    snapshots: object,
) -> RuntimeExecutionComposition:
    captured = context.bindings
    snapshot = context.snapshot
    workspaces = LocalGitWorkspaceProvider(state_dir, snapshots, blobs)
    codex = (
        CodexRunnerAdapter(
            owner_state_dir=state_dir,
            installation=lambda: captured.codex_installation,
            decision_gate=CodexLaunchDecisionGate(
                snapshot.codex.binding_digest, generation=snapshot.config_generation
            ),
            workspaces=workspaces,
            blobs=blobs,
            sandbox=CodexSandboxAttestor(
                cli_version=captured.codex_installation.cli_version
            ),
        )
        if captured.codex_installation is not None and snapshot.codex is not None
        else None
    )
    pinned = (
        PinnedRunnerAdapter(
            owner_state_dir=state_dir,
            installation=lambda: captured.pinned_installation,
            decision_gate=CodexLaunchDecisionGate(
                pinned_runner_binding_digest(
                    captured.pinned_installation.digest,
                    snapshot.pinned.pinned_permission_profile,
                ),
                generation=snapshot.config_generation,
            ),
            workspaces=workspaces,
            blobs=blobs,
            sandbox=CodexSandboxAttestor(
                cli_version=captured.pinned_installation.cli_version
            ),
            permission_profile=snapshot.pinned.pinned_permission_profile,
        )
        if captured.pinned_installation is not None and snapshot.pinned is not None
        else None
    )
    authority = OwnerRuntimeEffectAuthority(
        state_dir=state_dir,
        context=context,
        catalog=catalog,
        resolver=RuntimeBundleRequirementResolver(bundles),
        workspaces=workspaces,
    )
    return RuntimeExecutionComposition(
        ReleasedRunnerComposition(codex=codex, pinned=pinned), authority
    )
