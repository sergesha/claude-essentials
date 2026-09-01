"""Closed released runner composition; never a dynamic provider registry."""

from __future__ import annotations

from dataclasses import dataclass

from lockstep.runtime.providers.codex import CodexRunnerAdapter
from lockstep.runtime.providers.pinned import PinnedRunnerAdapter


@dataclass(frozen=True, slots=True)
class ReleasedRunnerComposition:
    """Exactly the two runner adapters released by this distribution."""

    codex: CodexRunnerAdapter
    pinned: PinnedRunnerAdapter

    def resolve(self, selector: str) -> CodexRunnerAdapter | PinnedRunnerAdapter:
        if selector == "codex":
            return self.codex
        if selector == "pinned":
            return self.pinned
        raise ValueError(f"unsupported runner selector: {selector!r}")
