"""Native Claude Code strategy on the shared durable local attempt lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

from lockstep.runtime.payload_limits import bounded_json
from lockstep.runtime.providers._codex_attempt import _CodexAttemptDriver
from lockstep.runtime.providers.base import EffectRequest
from lockstep.runtime.providers.codex import CodexRunnerAdapter
from lockstep.runtime.providers.local import (
    AttemptInstallation,
    AttemptProvider,
    ClaudeInstallationBinding,
    LaunchDetails,
)


class _ClaudeStrategy(_CodexAttemptDriver):
    provider = AttemptProvider.CLAUDE

    @staticmethod
    def _assert_no_project_control_surfaces(workspace: Path) -> None:
        # Claude's native safe mode disables project instructions, hooks and MCP
        # while preserving native authentication, including the OS keychain.
        del workspace

    def _launch_details(
        self, binding: AttemptInstallation, workspace: Path, request: EffectRequest
    ) -> LaunchDetails:
        if not isinstance(binding, ClaudeInstallationBinding):
            raise ValueError("Claude runner requires a Claude installation")
        environment = dict(binding.environment)
        environment["HOME"] = str(binding.home)
        argv = (
            str(binding.executable_path),
            "--print",
            "--output-format",
            "json",
            "--model",
            binding.model,
            "--safe-mode",
            "--no-session-persistence",
            "--permission-mode",
            "acceptEdits",
        )
        return (
            binding.executable_path,
            argv,
            tuple(sorted(environment.items())),
            None,
            None,
            binding.executable_identity,
        )

    def _final_message(self, stdout: bytes) -> str | None:
        event = bounded_json(json.loads(stdout), label="Claude JSON result")
        if (
            isinstance(event, dict)
            and event.get("type") == "result"
            and event.get("subtype") == "success"
            and event.get("is_error") is False
            and isinstance(event.get("result"), str)
        ):
            return event["result"]
        return None


class ClaudeRunnerAdapter(CodexRunnerAdapter):
    def __init__(self, **kwargs) -> None:
        self._driver = _ClaudeStrategy(**kwargs)
