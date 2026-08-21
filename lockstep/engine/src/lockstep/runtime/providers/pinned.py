"""Pinned commands using the shared durable Codex local-attempt lifecycle."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from lockstep.runtime.effects.descriptors import parse_effect_result
from lockstep.runtime.providers.base import EffectRequest
from lockstep.runtime.providers.codex import (
    CodexInstallationBinding,
    CodexLaunchRecord,
    CodexProviderError,
    _canonical,
    _CodexAttemptDriver,
)


@dataclass(frozen=True)
class PinnedCommandSpec:
    """Closed compiler-authored command identity safe for public status."""

    logical_argv: tuple[str, ...]
    logical_cwd: str
    result_source: Literal["exit", "file", "junit"]

    @classmethod
    def build(
        cls,
        *,
        logical_argv: tuple[str, ...],
        logical_cwd: str,
        result_source: Literal["exit", "file", "junit"] = "exit",
    ) -> PinnedCommandSpec:
        if (
            not isinstance(logical_argv, tuple)
            or not logical_argv
            or len(logical_argv) > 128
            or any(
                not isinstance(item, str)
                or not item
                or "\x00" in item
                or len(item.encode()) > 4096
                for item in logical_argv
            )
        ):
            raise CodexProviderError("pinned argv must be a bounded non-empty array")
        if not isinstance(logical_cwd, str) or not logical_cwd or "\x00" in logical_cwd:
            raise CodexProviderError("pinned cwd must be a bounded relative path")
        cwd = PurePosixPath(logical_cwd)
        if cwd.is_absolute() or any(part in {"", ".."} for part in cwd.parts):
            raise CodexProviderError("pinned cwd must remain inside its workspace")
        if result_source not in {"exit", "file", "junit"}:
            raise CodexProviderError("unknown pinned result source")
        return cls(logical_argv, logical_cwd, result_source)

    @classmethod
    def parse(cls, value: object) -> PinnedCommandSpec:
        if not isinstance(value, Mapping) or set(value) != {
            "schema",
            "logical_argv",
            "logical_cwd",
            "result_source",
        }:
            raise CodexProviderError("invalid closed pinned command spec")
        if value["schema"] != "lockstep.pinned-command/v1":
            raise CodexProviderError("unsupported pinned command spec")
        argv = value["logical_argv"]
        if not isinstance(argv, list):
            raise CodexProviderError("pinned argv must be an array")
        return cls.build(
            logical_argv=tuple(argv),
            logical_cwd=value["logical_cwd"],
            result_source=value["result_source"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "lockstep.pinned-command/v1",
            "logical_argv": list(self.logical_argv),
            "logical_cwd": self.logical_cwd,
            "result_source": self.result_source,
        }


class _PinnedCodexStrategy(_CodexAttemptDriver):
    """Pinned hooks for Task 6's one durable Codex attempt driver."""

    effect_kind = "pinned"
    required_capabilities = frozenset({"workspace", "bounded_result", "sandbox"})
    workspace_purpose: Literal["no_publish_operation"] = "no_publish_operation"
    execution_class: Literal["pinned-command"] = "pinned-command"

    def __init__(self, *, permission_profile: str, **kwargs) -> None:
        if (
            not isinstance(permission_profile, str)
            or not permission_profile
            or "\x00" in permission_profile
            or len(permission_profile.encode()) > 4096
        ):
            raise CodexProviderError("pinned permission profile must be owner-selected")
        self._pinned_permission_profile = permission_profile
        super().__init__(**kwargs)
        if self._binding.credential_identity_digest is not None:
            raise CodexProviderError("pinned Codex home must be credential-free")
        self.binding_digest = hashlib.sha256(
            _canonical(
                {
                    "schema": "lockstep.pinned-runner-binding/v1",
                    "installation_digest": self._binding.digest,
                    "permission_profile": permission_profile,
                    "execution_authority": "os_user_execution",
                    "deployment_profile": "local_unsandboxed",
                }
            )
        ).hexdigest()

    @staticmethod
    def _spec(request: EffectRequest) -> PinnedCommandSpec:
        values = dict(request.inputs)
        if set(values) != {"command", "snapshot"}:
            raise CodexProviderError("pinned request has unknown or missing inputs")
        return PinnedCommandSpec.parse(values["command"])

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
