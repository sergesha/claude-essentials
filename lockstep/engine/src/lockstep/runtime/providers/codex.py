"""Codex-specific implementation of the provider-neutral runner contract."""

from __future__ import annotations
import os
import subprocess
from lockstep.runtime.providers.base import EffectRequest, PreparedLaunch, RunnerObservation, TerminalSafetyObservation

from lockstep.runtime.providers._codex_support import (
    CodexCaptureLimits as CodexCaptureLimits,
    CodexInstallationBinding as CodexInstallationBinding,
    CodexLaunchDecisionGate as CodexLaunchDecisionGate,
    CodexProviderError as CodexProviderError,
    CodexSandboxAttestor as CodexSandboxAttestor,
    _attestation_digest as _attestation_digest,
    _canonical as _canonical,
    _capture_executable as _capture_executable,
    _credential_identity as _credential_identity,
    _managed_argv as _managed_argv,
    _sha256_file as _sha256_file,
    _stat_identity as _stat_identity,
)
from lockstep.runtime.providers._codex_services import (
    _CodexAttemptServices as _CodexAttemptServices,
    _ServiceAlias as _ServiceAlias,
)
from lockstep.runtime.providers._codex_preparation import (
    CodexLaunchRecord as CodexLaunchRecord,
    _CodexAttemptState as _CodexAttemptState,
    _CodexPreparation as _CodexPreparation,
)
from lockstep.runtime.providers._codex_attempt import _CodexAttemptDriver


class CodexRunnerAdapter:
    """Managed Codex strategy delegated to the shared durable attempt driver."""

    required_authorities = _CodexAttemptDriver.required_authorities
    reconciliation_boundary = _CodexAttemptDriver.reconciliation_boundary
    accepted_effect_kinds = _CodexAttemptDriver.accepted_effect_kinds

    def __init__(self, **kwargs) -> None:
        self._driver = _CodexAttemptDriver(**kwargs)

    @property
    def binding_digest(self) -> str:
        return self._driver.binding_digest

    @property
    def spawn_count(self) -> int:
        return self._driver.spawn_count

    def prepare(self, request: EffectRequest) -> PreparedLaunch:
        return self._driver.prepare(request)

    def ensure_started(self, launch: PreparedLaunch) -> RunnerObservation:
        return self._driver.ensure_started(launch)

    def inspect(self, effect_id: str) -> RunnerObservation:
        return self._driver.inspect(effect_id)

    lookup = inspect

    def cancel(self, effect_id: str) -> RunnerObservation:
        return self._driver.cancel(effect_id)

    def quiesce(self, effect_id: str) -> TerminalSafetyObservation:
        return self._driver.quiesce(effect_id)

    def __getattr__(self, name: str):
        # Compatibility for provider diagnostics and the Task 6 white-box
        # conformance tests; execution authority stays in the private driver.
        return getattr(self._driver, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_driver" or "_driver" not in self.__dict__:
            object.__setattr__(self, name, value)
        elif hasattr(self._driver, name):
            setattr(self._driver, name, value)
        else:
            object.__setattr__(self, name, value)
