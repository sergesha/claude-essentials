"""Non-destructive tests for startup hook data and lifecycle controls."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PLUGIN = Path(__file__).resolve().parents[1]
HOOK = PLUGIN / "hooks/ensure-stack.sh"


class EnsureStackTests(unittest.TestCase):
    def test_external_mode_exits_without_environment_or_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            env = {"PATH": os.defpath, "TECH_RADAR_STACK": "external"}
            result = subprocess.run(
                ["sh", str(HOOK)], cwd=work, env=env,
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            self.assertEqual(list(work.iterdir()), [])

    def test_explicit_data_override_drives_config_and_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy data"
            override = root / "shared data"
            calls = self.run_compose_with_stub(root, legacy, override)

            self.assertFalse(legacy.exists())
            self.assertTrue((override / "searxng/settings.yml").is_file())
            self.assertTrue((override / "searxng/limiter.toml").is_file())
            self.assertTrue(calls)
            self.assertTrue(all(line.split("\t", 1)[0] == str(override) for line in calls))
            self.assertFalse(any("rm -f" in line for line in calls))

    def test_legacy_plugin_data_default_remains_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy data"
            calls = self.run_compose_with_stub(root, legacy)

            self.assertTrue((legacy / "searxng/settings.yml").is_file())
            self.assertTrue((legacy / "searxng/limiter.toml").is_file())
            self.assertTrue(calls)
            self.assertTrue(all(line.split("\t", 1)[0] == str(legacy) for line in calls))
            self.assertFalse(any("rm -f" in line for line in calls))

    def run_compose_with_stub(self, root, plugin_data, override=None):
        bindir = root / "bin"
        bindir.mkdir()
        log = root / "docker.calls"
        docker = bindir / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            "printf '%s\\t%s\\n' \"${CLAUDE_PLUGIN_DATA-}\" \"$*\" >>\"$TECH_RADAR_TEST_DOCKER_LOG\"\n"
            "case \"$*\" in\n"
            "  *Destination*) printf '%s\\n' \"$TECH_RADAR_TEST_MOUNT\" ;;\n"
            "  *compose.project*) printf '%s\\n' tech-radar ;;\n"
            "esac\n"
        )
        docker.chmod(0o755)
        selected = override or plugin_data
        env = dict(
            os.environ,
            PATH=str(bindir) + os.pathsep + os.defpath,
            HOME=str(root / "home"),
            CLAUDE_PLUGIN_ROOT=str(PLUGIN),
            CLAUDE_PLUGIN_DATA=str(plugin_data),
            TECH_RADAR_STACK="compose",
            TECH_RADAR_TEST_DOCKER_LOG=str(log),
            TECH_RADAR_TEST_MOUNT=str(selected / "searxng"),
        )
        if override is not None:
            env["TECH_RADAR_DATA_DIR"] = str(override)
        else:
            env.pop("TECH_RADAR_DATA_DIR", None)
        result = subprocess.run(
            ["sh", str(HOOK)], cwd=root, env=env,
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return log.read_text().splitlines()


if __name__ == "__main__":
    unittest.main()
