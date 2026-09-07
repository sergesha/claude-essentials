"""Non-destructive tests for startup hook data and lifecycle controls."""

import os
from pathlib import Path
import shutil
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

    def test_compose_restarts_only_searxng_when_config_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            calls = self.run_compose_with_stub(root, data)
            commands = [line.split("\t", 1)[1] for line in calls]

            self.assertEqual(
                [command for command in commands if command.startswith("restart ")],
                ["restart tech-radar-searxng"],
            )
            self.assertIn("exec -i tech-radar-searxng python -", commands)

    def test_compose_does_not_restart_when_config_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            self.seed_current_config(data)
            calls = self.run_compose_with_stub(root, data)
            commands = [line.split("\t", 1)[1] for line in calls]

            self.assertFalse(any(command.startswith("restart ") for command in commands))

    def test_quadlet_restarts_only_searxng_when_config_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            self.seed_current_units(root, data)
            calls = self.run_quadlet_with_stubs(root, data)

            restart_calls = [line for line in calls if "--user restart " in line]
            self.assertEqual(
                restart_calls,
                ["systemctl\t--user restart tech-radar-searxng.service"],
            )
            self.assertIn("podman\texec -i tech-radar-searxng python -", calls)

    def test_quadlet_does_not_restart_when_config_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            self.seed_current_config(data)
            self.seed_current_units(root, data)
            calls = self.run_quadlet_with_stubs(root, data)

            self.assertFalse(any("--user restart " in line for line in calls))

    def test_json_readiness_retries_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = self.run_compose_with_stub(
                root, root / "data", probe_result=1, expected_returncode=1
            )

            probes = [line for line in calls if "\texec -i tech-radar-searxng python -" in line]
            self.assertEqual(len(probes), 15)

    def test_json_disabled_fails_without_retrying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = self.run_compose_with_stub(
                root, root / "data", probe_result=3, expected_returncode=1
            )

            probes = [line for line in calls if "\texec -i tech-radar-searxng python -" in line]
            self.assertEqual(len(probes), 1)

    def test_config_copy_failure_stops_before_container_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            for name, body in {
                "cp": "#!/bin/sh\nexit 1\n",
                "docker": "#!/bin/sh\ntouch \"$TECH_RADAR_TEST_DOCKER_CALLED\"\n",
            }.items():
                command = bindir / name
                command.write_text(body)
                command.chmod(0o755)
            called = root / "docker.called"
            env = dict(
                os.environ,
                PATH=str(bindir) + os.pathsep + os.defpath,
                HOME=str(root / "home"),
                CLAUDE_PLUGIN_ROOT=str(PLUGIN),
                CLAUDE_PLUGIN_DATA=str(root / "data"),
                TECH_RADAR_STACK="compose",
                TECH_RADAR_TEST_DOCKER_CALLED=str(called),
            )

            result = subprocess.run(
                ["sh", str(HOOK)], cwd=root, env=env,
                capture_output=True, text=True, timeout=5,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(called.exists())

    def seed_current_config(self, data):
        target = data / "searxng"
        target.mkdir(parents=True)
        for name in ("settings.yml", "limiter.toml"):
            shutil.copyfile(PLUGIN / "searxng" / name, target / name)

    def seed_current_units(self, root, data):
        unitdir = root / "config/containers/systemd"
        unitdir.mkdir(parents=True)
        for source in (PLUGIN / "quadlet").glob("*.container"):
            rendered = source.read_text().replace("@DATA_DIR@", str(data))
            (unitdir / source.name).write_text(rendered)

    def run_compose_with_stub(
        self, root, plugin_data, override=None, probe_result=0, expected_returncode=0
    ):
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
            "[ \"$1\" = exec ] && exit \"$TECH_RADAR_TEST_PROBE_RESULT\"\n"
            "exit 0\n"
        )
        docker.chmod(0o755)
        sleep = bindir / "sleep"
        sleep.write_text("#!/bin/sh\nexit 0\n")
        sleep.chmod(0o755)
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
            TECH_RADAR_TEST_PROBE_RESULT=str(probe_result),
        )
        if override is not None:
            env["TECH_RADAR_DATA_DIR"] = str(override)
        else:
            env.pop("TECH_RADAR_DATA_DIR", None)
        result = subprocess.run(
            ["sh", str(HOOK)], cwd=root, env=env,
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, expected_returncode, result.stderr)
        return log.read_text().splitlines()

    def run_quadlet_with_stubs(self, root, plugin_data):
        bindir = root / "bin"
        bindir.mkdir()
        log = root / "runtime.calls"
        podman = bindir / "podman"
        podman.write_text(
            "#!/bin/sh\n"
            "printf 'podman\\t%s\\n' \"$*\" >>\"$TECH_RADAR_TEST_RUNTIME_LOG\"\n"
            "[ \"$1\" = exec ] && exit 0\n"
            "exit 1\n"
        )
        podman.chmod(0o755)
        systemctl = bindir / "systemctl"
        systemctl.write_text(
            "#!/bin/sh\n"
            "printf 'systemctl\\t%s\\n' \"$*\" >>\"$TECH_RADAR_TEST_RUNTIME_LOG\"\n"
        )
        systemctl.chmod(0o755)
        env = dict(
            os.environ,
            PATH=str(bindir) + os.pathsep + os.defpath,
            HOME=str(root / "home"),
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_RUNTIME_DIR=str(root / "run"),
            CLAUDE_PLUGIN_ROOT=str(PLUGIN),
            CLAUDE_PLUGIN_DATA=str(plugin_data),
            TECH_RADAR_STACK="quadlet",
            TECH_RADAR_TEST_RUNTIME_LOG=str(log),
        )
        result = subprocess.run(
            ["sh", str(HOOK)], cwd=root, env=env,
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return log.read_text().splitlines()


if __name__ == "__main__":
    unittest.main()
