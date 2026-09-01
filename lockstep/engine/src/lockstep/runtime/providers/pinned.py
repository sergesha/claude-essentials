"""Pinned commands using the shared durable Codex local-attempt lifecycle."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Literal

from lockstep.runtime.effects.descriptors import parse_effect_result
from lockstep.runtime.effects.models import PinnedCommandSpec
from lockstep.runtime.providers.base import EffectRequest
from lockstep.runtime.providers.codex import (
    CodexInstallationBinding,
    CodexLaunchRecord,
    CodexProviderError,
    _canonical,
    _CodexAttemptDriver,
)


def validate_pinned_permission_profile(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value.encode()) > 4096
    ):
        raise CodexProviderError("pinned permission profile must be owner-selected")
    return value


def pinned_runner_binding_digest(
    installation_digest: str,
    permission_profile: str,
) -> str:
    """Bind one pinned runner to its installation and owner-selected profile."""

    if re.fullmatch(r"[0-9a-f]{64}", installation_digest) is None:
        raise CodexProviderError("pinned installation digest is invalid")
    profile = validate_pinned_permission_profile(permission_profile)
    return hashlib.sha256(
        _canonical(
            {
                "schema": "lockstep.pinned-runner-binding/v1",
                "installation_digest": installation_digest,
                "permission_profile": profile,
                "execution_authority": "os_user_execution",
                "deployment_profile": "local_unsandboxed",
            }
        )
    ).hexdigest()


class _PinnedCodexStrategy(_CodexAttemptDriver):
    """Pinned hooks for Task 6's one durable Codex attempt driver."""

    accepted_effect_kinds = frozenset({"pinned", "verify"})
    required_capabilities = frozenset({"workspace", "bounded_result", "sandbox"})
    workspace_purpose: Literal["no_publish_operation"] = "no_publish_operation"
    execution_class: Literal["pinned-command"] = "pinned-command"

    def __init__(
        self,
        *,
        permission_profile: str,
        **kwargs,
    ) -> None:
        self._pinned_permission_profile = validate_pinned_permission_profile(
            permission_profile
        )
        super().__init__(**kwargs)
        if self._binding.credential_identity_digest is not None:
            raise CodexProviderError("pinned Codex home must be credential-free")
        self.binding_digest = pinned_runner_binding_digest(
            self._binding.digest,
            permission_profile,
        )

    def _launcher_binding_digest(
        self, _binding: CodexInstallationBinding
    ) -> str:
        return self.binding_digest

    @staticmethod
    def _spec(request: EffectRequest) -> PinnedCommandSpec:
        values = dict(request.inputs)
        if set(values) != {"command", "snapshot"}:
            raise CodexProviderError("pinned request has unknown or missing inputs")
        try:
            return PinnedCommandSpec.parse(values["command"])
        except (TypeError, ValueError) as exc:
            raise CodexProviderError("invalid pinned command contract") from exc

    def _request_payload(self, request: EffectRequest) -> tuple[bytes, str]:
        spec = self._spec(request)
        if spec.result_source != "exit":
            raise CodexProviderError(
                "local pinned provider supports exit-only results; result stability is unavailable"
            )
        snapshot_ref = dict(request.inputs)["snapshot"]
        if not isinstance(snapshot_ref, str):
            raise CodexProviderError("pinned snapshot input must be a string")
        return b"", snapshot_ref

    def _inner_argv(
        self,
        binding: CodexInstallationBinding,
        workspace: Path,
        request: EffectRequest,
    ) -> tuple[str, ...]:
        spec = self._spec(request)
        cwd = (workspace / spec.logical_cwd).resolve(strict=False)
        if cwd != workspace and workspace not in cwd.parents:
            raise CodexProviderError("pinned cwd escaped its workspace")
        return (
            str(binding.executable_path),
            "sandbox",
            "--permission-profile",
            self._pinned_permission_profile,
            "--cd",
            str(cwd),
            "--include-managed-config",
            "--",
            *spec.logical_argv,
        )

    def _execution_cwd(self, workspace: Path, request: EffectRequest) -> Path:
        return (workspace / self._spec(request).logical_cwd).resolve(strict=False)

    def _parse_result(
        self,
        record: CodexLaunchRecord,
        receipt: dict[str, object],
        snapshot_ref: str | None,
    ):
        if snapshot_ref is not None:
            raise CodexProviderError("pinned result may not publish a snapshot")
        if receipt.get("termination_reason") in {"spawn_failed", "stdin_failed"}:
            outcome, error = "ERROR", "runner_failed"
        elif receipt["overflow"]:
            outcome, error = "ERROR", "result_invalid"
        elif receipt["timed_out"]:
            outcome, error = "ERROR", "deadline_timeout"
        else:
            outcome = "PASS" if receipt["returncode"] == 0 else "FAIL"
            error = None
        return parse_effect_result(
            {
                "schema": "lockstep.effect-result/v1",
                "effect_id": record.effect_id,
                "outcome": outcome,
                "result_ref": None,
                "artifact_refs": [],
                "snapshot_ref": None,
                "diff_ref": None,
                "fixed_error_code": error,
                "evidence_refs": [],
            }
        )


class PinnedRunnerAdapter:
    """Exit-only pinned adapter delegating the complete local-attempt lifecycle."""

    required_authorities = _CodexAttemptDriver.required_authorities
    reconciliation_boundary = _CodexAttemptDriver.reconciliation_boundary
    accepted_effect_kinds = _PinnedCodexStrategy.accepted_effect_kinds

    def __init__(self, **kwargs) -> None:
        self._driver = _PinnedCodexStrategy(**kwargs)

    @property
    def binding_digest(self) -> str:
        return self._driver.binding_digest

    @property
    def spawn_count(self) -> int:
        return self._driver.spawn_count

    def prepare(self, request: EffectRequest):
        return self._driver.prepare(request)

    def ensure_started(self, launch):
        return self._driver.ensure_started(launch)

    def inspect(self, effect_id: str):
        return self._driver.inspect(effect_id)

    lookup = inspect

    def cancel(self, effect_id: str):
        return self._driver.cancel(effect_id)

    def quiesce(self, effect_id: str):
        return self._driver.quiesce(effect_id)

    def wait_terminal(self, effect_id: str, *, timeout: float):
        return self._driver.wait_terminal(effect_id, timeout=timeout)

    def __getattr__(self, name: str):
        return getattr(self._driver, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_driver" or "_driver" not in self.__dict__:
            object.__setattr__(self, name, value)
        elif hasattr(self._driver, name):
            setattr(self._driver, name, value)
        else:
            object.__setattr__(self, name, value)
