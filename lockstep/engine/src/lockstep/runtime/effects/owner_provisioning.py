"""Owner-selected runtime binding capture and snapshot provisioning."""

from __future__ import annotations

import os
from pathlib import Path

from lockstep.runtime.effects.owner_policy import (
    OwnerRuntimeSnapshot,
    RuntimeRequirementIndex,
    _RuntimeBindingFacts,
)
from lockstep.runtime.effects.owner_snapshot_store import replace_runtime_snapshot
from lockstep.runtime.owner_state import ensure_owner_directory, verify_owner_directory
from lockstep.runtime.providers.codex import CodexInstallationBinding
from lockstep.runtime.providers.pinned import pinned_runner_binding_digest


def _validated_owner_state_root(state_dir: Path, *, project: Path) -> Path:
    error = "owner runtime state must be outside project"
    supplied = Path(state_dir)
    if not supplied.is_absolute():
        raise ValueError(error)
    project = project.resolve(strict=True)
    lexical = Path(os.path.abspath(supplied))
    resolved = supplied.resolve(strict=False)
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


def _validate_provision_tmpdir(binding: object, *, project: Path) -> None:
    error = "TMPDIR must be an absolute non-symlink owner-only directory outside project"
    environment = dict(binding.environment)  # type: ignore[attr-defined]
    supplied = Path(environment["TMPDIR"])
    if not supplied.is_absolute() or supplied.is_symlink():
        raise ValueError(error)
    try:
        resolved = supplied.resolve(strict=True)
        verify_owner_directory(resolved)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(error) from exc
    if supplied != resolved or resolved == project or project in resolved.parents:
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
    codex: dict[str, object],
    pinned: dict[str, object],
    replacement_keys: tuple[str, ...],
    index: RuntimeRequirementIndex,
    project: Path,
) -> tuple[_RuntimeBindingFacts, _RuntimeBindingFacts]:
    """Capture and normalize both bindings after closed input validation."""

    codex_binding = _capture_provision_binding(codex)
    pinned_binding = _capture_provision_binding(pinned)
    if codex_binding.codex_home == pinned_binding.codex_home:
        raise ValueError("runtime provision Codex homes must differ")
    if codex_binding.credential_identity_digest is None:
        raise ValueError("runtime codex binding requires an owner credential auth.json")
    if pinned_binding.credential_identity_digest is not None:
        raise ValueError("runtime pinned binding must be credential-free")
    _validate_provision_tmpdir(codex_binding, project=project)
    _validate_provision_tmpdir(pinned_binding, project=project)
    inventory_keys = {
        requirement.grant_selection_key for requirement in index.requirements
    }
    if any(key not in inventory_keys for key in replacement_keys):
        raise ValueError(
            "runtime replacement grant key is outside the static runtime inventory"
        )
    return (
        _runtime_binding_facts(codex_binding, pinned_permission_profile=None),
        _runtime_binding_facts(
            pinned_binding,
            pinned_permission_profile=pinned["pinned_permission_profile"],
            binding_digest=pinned_runner_binding_digest(
                pinned_binding.digest,
                pinned["pinned_permission_profile"],
            ),
        ),
    )


def provision_runtime_snapshot(
    *,
    state_dir: Path,
    codex: dict[str, object],
    pinned: dict[str, object],
    replacement_keys: tuple[str, ...],
    index: RuntimeRequirementIndex,
    project: Path,
) -> OwnerRuntimeSnapshot:
    """Atomically replace the complete owner runtime configuration and grants."""

    state_root = _validated_owner_state_root(Path(state_dir), project=project)
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
