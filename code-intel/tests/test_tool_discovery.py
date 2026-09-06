"""Tool execution uses the caller's PATH, independent of the installer."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "code_intel.py"


class ToolDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="code intel discovery ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {**os.environ, "HOME": str(self.root), "PATH": str(self.bin)}

    def executable(self, name):
        path = self.bin / name
        path.write_text("#!/bin/sh\nprintf '%s\\n' " + name + " \"$@\"\n")
        path.chmod(0o755)

    def test_serve_uses_tools_installed_only_on_path(self):
        for engine, executable, expected in (
            ("codegraph", "codegraph", "codegraph\nserve\n--mcp\n"),
            ("crg", "code-review-graph", "code-review-graph\nserve\n"),
        ):
            with self.subTest(engine=engine):
                self.executable(executable)
                result = subprocess.run(
                    [sys.executable, "-B", str(SCRIPT), "serve", engine],
                    env=self.env, capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_missing_tools_report_the_executable_name(self):
        for variable, executable in (("CODEGRAPH", "codegraph"), ("CRG", "code-review-graph")):
            with self.subTest(executable=executable):
                result = subprocess.run(
                    [sys.executable, "-B", "-c",
                     "import runpy, sys; module = runpy.run_path(sys.argv[1]); "
                     "sys.exit(module['run']([module[sys.argv[2]], '--version']))",
                     str(SCRIPT), variable],
                    env=self.env, capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 127)
                self.assertEqual(result.stderr, f"missing executable: {executable}\n")

    def test_serve_searches_path_when_tool_appears_after_discovery(self):
        self.executable("codegraph")
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import os, runpy, sys; os.environ['PATH'] = ''; "
             "module = runpy.run_path(sys.argv[1]); os.environ['PATH'] = sys.argv[2]; "
             "sys.argv = [sys.argv[1], 'serve', 'codegraph']; module['main']()",
             str(SCRIPT), str(self.bin)],
            env=self.env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "codegraph\nserve\n--mcp\n")


if __name__ == "__main__":
    unittest.main()
