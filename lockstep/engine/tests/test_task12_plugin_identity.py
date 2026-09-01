from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_codex_plugin_identity_describes_native_workflows_not_runner_subcalls() -> None:
    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())

    assert manifest["name"] == "lockstep"
    assert manifest["interface"] == {
        "displayName": "Lockstep",
        "shortDescription": "Native durable workflows for coding agents",
        "longDescription": (
            "Author and run deterministic yamlgraph/LangGraph workflows with "
            "evidence-gated external effects, native recovery, and artifact publication."
        ),
        "developerName": "sergesha",
        "category": "Developer Tools",
        "capabilities": [
            "Skills",
            "MCP server",
            "Policy hooks",
            "Native durable workflows",
            "External-effect bridging",
        ],
        "defaultPrompt": [
            "Use lockstep to author or run the requested native workflow and validate its evidence."
        ],
    }
    assert "LOCKSTEP_RUNNER" not in json.dumps(manifest, sort_keys=True)
