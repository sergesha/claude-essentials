"""Immutable collaborator bindings for one durable Codex attempt driver."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.providers._codex_support import (
    CodexCaptureLimits,
    CodexLaunchDecisionGate,
)
from lockstep.runtime.providers.workspaces import LocalGitWorkspaceProvider
from lockstep.runtime.sandbox import SandboxAttestor


@dataclass(frozen=True)
class _CodexAttemptServices:
    _blobs: BlobStore
    _clock: Callable[[], datetime]
    _decision_gate: CodexLaunchDecisionGate
    _limits: CodexCaptureLimits
    _sandbox: SandboxAttestor
    _workspaces: LocalGitWorkspaceProvider


@dataclass(frozen=True)
class _ServiceAlias:
    _name: str

    def __get__(self, instance: object | None, owner: type | None = None) -> object:
        del owner
        if instance is None:
            return self
        return instance._services.__dict__[self._name]

    def __set__(self, instance: object, value: object) -> None:
        instance._services = replace(
            instance._services,
            **{self._name: value},
        )
