"""Plugin activation provisions and verifies the tools once per release."""

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location("code_intel", PACKAGE / "scripts/code_intel.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ToolchainLifecycleTests(unittest.TestCase):
    def test_first_activation_upgrades_then_verifies_and_next_activation_skips_installers(self):
        module = load_module()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(module, "HOME", Path(directory)),
            patch.object(module, "refresh_mise_paths"),
            patch.object(module, "toolchain_health", return_value={"igraph": "1.0", "crg": "/tools/crg"}),
            patch.object(module, "install_tools", return_value=0) as install,
        ):
            self.assertEqual(module.ensure_tools(), 0)
            install.assert_called_once()
            install.reset_mock()
            self.assertEqual(module.ensure_tools(), 0)
            install.assert_not_called()

    def test_new_plugin_release_and_changed_tool_environment_trigger_update(self):
        module = load_module()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(module, "HOME", Path(directory)),
            patch.object(module, "refresh_mise_paths"),
            patch.object(module, "toolchain_health", return_value={"igraph": "1.0", "crg": "/tools/crg"}) as health,
            patch.object(module, "install_tools", return_value=0) as install,
            patch.object(module, "plugin_version", return_value="1") as version,
        ):
            module.ensure_tools()
            version.return_value = "2"
            module.ensure_tools()
            health.return_value = {"igraph": "1.1", "crg": "/tools/new-crg"}
            module.ensure_tools()
            self.assertEqual(install.call_count, 3)

    def test_failed_installation_is_not_saved_as_success(self):
        module = load_module()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(module, "HOME", Path(directory)),
            patch.object(module, "refresh_mise_paths"),
            patch.object(module, "toolchain_health", return_value={"igraph": "1.0"}),
            patch.object(module, "install_tools", side_effect=[1, 0]) as install,
        ):
            self.assertEqual(module.ensure_tools(), 1)
            self.assertEqual(module.ensure_tools(), 0)
            self.assertEqual(install.call_count, 2)

    def test_installer_success_without_igraph_is_not_accepted(self):
        module = load_module()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(module, "HOME", Path(directory)),
            patch.object(module, "refresh_mise_paths"),
            patch.object(module, "toolchain_health", side_effect=RuntimeError("igraph is missing")),
            patch.object(module, "install_tools", return_value=0),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(module.ensure_tools(), 1)
            self.assertIn("igraph is missing", output.getvalue())
            self.assertFalse((Path(directory) / ".cache/code-intel/toolchain.json").exists())

    def test_session_start_provisions_before_index_checks_and_fails_open_on_error(self):
        module = load_module()
        for error in (0, 1):
            calls = []
            with (
                self.subTest(error=error),
                patch.object(module, "ensure_tools", side_effect=lambda: calls.append("tools") or error),
                patch.object(module, "hook_status", side_effect=lambda: calls.append("indexes") or 0),
            ):
                self.assertEqual(module.hook_activate(), 0)
                self.assertEqual(calls, ["tools", "indexes"] if not error else ["tools"])

    def test_both_client_manifests_use_the_provisioning_session_hook(self):
        hooks = json.loads((PACKAGE / "hooks/indexing.json").read_text())["hooks"]
        self.assertTrue(hooks["SessionStart"][0]["hooks"][0]["command"].endswith(" hook-start"))
        for manifest in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
            self.assertEqual(json.loads((PACKAGE / manifest).read_text())["hooks"], "./hooks/indexing.json")

    def test_mise_install_preserves_owner_version_options_and_existing_extras(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "mise.toml"
            config.write_text('[tools]\n"pipx:code-review-graph" = {version="2.3.9", extras=["enrichment"], uvx=true}\n')
            executable = root / "install/bin/code-review-graph"
            executable.parent.mkdir(parents=True)
            executable.touch()
            response = [{"active": True, "requested_version": "2.3.9", "install_path": str(root / "install"),
                         "source": {"type": "mise.toml", "path": str(config)}}]
            with (
                patch.object(module.shutil, "which", side_effect=lambda name: "/bin/mise" if name == "mise" else str(executable)),
                patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(response), "")),
            ):
                command = module.mise_install_command("pipx:code-review-graph", "code-review-graph")
            self.assertEqual(command[:5], ["/bin/mise", "use", "--path", str(config), "--force"])
            self.assertEqual(command[-1], "pipx:code-review-graph@2.3.9")
            self.assertEqual(json.loads(command[6].removeprefix("extras=")), ["communities", "enrichment"])

    def test_mise_must_not_take_over_a_different_active_installation(self):
        module = load_module()
        response = [{"active": True, "install_path": "/mise/installation", "requested_version": "latest",
                     "source": {"type": "mise.toml", "path": "/mise/config.toml"}}]
        with (
            patch.object(module.shutil, "which", side_effect=lambda name: "/bin/mise" if name == "mise" else "/other/code-review-graph"),
            patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(response), "")),
        ):
            self.assertIsNone(module.mise_install_command("pipx:code-review-graph", "code-review-graph"))

    def test_mise_shim_is_resolved_before_environment_checks(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mise").touch()
            shim = root / "code-review-graph"
            shim.symlink_to(root / "mise")
            with (
                patch.object(module.shutil, "which", side_effect=lambda name: str(root / name)),
                patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "/real/venv/bin/code-review-graph\n", "")),
            ):
                self.assertEqual(module.tool_executable("code-review-graph"), "/real/venv/bin/code-review-graph")

    def test_missing_mise_tool_is_installed_from_its_existing_declaration(self):
        module = load_module()
        response = [{"active": False, "installed": False, "install_path": "/mise/installation", "requested_version": "1.6.2",
                     "source": {"type": "mise.toml", "path": "/mise/config.toml"}}]
        with (
            patch.object(module.shutil, "which", side_effect=lambda name: "/bin/mise" if name == "mise" else None),
            patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(response), "")),
        ):
            self.assertEqual(module.mise_install_command("npm:@colbymchenry/codegraph", "codegraph"),
                             ["/bin/mise", "use", "--path", "/mise/config.toml", "npm:@colbymchenry/codegraph@1.6.2"])

    def test_existing_pipx_environment_is_not_replaced_by_uv(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            crg = root / "bin/code-review-graph"
            crg.touch()
            (root / "pipx_metadata.json").write_text("{}")
            tools = {"codegraph": "/tools/codegraph", "code-review-graph": str(crg),
                     "npm": "/tools/npm", "uv": "/tools/uv", "pipx": "/tools/pipx"}
            calls = []
            with patch.object(module.shutil, "which", side_effect=tools.get):
                self.assertEqual(module.install_tools(upgrade=True, runner=lambda cmd: calls.append(cmd) or 0), 0)
            self.assertEqual(calls[1], ["/tools/pipx", "install", "--force", "code-review-graph[communities]"])


if __name__ == "__main__":
    unittest.main()
