import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parent


def read_json(relative):
    return json.loads((REPO / relative).read_text())


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.claude = read_json("code-intel/.claude-plugin/plugin.json")
        self.codex = read_json("code-intel/.codex-plugin/plugin.json")

    def test_manifest_identity_and_shared_entrypoints(self):
        for field in (
            "name",
            "version",
            "description",
            "author",
            "homepage",
            "repository",
            "license",
        ):
            self.assertEqual(self.claude[field], self.codex[field])
        self.assertEqual(self.codex["name"], "code-intel")
        self.assertEqual(self.codex["skills"], "./skills/")
        self.assertEqual(self.codex["hooks"], "./hooks/hooks.json")
        self.assertEqual(self.codex["mcpServers"], "./.mcp.json")
        self.assertEqual(self.claude["hooks"], "./hooks/hooks.json")

    def test_mcp_servers_launch_both_engines_through_the_dispatcher(self):
        expected = {
            "codegraph": {
                "command": "python3",
                "args": [
                    "${CLAUDE_PLUGIN_ROOT}/scripts/code_intel.py",
                    "serve",
                    "codegraph",
                ],
            },
            "code-review-graph": {
                "command": "python3",
                "args": [
                    "${CLAUDE_PLUGIN_ROOT}/scripts/code_intel.py",
                    "serve",
                    "crg",
                ],
            },
        }
        self.assertEqual(self.claude["mcpServers"], expected)
        self.assertEqual(read_json("code-intel/.mcp.json"), {"mcpServers": expected})

    def test_hook_events_are_finite_and_update_after_mutating_tools(self):
        hooks = read_json("code-intel/hooks/hooks.json")["hooks"]
        self.assertEqual(
            set(hooks), {"SessionStart", "UserPromptSubmit", "PostToolUse"}
        )
        self.assertEqual(
            hooks["PostToolUse"][0]["matcher"],
            "Bash|Write|Edit|NotebookEdit|apply_patch",
        )
        commands = {
            "SessionStart": "hook-status",
            "UserPromptSubmit": "hook-prompt",
            "PostToolUse": "hook-update",
        }
        for event, command in commands.items():
            command_hooks = hooks[event][0]["hooks"]
            self.assertEqual(
                command_hooks,
                [
                    {
                        "type": "command",
                        "command": (
                            'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/code_intel.py" '
                            + command
                        ),
                        "timeout": 55,
                    }
                ],
            )

    def test_marketplaces_register_only_the_package_source(self):
        claude_marketplace = read_json(".claude-plugin/marketplace.json")
        codex_marketplace = read_json(".agents/plugins/marketplace.json")
        claude_entry = next(
            p for p in claude_marketplace["plugins"] if p["name"] == "code-intel"
        )
        codex_entry = next(
            p for p in codex_marketplace["plugins"] if p["name"] == "code-intel"
        )
        self.assertEqual(claude_entry["source"], "./code-intel")
        self.assertEqual(
            codex_entry["source"], {"source": "local", "path": "./code-intel"}
        )
        self.assertEqual(
            [p for p in claude_marketplace["plugins"] if p["name"] != "code-intel"],
            [
                {
                    "name": "continuous-learning",
                    "description": "Capture runtime surprises as they happen, periodically promote them into a project's own versioned skills/docs/commands, then forget them — git is the only permanent record.",
                    "source": "./continuous-learning",
                    "category": "developer-tools",
                    "dependencies": ["redis-memory"],
                },
                {
                    "name": "redis-memory",
                    "description": "Persistent cross-session memory for AI agents. Two storage modes: semantic vector search (mem_*) for knowledge found by meaning, and key-value store (kv_*) for instant lookup. Auto-expiry via TTL + volatile-lru eviction.",
                    "source": "./redis-memory-mcp",
                    "category": "memory",
                },
                {
                    "name": "lockstep",
                    "description": "Flow-enforcement engine for coding agents: declarative yamlgraph recipes, durable runs, deterministic evidence gates",
                    "source": "./lockstep",
                    "category": "developer-tools",
                },
                {
                    "name": "speciflow",
                    "description": "Coordinated specification-driven workflow guidance for coding agents.",
                    "source": "./speciflow",
                    "category": "developer-tools",
                },
            ],
        )
        self.assertEqual(
            [p for p in codex_marketplace["plugins"] if p["name"] != "code-intel"],
            [
                {
                    "name": "speciflow",
                    "source": {"source": "local", "path": "./speciflow"},
                    "category": "Productivity",
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                }
            ],
        )

    def test_release_please_updates_both_manifest_versions(self):
        release_config = read_json("release-please-config.json")
        release_state = read_json(".release-please-manifest.json")
        extra = release_config["packages"]["code-intel"]["extra-files"]
        self.assertEqual(
            {item["path"] for item in extra},
            {".claude-plugin/plugin.json", ".codex-plugin/plugin.json"},
        )
        self.assertEqual(release_state["code-intel"], "0.1.0")
        self.assertTrue(
            all(
                item["type"] == "json" and item["jsonpath"] == "$.version"
                for item in extra
            )
        )
        self.assertEqual(
            release_config["packages"]["code-intel"]["changelog-path"],
            "CHANGELOG.md",
        )

    def test_ci_runs_for_every_packaging_input(self):
        workflow = (REPO / ".github/workflows/code-intel.yml").read_text()
        expected_paths = {
            "code-intel/**",
            ".claude-plugin/marketplace.json",
            ".agents/plugins/marketplace.json",
            "release-please-config.json",
            ".release-please-manifest.json",
            ".github/workflows/code-intel.yml",
        }
        actual_paths = {
            line.strip()[2:].strip('"')
            for line in workflow.splitlines()
            if line.strip().startswith('- "')
        }
        self.assertTrue(expected_paths.issubset(actual_paths))
        self.assertIn("python3 code-intel/tests/test_packaging.py -v", workflow)


if __name__ == "__main__":
    unittest.main()
