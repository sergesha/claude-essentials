"""Owner-selected runtime binding capture and snapshot provisioning."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from collections.abc import Mapping

from lockstep.runtime.effects.owner_policy import (
    OwnerRuntimeSnapshot,
    RuntimeProvisioningInventory,
    RuntimeRequirementIndex,
    _RuntimeBindingFacts,
)
from lockstep.runtime.effects.owner_snapshot_store import replace_runtime_snapshot
from lockstep.runtime.owner_state import ensure_owner_directory, verify_owner_directory
from lockstep.runtime.providers.codex import CodexInstallationBinding
from lockstep.runtime.providers.pinned import pinned_runner_binding_digest, validate_pinned_permission_profile
from lockstep.runtime.providers.local import ClaudeInstallationBinding, DirectInstallationBinding, PinnedBackend
from lockstep.runtime.effects._owner_policy_values import (
    _ClaudeBindingFacts, _DirectBindingFacts, PinnedBindingFacts,
)


@dataclass(frozen=True, slots=True)
class CapturedRuntimeBindings:
    """One validation pass over the snapshot-selected installations."""

    codex_installation: CodexInstallationBinding | None
    pinned_installation: CodexInstallationBinding | DirectInstallationBinding | None
    codex_facts: _RuntimeBindingFacts | None
    pinned_facts: PinnedBindingFacts | None
    claude_installation: ClaudeInstallationBinding | None = None
    claude_facts: _ClaudeBindingFacts | None = None


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


def _capture_provision_binding(member: dict[str, object]) -> CodexInstallationBinding:
    try:
        return CodexInstallationBinding.capture(
            executable=cast(str, member["executable"]),
            model=cast(str, member["model"]),
            cli_version=cast(str, member["cli_version"]),
            permission_profile=cast(Mapping[str, object], member["permission_profile"]),
            codex_home=cast(str, member["codex_home"]),
            environment=cast(Mapping[str, str], member["environment"]),
        )
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise ValueError(str(exc)) from exc


def _capture_pinned(member: dict[str, object]) -> CodexInstallationBinding | DirectInstallationBinding:
    if member.get("backend") == PinnedBackend.DIRECT_LOCAL:
        if set(member) != {"backend", "environment"}:
            raise ValueError("direct-local binding schema is invalid")
        return DirectInstallationBinding.capture(environment=member["environment"])
    return _capture_provision_binding(member)


def _capture_claude(member: dict[str, object]) -> ClaudeInstallationBinding:
    return ClaudeInstallationBinding.capture(executable=cast(str, member["executable"]),
                                             model=cast(str, member["model"]), home=cast(str, member["home"]),
                                             environment=member["environment"])


def _claude_facts(binding: ClaudeInstallationBinding | None) -> _ClaudeBindingFacts | None:
    if binding is None:
        return None
    return _ClaudeBindingFacts(str(binding.executable_path), binding.model, str(binding.home), binding.environment, binding.digest)


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
    binding: CodexInstallationBinding,
    *,
    pinned_permission_profile: str | None,
    binding_digest: str | None = None,
) -> _RuntimeBindingFacts:
    return _RuntimeBindingFacts(
        executable=str(binding.executable_path),
        model=binding.model,
        cli_version=binding.cli_version,
        permission_profile=binding.permission_profile,
        codex_home=str(binding.codex_home),
        environment=binding.environment,
        credential_identity_digest=binding.credential_identity_digest,
        binding_digest=(
            binding.digest
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
    claude: dict[str, object] | None = None,
) -> tuple[_RuntimeBindingFacts | None, PinnedBindingFacts | None, _ClaudeBindingFacts | None]:
    """Capture supplied bindings and require each inventory-selected runner."""

    projects = _provision_projects(index, project)
    required = {requirement.runner_selector for requirement in index.requirements}
    for selector, configured in (("codex", codex), ("pinned", pinned), ("claude", claude)):
        if selector in required and configured is None:
            raise ValueError(f"runtime inventory requires {selector} binding")
    codex_binding = _capture_provision_binding(codex) if codex is not None else None
    pinned_binding = _capture_pinned(pinned) if pinned is not None else None
    claude_binding = _capture_claude(claude) if claude is not None else None
    if (
        codex_binding is not None
        and isinstance(pinned_binding, CodexInstallationBinding)
        and codex_binding.codex_home == pinned_binding.codex_home
    ):
        raise ValueError("runtime provision Codex homes must differ")
    if codex_binding is not None and codex_binding.credential_identity_digest is None:
        raise ValueError("runtime codex binding requires an owner credential auth.json")
    if (
        isinstance(pinned_binding, CodexInstallationBinding)
        and pinned_binding.credential_identity_digest is not None
    ):
        raise ValueError("runtime pinned binding must be credential-free")
    for binding in (codex_binding, pinned_binding, claude_binding):
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
        _DirectBindingFacts(pinned_binding.environment, pinned_binding.digest)
        if isinstance(pinned_binding, DirectInstallationBinding)
        else _runtime_binding_facts(
            pinned_binding,
            pinned_permission_profile=validate_pinned_permission_profile(pinned["pinned_permission_profile"]),
            binding_digest=pinned_runner_binding_digest(
                pinned_binding.digest,
                validate_pinned_permission_profile(pinned["pinned_permission_profile"]),
            ),
        )
        if pinned_binding is not None and pinned is not None
        else None,
        _claude_facts(claude_binding),
    )


def capture_runtime_snapshot_bindings(
    snapshot: OwnerRuntimeSnapshot,
    *,
    project: Path,
) -> tuple[_RuntimeBindingFacts | None, PinnedBindingFacts | None, _ClaudeBindingFacts | None]:
    """Capture each configured installation once and reject binding drift."""

    captured = capture_runtime_execution_bindings(snapshot, project=project)
    return captured.codex_facts, captured.pinned_facts, captured.claude_facts


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
        DirectInstallationBinding.capture(environment=dict(snapshot.pinned.environment))
        if isinstance(snapshot.pinned, _DirectBindingFacts)
        else _capture_provision_binding(member(snapshot.pinned))
        if snapshot.pinned is not None
        else None
    )
    claude_binding = (
        ClaudeInstallationBinding.capture(executable=snapshot.claude.executable,
                                         model=snapshot.claude.model, home=snapshot.claude.home,
                                         environment=dict(snapshot.claude.environment))
        if snapshot.claude is not None else None
    )
    projects = (project.resolve(strict=True),)
    for binding in (codex_binding, pinned_binding, claude_binding):
        if binding is not None:
            _validate_provision_tmpdir(binding, projects=projects)
    codex = (
        _runtime_binding_facts(codex_binding, pinned_permission_profile=None)
        if codex_binding is not None
        else None
    )
    pinned = (
        _DirectBindingFacts(pinned_binding.environment, pinned_binding.digest)
        if isinstance(pinned_binding, DirectInstallationBinding)
        else _runtime_binding_facts(
            pinned_binding,
            pinned_permission_profile=snapshot.pinned.pinned_permission_profile,
            binding_digest=pinned_runner_binding_digest(
                pinned_binding.digest,
                validate_pinned_permission_profile(snapshot.pinned.pinned_permission_profile),
            ),
        )
        if pinned_binding is not None and isinstance(snapshot.pinned, _RuntimeBindingFacts)
        else None
    )
    claude = _claude_facts(claude_binding)
    if codex != snapshot.codex or pinned != snapshot.pinned or claude != snapshot.claude:
        raise ValueError("owner runtime binding changed after provisioning")
    return CapturedRuntimeBindings(
        codex_installation=codex_binding,
        pinned_installation=pinned_binding,
        codex_facts=codex,
        pinned_facts=pinned,
        claude_installation=claude_binding,
        claude_facts=claude,
    )


def provision_runtime_snapshot(
    *,
    state_dir: Path,
    codex: dict[str, object] | None,
    pinned: dict[str, object] | None,
    replacement_keys: tuple[str, ...],
    index: RuntimeRequirementIndex | RuntimeProvisioningInventory,
    project: Path,
    claude: dict[str, object] | None = None,
) -> OwnerRuntimeSnapshot:
    """Atomically replace the complete owner runtime configuration and grants."""

    projects = _provision_projects(index, project)
    state_root = _validated_owner_state_root(Path(state_dir), projects=projects)
    codex_facts, pinned_facts, claude_facts = validate_runtime_provision_inputs(
        codex=codex,
        pinned=pinned,
        claude=claude,
        replacement_keys=replacement_keys,
        index=index,
        project=project,
    )
    directory = ensure_owner_directory(state_root, "runtime-owner")
    return replace_runtime_snapshot(
        directory=directory,
        codex=codex_facts,
        pinned=pinned_facts,
        claude=claude_facts,
        replacement_keys=replacement_keys,
        index=index,
    )
