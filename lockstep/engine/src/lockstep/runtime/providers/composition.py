"""Closed released runner composition; never a dynamic provider registry."""

from __future__ import annotations

from dataclasses import dataclass

from lockstep.runtime.providers.codex import CodexRunnerAdapter
from lockstep.runtime.providers.pinned import PinnedRunnerAdapter
from lockstep.runtime.providers.claude import ClaudeRunnerAdapter
from lockstep.runtime.providers.local import RunnerSelector


@dataclass(frozen=True, slots=True)
class ReleasedRunnerComposition:
    """Configured adapters drawn from the two released runner types."""

    codex: CodexRunnerAdapter | None
    pinned: PinnedRunnerAdapter | None
    claude: ClaudeRunnerAdapter | None = None

    def resolve(self, selector: str) -> CodexRunnerAdapter | PinnedRunnerAdapter:
        selected = RunnerSelector(selector)
        if selected is RunnerSelector.CLAUDE:
            if self.claude is None:
                raise ValueError("owner runtime claude runner is unavailable")
            return self.claude
        if selector == "codex":
            if self.codex is None:
                raise ValueError("owner runtime codex runner is unavailable")
            return self.codex
        if selector == "pinned":
            if self.pinned is None:
                raise ValueError("owner runtime pinned runner is unavailable")
            return self.pinned
        raise ValueError(f"unsupported runner selector: {selector!r}")
