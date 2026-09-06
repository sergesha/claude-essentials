"""Missing tools are offered for installation and provisioned only on request."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "code_intel.py"


class MissingToolsTests(unittest.TestCase):
    def invoke(self, available, command, *, indexed=False):
        with tempfile.TemporaryDirectory(prefix="code intel prerequisites ") as directory:
            root = Path(directory)
            binaries = root / "bin"
            binaries.mkdir()
            project = root / "project"
            project.mkdir()
            if indexed:
                for name in (".git", ".codegraph", ".code-review-graph"):
                    (project / name).mkdir()
            registry = root / ".code-review-graph"
            registry.mkdir()
            (registry / "registry.json").write_text('{"repos": []}')
            log = root / "calls.log"
            for name in available:
                executable = binaries / name
                body = f"#!/bin/sh\nprintf '%s\\n' '{name}' \"$@\" >> \"$TEST_TOOL_LOG\"\n"
                if name == "git":
                    body += "[ -d .git ] || exit 128\nprintf '%s\\n' \"$PWD\"\n"
                executable.write_text(body)
                executable.chmod(0o755)
            before = set(root.rglob("*"))
            arguments = ["upgrade", "--base", str(project)] if command == "upgrade" else [command]
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), *arguments],
                env={**os.environ, "HOME": str(root), "PATH": str(binaries), "TEST_TOOL_LOG": str(log)},
                cwd=project, input=json.dumps({"cwd": str(project)}),
                capture_output=True, text=True, check=False,
            )
            calls = log.read_text().splitlines() if log.exists() else []
            added = {path.relative_to(root).as_posix() for path in set(root.rglob("*")) - before}
            return result, calls, added

    def test_hook_offers_missing_tools_before_git_or_index_checks(self):
        for indexed in (False, True):
            for available, missing in (
                ({"git", "npm", "uv"}, "codegraph, code-review-graph"),
                ({"git", "npm", "uv", "codegraph"}, "code-review-graph"),
                ({"git", "npm", "uv", "code-review-graph"}, "codegraph"),
            ):
                with self.subTest(indexed=indexed, missing=missing):
                    result, calls, added = self.invoke(available, "hook-status", indexed=indexed)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"missing tools on PATH: {missing}.", result.stdout)
                    self.assertIn("code-intel skill", result.stdout)
                    self.assertIn("Ask the user", result.stdout)
                    self.assertIn("install-tools", result.stdout)
                    self.assertIn("verify", result.stdout)
                    self.assertEqual(calls, [])
                    self.assertEqual(added, set())

    def test_hook_with_available_tools_keeps_initialized_behavior(self):
        result, calls, added = self.invoke(
            {"git", "npm", "uv", "codegraph", "code-review-graph"}, "hook-status", indexed=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Code intelligence: initialized", result.stdout)
        self.assertNotIn("install-tools", result.stdout)
        self.assertEqual(calls[:1], ["git"])
        self.assertNotIn("npm", calls)
        self.assertNotIn("uv", calls)
        self.assertEqual(added, {"calls.log"})

    def test_install_only_missing_engines_with_only_required_managers(self):
        for available, expected in (
            ({"codegraph", "code-review-graph"}, []),
            ({"codegraph", "uv"}, ["uv", "tool", "install", "--upgrade", "code-review-graph"]),
            ({"codegraph", "pipx"}, ["pipx", "install", "--force", "code-review-graph"]),
            ({"code-review-graph", "npm"}, ["npm", "install", "--global", "@colbymchenry/codegraph"]),
        ):
            with self.subTest(available=available):
                result, calls, _ = self.invoke(available, "install-tools")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, expected)

    def test_missing_required_manager_prevents_any_install(self):
        result, calls, added = self.invoke({"npm"}, "install-tools")
        self.assertEqual(result.returncode, 127)
        self.assertIn("uv or pipx", result.stderr)
        self.assertEqual(calls, [])
        self.assertEqual(added, set())

    def test_upgrade_still_upgrades_both_existing_engines(self):
        result, calls, _ = self.invoke({"npm", "uv", "codegraph", "code-review-graph"}, "upgrade")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[:9], [
            "npm", "install", "--global", "@colbymchenry/codegraph",
            "uv", "tool", "install", "--upgrade", "code-review-graph",
        ])
        self.assertEqual(calls[9:], ["codegraph", "--version", "code-review-graph", "--version"])


if __name__ == "__main__":
    unittest.main()
