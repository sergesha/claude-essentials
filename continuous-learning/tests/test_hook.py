"""Run the actual hook declared by each host; no model or Redis required."""

import json
from pathlib import Path
import subprocess
import unittest


PACKAGE = Path(__file__).resolve().parents[1]


class StartupReminderTests(unittest.TestCase):
    def test_both_hosts_execute_the_startup_reminder(self):
        for host in (".claude-plugin", ".codex-plugin"):
            with self.subTest(host=host):
                manifest = json.loads((PACKAGE / host / "plugin.json").read_text())
                hooks = json.loads((PACKAGE / manifest["hooks"]).read_text())
                outputs = []
                for group in hooks["hooks"]["SessionStart"]:
                    for hook in group["hooks"]:
                        result = subprocess.run(
                            hook["command"], shell=True, capture_output=True,
                            text=True, input="{}", timeout=5, cwd=PACKAGE,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stderr, "")
                        outputs.append(result.stdout)
                context = "\n".join(outputs)
                self.assertIn("learn: none", context)
                self.assertIn("mem_save", context)
                self.assertIn("continuous-learning", context)


if __name__ == "__main__":
    unittest.main()
