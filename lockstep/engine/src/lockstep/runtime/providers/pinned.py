"""Pinned commands using the shared durable Codex local-attempt lifecycle."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Literal

from lockstep.runtime.effects.descriptors import parse_effect_result
from lockstep.runtime.effects.models import PinnedCommandSpec
from lockstep.runtime.providers.base import EffectRequest
from lockstep.runtime.providers.local import (
    AttemptInstallation, AttemptProvider, DirectInstallationBinding, ExecutableIdentity,
    LaunchDetails,
)
from lockstep.runtime.providers.codex import (
    CodexLaunchRecord,
    CodexProviderError,
    _CodexAttemptDriver,
)


class _PinnedStrategy(_CodexAttemptDriver):
    """Direct command hooks for the shared durable attempt driver."""

    accepted_effect_kinds = frozenset({"pinned", "verify"})
    required_capabilities = frozenset({"workspace", "bounded_result", "sandbox"})
    workspace_purpose: Literal["no_publish_operation"] = "no_publish_operation"
    execution_class: Literal["pinned-command"] = "pinned-command"

    provider = AttemptProvider.DIRECT_LOCAL

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        if not isinstance(self._binding, DirectInstallationBinding):
            raise ValueError("direct-local runner requires a direct-local binding")

    def _launcher_binding_digest(
        self, _binding: AttemptInstallation
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

    def _execution_cwd(self, workspace: Path, request: EffectRequest) -> Path:
        return (workspace / self._spec(request).logical_cwd).resolve(strict=False)

    @staticmethod
    def _assert_no_project_control_surfaces(workspace: Path) -> None:
        del workspace

    def _launch_details(self, binding: AttemptInstallation, workspace: Path,
                        request: EffectRequest) -> LaunchDetails:
        if not isinstance(binding, DirectInstallationBinding):
            raise CodexProviderError("direct-local runner requires a direct installation")
        spec = self._spec(request)
        cwd = self._execution_cwd(workspace, request)
        if cwd != workspace and workspace not in cwd.parents:
            raise CodexProviderError("pinned cwd escaped its workspace")
        requested = spec.logical_argv[0]
        if "/" in requested:
            invocation = cwd / requested
        else:
            search_path = os.pathsep.join(
                str(cwd / entry)
                for entry in dict(binding.environment)["PATH"].split(os.pathsep)
            )
            found = shutil.which(requested, path=search_path)
            if found is None:
                raise CodexProviderError("pinned command executable is unavailable")
            invocation = Path(found)
        # Invocation paths carry interpreter semantics (notably pyvenv.cfg).
        # Bind the resolved bytes without rewriting the selected invocation.
        executable = invocation.resolve(strict=True)
        identity = ExecutableIdentity.capture(executable)
        return (executable, (str(invocation), *spec.logical_argv[1:]),
                binding.environment, None, None, identity)

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
    accepted_effect_kinds = _PinnedStrategy.accepted_effect_kinds

    def __init__(self, **kwargs) -> None:
        self._driver = _PinnedStrategy(**kwargs)

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

    def launch_record(self, effect_id: str):
        return self._driver.launch_record(effect_id)

    def __getattr__(self, name: str):
        return getattr(self._driver, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_driver" or "_driver" not in self.__dict__:
            object.__setattr__(self, name, value)
        elif hasattr(self._driver, name):
            setattr(self._driver, name, value)
        else:
            object.__setattr__(self, name, value)
