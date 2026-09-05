"""Checks for the marketplace package around the existing local skill."""

import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parent


def read_json(path):
    return json.loads(path.read_text())


class PackagingTests(unittest.TestCase):
    def test_manifests_and_release_versions_match(self):
        claude = read_json(PACKAGE / ".claude-plugin/plugin.json")
        codex = read_json(PACKAGE / ".codex-plugin/plugin.json")
        for field in ("name", "version", "description", "author", "license"):
            self.assertEqual(claude[field], codex[field])
        self.assertEqual(codex["name"], "code-intel")
        self.assertEqual(codex["skills"], "./skills/")
        self.assertEqual(codex["hooks"], "./hooks/hooks.json")
        self.assertEqual(codex["mcpServers"], "./.mcp.json")
        self.assertEqual(claude["mcpServers"], read_json(PACKAGE / ".mcp.json")["mcpServers"])
        self.assertEqual(claude["version"], read_json(REPO / ".release-please-manifest.json")["code-intel"])
        config = read_json(REPO / "release-please-config.json")["packages"]["code-intel"]
        self.assertEqual({item["path"] for item in config["extra-files"]},
                         {".claude-plugin/plugin.json", ".codex-plugin/plugin.json"})

    def test_marketplaces_point_at_package(self):
        for relative, source in (
            (".claude-plugin/marketplace.json", "./code-intel"),
            (".agents/plugins/marketplace.json", {"source": "local", "path": "./code-intel"}),
        ):
            entries = read_json(REPO / relative)["plugins"]
            self.assertEqual(next(item for item in entries if item["name"] == "code-intel")["source"], source)

    def test_hooks_share_baseline_entrypoints_and_build_timeout(self):
        hooks = read_json(PACKAGE / "hooks/hooks.json")["hooks"]
        for event, command in (
            ("SessionStart", "hook-status"),
            ("UserPromptSubmit", "hook-prompt"),
            ("PostToolUse", "hook-update"),
        ):
            hook = hooks[event][0]["hooks"][0]
            self.assertEqual(hook["command"], 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/code_intel.py" ' + command)
            self.assertEqual(hook["timeout"], 300)
        self.assertIn("apply_patch", hooks["PostToolUse"][0]["matcher"].split("|"))

    def test_installed_copy_resolves_mcp_entrypoints(self):
        with tempfile.TemporaryDirectory(prefix="code-intel package ") as directory:
            installed = Path(directory) / "installed plugin"
            shutil.copytree(PACKAGE, installed)
            servers = read_json(installed / ".mcp.json")["mcpServers"]
            for engine, server in servers.items():
                script, *arguments = server["args"]
                script = Path(script.replace("${CLAUDE_PLUGIN_ROOT}", str(installed)))
                spec = importlib.util.spec_from_file_location("installed_code_intel", script)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                expected = ([module.CODEGRAPH, "serve", "--mcp"] if engine == "codegraph"
                            else [module.CRG, "serve"])
                with patch.object(sys, "argv", [str(script), *arguments]), patch.object(module.os, "execv") as execute:
                    module.main()
                execute.assert_called_once_with(expected[0], expected)
            self.assertTrue((installed / "skills/code-intel/../../scripts/code_intel.py").is_file())

    def test_install_and_upgrade_keep_baseline_tool_commands(self):
        spec = importlib.util.spec_from_file_location("code_intel", PACKAGE / "scripts/code_intel.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module, "run", return_value=0) as run:
            self.assertEqual(module.install_tools(), 0)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["mise", "use", "-g", "npm:@colbymchenry/codegraph@latest"],
            ["mise", "use", "-g", "pipx:code-review-graph@latest"],
        ])
        with patch.object(module, "install_tools", return_value=0), patch.object(module, "status", return_value=0) as status:
            self.assertEqual(module.upgrade(PACKAGE), 0)
            status.assert_called_once_with(PACKAGE)


if __name__ == "__main__":
    unittest.main()
