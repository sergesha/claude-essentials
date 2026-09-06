"""Closed released runner composition; never a dynamic provider registry."""

from __future__ import annotations

from dataclasses import dataclass

from lockstep.runtime.providers.codex import CodexRunnerAdapter
from lockstep.runtime.providers.pinned import PinnedRunnerAdapter


@dataclass(frozen=True, slots=True)
class ReleasedRunnerComposition:
    """Configured adapters drawn from the two released runner types."""

    codex: CodexRunnerAdapter | None
    pinned: PinnedRunnerAdapter | None

    def resolve(self, selector: str) -> CodexRunnerAdapter | PinnedRunnerAdapter:
        if selector == "codex":
            if self.codex is None:
                raise ValueError("owner runtime codex runner is unavailable")
            return self.codex
        if selector == "pinned":
            if self.pinned is None:
                raise ValueError("owner runtime pinned runner is unavailable")
            return self.pinned
        raise ValueError(f"unsupported runner selector: {selector!r}")
