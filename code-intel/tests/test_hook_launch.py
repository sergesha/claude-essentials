"""Run cached hook commands after their installation disappears."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PACKAGE = Path(__file__).resolve().parents[1]


class HookLaunchTests(unittest.TestCase):
    def test_cached_commands_report_missing_installation_without_blocking(self):
        with tempfile.TemporaryDirectory(prefix="hook installation ") as directory:
            root = Path(directory) / "old plugin"
            shutil.copytree(PACKAGE, root)
            hooks = json.loads((root / "hooks/indexing.json").read_text())["hooks"]
            shutil.rmtree(root)
            for event, entries in hooks.items():
                with self.subTest(event=event):
                    result = subprocess.run(
                        entries[0]["hooks"][0]["command"], shell=True,
                        env=dict(os.environ, CLAUDE_PLUGIN_ROOT=str(root)),
                        input='{"cwd":"/tmp"}', text=True, capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    message = json.loads(result.stdout)["systemMessage"]
                    self.assertIn("/reload-plugins", message)
                    self.assertIn("Codex", message)
                    self.assertIn("restart", message.lower())

    def test_existing_script_keeps_arguments_stdin_and_exit_status(self):
        hooks = json.loads((PACKAGE / "hooks/indexing.json").read_text())["hooks"]
        with tempfile.TemporaryDirectory(prefix="hook forwarding ") as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            script = root / "scripts/code_intel.py"
            script.write_text(
                "import json, sys\n"
                "print(json.dumps([sys.argv, sys.stdin.read(), __name__, __file__]))\n"
                "sys.exit(7)\n"
            )
            for event, argument in [("SessionStart", "hook-status"),
                                    ("UserPromptSubmit", "hook-prompt"),
                                    ("PostToolUse", "hook-update")]:
                with self.subTest(event=event):
                    result = subprocess.run(
                        hooks[event][0]["hooks"][0]["command"], shell=True,
                        env=dict(os.environ, CLAUDE_PLUGIN_ROOT=str(root)),
                        input="payload", text=True, capture_output=True, timeout=10,
                    )
                    self.assertEqual(result.returncode, 7, result.stderr)
                    self.assertEqual(json.loads(result.stdout),
                                     [[str(script), argument], "payload", "__main__", str(script)])

    def test_missing_file_inside_existing_script_is_not_hidden(self):
        command = json.loads((PACKAGE / "hooks/indexing.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts/code_intel.py").write_text("raise FileNotFoundError('internal defect')\n")
            result = subprocess.run(command, shell=True,
                                    env=dict(os.environ, CLAUDE_PLUGIN_ROOT=str(root)),
                                    text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("internal defect", result.stderr)
            self.assertEqual(result.stdout, "")
