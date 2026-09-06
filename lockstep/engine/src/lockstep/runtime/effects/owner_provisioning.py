"""Owner-selected runtime binding capture and snapshot provisioning."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from lockstep.runtime.effects.owner_policy import (
    OwnerRuntimeSnapshot,
    RuntimeProvisioningInventory,
    RuntimeRequirementIndex,
    _RuntimeBindingFacts,
)
from lockstep.runtime.effects.owner_snapshot_store import replace_runtime_snapshot
from lockstep.runtime.owner_state import ensure_owner_directory, verify_owner_directory
from lockstep.runtime.providers.codex import CodexInstallationBinding
from lockstep.runtime.providers.pinned import pinned_runner_binding_digest


@dataclass(frozen=True, slots=True)
class CapturedRuntimeBindings:
    """One validation pass over the snapshot-selected installations."""

    codex_installation: CodexInstallationBinding | None
    pinned_installation: CodexInstallationBinding | None
    codex_facts: _RuntimeBindingFacts | None
    pinned_facts: _RuntimeBindingFacts | None


def _provision_projects(
    index: RuntimeRequirementIndex | RuntimeProvisioningInventory,
    project: Path,
) -> tuple[Path, ...]:
    requested = project.resolve(strict=True)
    if isinstance(index, RuntimeRequirementIndex):
        if index.project_identity != str(requested):
            raise ValueError("runtime requirement project identity mismatch")
        return (requested,)
    projects = tuple(Path(value).resolve(strict=True) for value in index.project_identities)
    if requested not in projects:
        raise ValueError("provisioning project is absent from runtime inventory")
    return projects


def _validated_owner_state_root(
    state_dir: Path, *, projects: tuple[Path, ...]
) -> Path:
    error = "owner runtime state must be outside project"
    supplied = Path(state_dir)
    if not supplied.is_absolute():
        raise ValueError(error)
    lexical = Path(os.path.abspath(supplied))
    resolved = supplied.resolve(strict=False)
    for project in projects:
        if (
            lexical == project
            or project in lexical.parents
            or resolved == project
            or project in resolved.parents
        ):
            raise ValueError(error)
    return supplied


def _capture_provision_binding(member: dict[str, object]):
    try:
        return CodexInstallationBinding.capture(
            executable=member["executable"],
            model=member["model"],
            cli_version=member["cli_version"],
            permission_profile=member["permission_profile"],
            codex_home=member["codex_home"],
            environment=member["environment"],
        )
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise ValueError(str(exc)) from exc


def _validate_provision_tmpdir(binding: object, *, projects: tuple[Path, ...]) -> None:
    error = (
        "TMPDIR must be an absolute non-symlink owner-only directory outside project"
    )
    environment = dict(binding.environment)  # type: ignore[attr-defined]
    supplied = Path(environment["TMPDIR"])
    if not supplied.is_absolute() or supplied.is_symlink():
        raise ValueError(error)
    try:
        resolved = supplied.resolve(strict=True)
        verify_owner_directory(resolved)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(error) from exc
    if supplied != resolved or any(
        resolved == project or project in resolved.parents for project in projects
    ):
        raise ValueError(error)


def _runtime_binding_facts(
    binding: object,
    *,
    pinned_permission_profile: object,
    binding_digest: str | None = None,
) -> _RuntimeBindingFacts:
    return _RuntimeBindingFacts(
        executable=str(binding.executable_path),  # type: ignore[attr-defined]
        model=binding.model,  # type: ignore[attr-defined]
        cli_version=binding.cli_version,  # type: ignore[attr-defined]
        permission_profile=binding.permission_profile,  # type: ignore[attr-defined]
        codex_home=str(binding.codex_home),  # type: ignore[attr-defined]
        environment=binding.environment,  # type: ignore[attr-defined]
        credential_identity_digest=binding.credential_identity_digest,  # type: ignore[attr-defined]
        binding_digest=(
            binding.digest  # type: ignore[attr-defined]
            if binding_digest is None
            else binding_digest
        ),
        pinned_permission_profile=pinned_permission_profile,
    )


def validate_runtime_provision_inputs(
    *,
    codex: dict[str, object] | None,
    pinned: dict[str, object] | None,
    replacement_keys: tuple[str, ...],
    index: RuntimeRequirementIndex | RuntimeProvisioningInventory,
    project: Path,
) -> tuple[_RuntimeBindingFacts | None, _RuntimeBindingFacts | None]:
    """Capture supplied bindings and require each inventory-selected runner."""

    projects = _provision_projects(index, project)
    required = {requirement.runner_selector for requirement in index.requirements}
    for selector, configured in (("codex", codex), ("pinned", pinned)):
        if selector in required and configured is None:
            raise ValueError(f"runtime inventory requires {selector} binding")
    codex_binding = _capture_provision_binding(codex) if codex is not None else None
    pinned_binding = _capture_provision_binding(pinned) if pinned is not None else None
    if (
        codex_binding is not None
        and pinned_binding is not None
        and codex_binding.codex_home == pinned_binding.codex_home
    ):
        raise ValueError("runtime provision Codex homes must differ")
    if codex_binding is not None and codex_binding.credential_identity_digest is None:
        raise ValueError("runtime codex binding requires an owner credential auth.json")
    if (
        pinned_binding is not None
        and pinned_binding.credential_identity_digest is not None
    ):
        raise ValueError("runtime pinned binding must be credential-free")
    for binding in (codex_binding, pinned_binding):
        if binding is not None:
            _validate_provision_tmpdir(binding, projects=projects)
    inventory_keys = {
        requirement.grant_selection_key for requirement in index.requirements
    }
    if any(key not in inventory_keys for key in replacement_keys):
        raise ValueError(
            "runtime replacement grant key is outside the static runtime inventory"
        )
    return (
        _runtime_binding_facts(codex_binding, pinned_permission_profile=None)
        if codex_binding is not None
        else None,
        _runtime_binding_facts(
            pinned_binding,
            pinned_permission_profile=pinned["pinned_permission_profile"],
            binding_digest=pinned_runner_binding_digest(
                pinned_binding.digest,
                pinned["pinned_permission_profile"],
            ),
        )
        if pinned_binding is not None and pinned is not None
        else None,
    )


def capture_runtime_snapshot_bindings(
    snapshot: OwnerRuntimeSnapshot,
    *,
    project: Path,
) -> tuple[_RuntimeBindingFacts | None, _RuntimeBindingFacts | None]:
    """Capture each configured installation once and reject binding drift."""

    captured = capture_runtime_execution_bindings(snapshot, project=project)
    return captured.codex_facts, captured.pinned_facts


def capture_runtime_execution_bindings(
    snapshot: OwnerRuntimeSnapshot,
    *,
    project: Path,
) -> CapturedRuntimeBindings:
    """Return the exact installations and normalized facts from one capture."""

    def member(binding: _RuntimeBindingFacts) -> dict[str, object]:
        return {
            "executable": binding.executable,
            "model": binding.model,
            "cli_version": binding.cli_version,
            "permission_profile": dict(binding.permission_profile),
            "codex_home": binding.codex_home,
            "environment": dict(binding.environment),
        }

    codex_binding = (
        _capture_provision_binding(member(snapshot.codex))
        if snapshot.codex is not None
        else None
    )
    pinned_binding = (
        _capture_provision_binding(member(snapshot.pinned))
        if snapshot.pinned is not None
        else None
    )
    projects = (project.resolve(strict=True),)
    for binding in (codex_binding, pinned_binding):
        if binding is not None:
            _validate_provision_tmpdir(binding, projects=projects)
    codex = (
        _runtime_binding_facts(codex_binding, pinned_permission_profile=None)
        if codex_binding is not None
        else None
    )
    pinned = (
        _runtime_binding_facts(
            pinned_binding,
            pinned_permission_profile=snapshot.pinned.pinned_permission_profile,
            binding_digest=pinned_runner_binding_digest(
                pinned_binding.digest,
                snapshot.pinned.pinned_permission_profile,
            ),
        )
        if pinned_binding is not None and snapshot.pinned is not None
        else None
    )
    if codex != snapshot.codex or pinned != snapshot.pinned:
        raise ValueError("owner runtime binding changed after provisioning")
    return CapturedRuntimeBindings(
        codex_installation=codex_binding,
        pinned_installation=pinned_binding,
        codex_facts=codex,
        pinned_facts=pinned,
    )


def provision_runtime_snapshot(
    *,
    state_dir: Path,
    codex: dict[str, object] | None,
    pinned: dict[str, object] | None,
    replacement_keys: tuple[str, ...],
    index: RuntimeRequirementIndex | RuntimeProvisioningInventory,
    project: Path,
) -> OwnerRuntimeSnapshot:
    """Atomically replace the complete owner runtime configuration and grants."""

    projects = _provision_projects(index, project)
    state_root = _validated_owner_state_root(Path(state_dir), projects=projects)
    codex_facts, pinned_facts = validate_runtime_provision_inputs(
        codex=codex,
        pinned=pinned,
        replacement_keys=replacement_keys,
        index=index,
        project=project,
    )
    directory = ensure_owner_directory(state_root, "runtime-owner")
    return replace_runtime_snapshot(
        directory=directory,
        codex=codex_facts,
        pinned=pinned_facts,
        replacement_keys=replacement_keys,
        index=index,
    )
