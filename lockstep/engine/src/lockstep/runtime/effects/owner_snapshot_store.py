"""Durable store and transition reducer for owner runtime snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from lockstep.runtime.advisory_lock import advisory_file_lock
from lockstep.runtime.bounded_files import read_bounded_regular_file
from lockstep.runtime.effects.owner_policy import (
    OwnerRuntimeGrant,
    OwnerRuntimeSnapshot,
    RuntimeProvisioningInventory,
    RuntimeRequirementIndex,
    _RuntimeAdmissionChanged,
    _RuntimeBindingFacts,
    requirement_digest,
)
from lockstep.runtime.effects.owner_policy_ingress import _json_document
from lockstep.runtime.effects.owner_snapshot_file import (
    MAX_OWNER_RUNTIME_SNAPSHOT_BYTES,
)
from lockstep.runtime.owner_state import (
    fsync_owner_directory,
    verify_owner_directory,
)
from lockstep.runtime.effects._owner_policy_values import (
    _ClaudeBindingFacts, _DirectBindingFacts, PinnedBindingFacts, RuntimeBindingFacts,
)
from lockstep.runtime.providers.local import PinnedBackend

_SNAPSHOT_SCHEMA = "lockstep.runtime-owner/v1"


def _binding_document(binding: RuntimeBindingFacts | None) -> dict[str, object] | None:
    if binding is None:
        return None
    if isinstance(binding, _DirectBindingFacts):
        return {"backend": binding.backend, "environment": [list(item) for item in binding.environment],
                "binding_digest": binding.binding_digest}
    if isinstance(binding, _ClaudeBindingFacts):
        return {"executable": binding.executable, "model": binding.model, "home": binding.home,
                "environment": [list(item) for item in binding.environment], "binding_digest": binding.binding_digest}
    return {
        "executable": binding.executable,
        "model": binding.model,
        "cli_version": binding.cli_version,
        "permission_profile": [list(item) for item in binding.permission_profile],
        "codex_home": binding.codex_home,
        "environment": [list(item) for item in binding.environment],
        "credential_identity_digest": binding.credential_identity_digest,
        "binding_digest": binding.binding_digest,
        "pinned_permission_profile": binding.pinned_permission_profile,
    }


def _grant_document(grant: OwnerRuntimeGrant) -> dict[str, object]:
    return {
        "grant_selection_key": grant.grant_selection_key,
        "requirement_digest": grant.requirement_digest,
        "authority": grant.authority,
        "grant_generation": grant.grant_generation,
        "policy_generation": grant.policy_generation,
        "config_generation": grant.config_generation,
    }


def _snapshot_document(snapshot: OwnerRuntimeSnapshot) -> dict[str, object]:
    document = {
        "schema": snapshot.schema,
        "config_generation": snapshot.config_generation,
        "policy_generation": snapshot.policy_generation,
        "codex": _binding_document(snapshot.codex),
        "pinned": _binding_document(snapshot.pinned),
        "grants": [_grant_document(grant) for grant in snapshot.grants],
    }
    if snapshot.claude is not None:
        document["claude"] = _binding_document(snapshot.claude)
    return document


def _canonical_snapshot_bytes(snapshot: OwnerRuntimeSnapshot) -> bytes:
    encoded = json.dumps(
        _snapshot_document(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_OWNER_RUNTIME_SNAPSHOT_BYTES:
        raise ValueError("owner runtime snapshot exceeds byte limit")
    return encoded


def _pairs(value: object, *, label: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise ValueError(  # noqa: TRY004 - malformed persisted data, not API misuse
            f"owner runtime snapshot {label} must be an array"
        )
    pairs: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise ValueError(f"owner runtime snapshot {label} is invalid")
        pairs.append((item[0], item[1]))
    return tuple(pairs)


def _binding_from_document(value: object) -> PinnedBindingFacts | None:
    if value is None:
        return None
    if isinstance(value, dict) and "backend" in value:
        if set(value) != {"backend", "environment", "binding_digest"}:
            raise ValueError("owner runtime direct binding schema is invalid")
        return _DirectBindingFacts(_pairs(value["environment"], label="environment"),
                                   value["binding_digest"], PinnedBackend(value["backend"]))
    fields = {
        "executable",
        "model",
        "cli_version",
        "permission_profile",
        "codex_home",
        "environment",
        "credential_identity_digest",
        "binding_digest",
        "pinned_permission_profile",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("owner runtime snapshot binding schema is invalid")
    return _RuntimeBindingFacts(
        executable=value["executable"],
        model=value["model"],
        cli_version=value["cli_version"],
        permission_profile=_pairs(
            value["permission_profile"], label="permission profile"
        ),
        codex_home=value["codex_home"],
        environment=_pairs(value["environment"], label="environment"),
        credential_identity_digest=value["credential_identity_digest"],
        binding_digest=value["binding_digest"],
        pinned_permission_profile=value["pinned_permission_profile"],
    )


def _snapshot_from_bytes(encoded: bytes) -> OwnerRuntimeSnapshot:
    document = _json_document(encoded, label="owner snapshot")
    fields = {
        "schema",
        "config_generation",
        "policy_generation",
        "codex",
        "pinned",
        "grants",
    }
    if not isinstance(document, dict) or set(document) not in (fields, fields | {"claude"}):
        raise ValueError("owner runtime snapshot schema is invalid")
    if (
        type(document["config_generation"]) is not int
        or document["config_generation"] <= 0
        or type(document["policy_generation"]) is not int
        or document["policy_generation"] <= 0
    ):
        raise ValueError("owner runtime snapshot generations must be positive")
    grants_value = document["grants"]
    if not isinstance(grants_value, list) or len(grants_value) > 4096:
        raise ValueError("owner runtime snapshot grants are invalid")
    grants: list[OwnerRuntimeGrant] = []
    grant_fields = {
        "grant_selection_key",
        "requirement_digest",
        "authority",
        "grant_generation",
        "policy_generation",
        "config_generation",
    }
    for value in grants_value:
        if not isinstance(value, dict) or set(value) != grant_fields:
            raise ValueError("owner runtime snapshot grant schema is invalid")
        grants.append(OwnerRuntimeGrant(**value))
    codex = _binding_from_document(document["codex"])
    if codex is not None and not isinstance(codex, _RuntimeBindingFacts):
        raise ValueError("owner runtime Codex binding schema is invalid")
    snapshot = OwnerRuntimeSnapshot(
        schema=document["schema"],
        config_generation=document["config_generation"],
        policy_generation=document["policy_generation"],
        codex=codex,
        pinned=_binding_from_document(document["pinned"]),
        grants=tuple(grants),
        claude=_claude_from_document(document.get("claude")),
    )
    _assert_snapshot_grants_consistent(snapshot)
    return snapshot


def _claude_from_document(value: object) -> _ClaudeBindingFacts | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"executable", "model", "home", "environment", "binding_digest"}:
        raise ValueError("owner runtime Claude binding schema is invalid")
    return _ClaudeBindingFacts(value["executable"], value["model"], value["home"],
                               _pairs(value["environment"], label="environment"), value["binding_digest"])


def _read_snapshot(path: Path) -> tuple[bytes, OwnerRuntimeSnapshot] | None:
    encoded = read_bounded_regular_file(
        path,
        max_bytes=MAX_OWNER_RUNTIME_SNAPSHOT_BYTES,
        label="owner runtime snapshot",
        missing_ok=True,
        required_uid=os.getuid(),
        required_mode=0o600,
    )
    if encoded is None:
        return None
    return encoded, _snapshot_from_bytes(encoded)


def open_runtime_snapshot(state_dir: Path) -> tuple[str, OwnerRuntimeSnapshot]:
    """Open one existing verified snapshot without creating owner state."""

    root = Path(state_dir)
    verify_owner_directory(root)
    directory = root / "runtime-owner"
    verify_owner_directory(directory)
    opened = _read_snapshot(directory / "snapshot.json")
    if opened is None:
        raise FileNotFoundError("owner runtime snapshot is unavailable")
    encoded, snapshot = opened
    return hashlib.sha256(encoded).hexdigest(), snapshot


@contextmanager
def hold_runtime_snapshot_current(
    state_dir: Path,
    *,
    expected_digest: str,
    expected_snapshot: OwnerRuntimeSnapshot,
) -> Iterator[None]:
    """Verify one admitted snapshot and retain its provisioning lock."""

    root = Path(state_dir)
    verify_owner_directory(root)
    directory = root / "runtime-owner"
    verify_owner_directory(directory)
    with advisory_file_lock(directory / "snapshot.lock"):
        current = _read_snapshot(directory / "snapshot.json")
        if current is None:
            raise _RuntimeAdmissionChanged("owner runtime snapshot is unavailable")
        encoded, snapshot = current
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != expected_digest or snapshot != expected_snapshot:
            raise _RuntimeAdmissionChanged("owner runtime admission is no longer current")
        yield


def _assert_snapshot_grants_consistent(snapshot: OwnerRuntimeSnapshot) -> None:
    """Reject any grant not bound to a captured snapshot binding generation."""

    for grant in snapshot.grants:
        candidates = {
            requirement_digest(
                grant_selection_key=grant.grant_selection_key,
                runner_binding_digest=binding.binding_digest,
                config_generation=snapshot.config_generation,
            )
            for binding in (snapshot.codex, snapshot.pinned, snapshot.claude)
            if binding is not None
        }
        if grant.requirement_digest not in candidates:
            raise ValueError("owner runtime snapshot grant requirement digest is stale")


def _assert_predecessor_consistent(
    snapshot: OwnerRuntimeSnapshot,
    index: RuntimeRequirementIndex | RuntimeProvisioningInventory,
) -> None:
    _assert_snapshot_grants_consistent(snapshot)
    requirements = {
        requirement.grant_selection_key: requirement
        for requirement in index.requirements
    }
    for grant in snapshot.grants:
        requirement = requirements.get(grant.grant_selection_key)
        if requirement is None:
            continue
        binding = snapshot.binding_for(requirement.runner_selector)
        if binding is None:
            raise ValueError("owner runtime snapshot grant binding is missing")
        expected = requirement_digest(
            grant_selection_key=grant.grant_selection_key,
            runner_binding_digest=binding.binding_digest,
            config_generation=snapshot.config_generation,
        )
        if grant.requirement_digest != expected:
            raise ValueError("owner runtime snapshot grant requirement digest is stale")


def _next_snapshot(
    predecessor: OwnerRuntimeSnapshot | None,
    *,
    codex: _RuntimeBindingFacts | None,
    pinned: PinnedBindingFacts | None,
    claude: _ClaudeBindingFacts | None = None,
    replacement_keys: tuple[str, ...],
    index: RuntimeRequirementIndex | RuntimeProvisioningInventory,
) -> OwnerRuntimeSnapshot:
    previous_keys = (
        tuple(grant.grant_selection_key for grant in predecessor.grants)
        if predecessor is not None
        else ()
    )
    config_changed = predecessor is None or (
        predecessor.codex != codex or predecessor.pinned != pinned or predecessor.claude != claude
    )
    policy_changed = predecessor is None or previous_keys != replacement_keys
    config_generation = (
        1
        if predecessor is None
        else predecessor.config_generation + int(config_changed)
    )
    policy_generation = (
        1
        if predecessor is None
        else predecessor.policy_generation + int(policy_changed)
    )
    old_grants = (
        {grant.grant_selection_key: grant for grant in predecessor.grants}
        if predecessor is not None
        else {}
    )
    requirements = {
        requirement.grant_selection_key: requirement
        for requirement in index.requirements
    }
    grants: list[OwnerRuntimeGrant] = []
    for key in replacement_keys:
        requirement = requirements[key]
        binding = {"codex": codex, "pinned": pinned, "claude": claude}[requirement.runner_selector]
        if binding is None:
            raise ValueError("owner runtime snapshot grant binding is missing")
        digest = requirement_digest(
            grant_selection_key=key,
            runner_binding_digest=binding.binding_digest,
            config_generation=config_generation,
        )
        previous = old_grants.get(key)
        grant_generation = (
            1
            if previous is None
            else previous.grant_generation + int(previous.requirement_digest != digest)
        )
        grants.append(
            OwnerRuntimeGrant(
                grant_selection_key=key,
                requirement_digest=digest,
                authority="os_user_execution",
                grant_generation=grant_generation,
                policy_generation=policy_generation,
                config_generation=config_generation,
            )
        )
    return OwnerRuntimeSnapshot(
        schema=_SNAPSHOT_SCHEMA,
        config_generation=config_generation,
        policy_generation=policy_generation,
        codex=codex,
        pinned=pinned,
        claude=claude,
        grants=tuple(grants),
    )


def _replace_snapshot(path: Path, encoded: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".snapshot-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_owner_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def replace_runtime_snapshot(
    *,
    directory: Path,
    codex: _RuntimeBindingFacts | None,
    pinned: PinnedBindingFacts | None,
    claude: _ClaudeBindingFacts | None = None,
    replacement_keys: tuple[str, ...],
    index: RuntimeRequirementIndex | RuntimeProvisioningInventory,
) -> OwnerRuntimeSnapshot:
    """Serialize one complete snapshot transition under the owner lock."""

    snapshot_path = directory / "snapshot.json"
    with advisory_file_lock(directory / "snapshot.lock"):
        current = _read_snapshot(snapshot_path)
        predecessor = current[1] if current is not None else None
        if predecessor is not None:
            _assert_predecessor_consistent(predecessor, index)
        snapshot = _next_snapshot(
            predecessor,
            codex=codex,
            pinned=pinned,
            claude=claude,
            replacement_keys=replacement_keys,
            index=index,
        )
        encoded = _canonical_snapshot_bytes(snapshot)
        if current is not None and current[0] == encoded:
            assert predecessor is not None
            return predecessor
        _replace_snapshot(snapshot_path, encoded)
        return snapshot
