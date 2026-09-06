"""Exercise plugin discovery and relocated report entry points without live services."""
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parent


class ImportedPluginTests(unittest.TestCase):
    def assert_manifest_versions_match(self, plugin):
        claude = json.loads((plugin / ".claude-plugin/plugin.json").read_text())
        codex = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(codex["version"], claude["version"])

    def test_marketplace_resolves_plugin_and_runtime_files(self):
        marketplace = json.loads((REPO / ".claude-plugin/marketplace.json").read_text())
        entry = next(p for p in marketplace["plugins"] if p["name"] == "tech-radar")
        source = REPO / entry["source"]
        manifest = json.loads((source / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(manifest["name"], "tech-radar")
        self.assertTrue((source / manifest["hooks"]).is_file())
        for relative in ("hooks/ensure-stack.sh", "docker-compose.yaml",
                         "quadlet/tech-radar-cache.container",
                         "quadlet/tech-radar-searxng.container",
                         "searxng/settings.yml", "searxng/limiter.toml"):
            self.assertTrue((source / relative).is_file(), relative)
        dependencies = {p["name"] for p in marketplace["plugins"]}
        self.assertIn("redis-memory", entry["dependencies"])
        self.assertTrue(set(entry["dependencies"]) <= dependencies)

    def test_codex_package_resolves_shared_runtime_files(self):
        marketplace = json.loads((REPO / ".agents/plugins/marketplace.json").read_text())
        entry = next(p for p in marketplace["plugins"] if p["name"] == "tech-radar")
        self.assertEqual(entry, {
            "name": "tech-radar",
            "source": {"source": "local", "path": "./tech-radar"},
            "category": "Productivity",
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
        })
        manifest = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(manifest["name"], "tech-radar")
        self.assert_manifest_versions_match(PLUGIN)
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["hooks"], "./hooks/session-start.json")
        self.assertEqual(manifest["mcpServers"], "./.codex-plugin/mcp.json")
        self.assertTrue((PLUGIN / manifest["skills"]).is_dir())
        self.assertTrue((PLUGIN / manifest["hooks"]).is_file())

        mcp = json.loads((PLUGIN / manifest["mcpServers"]).read_text())["mcpServers"]
        self.assertEqual(set(mcp), {"searxng-mcp"})
        self.assertEqual(mcp["searxng-mcp"]["command"], "npx")
        self.assertEqual(mcp["searxng-mcp"]["args"], ["-y", "@tadmstr/searxng-mcp"])
        self.assertEqual(mcp["searxng-mcp"]["startup_timeout_sec"], 300)

        release = json.loads((REPO / "release-please-config.json").read_text())
        extra_files = release["packages"]["tech-radar"]["extra-files"]
        self.assertIn(
            {"type": "json", "path": ".codex-plugin/plugin.json", "jsonpath": "$.version"},
            extra_files,
        )

    def test_codex_manifest_version_tracks_release_bumps(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "tech-radar"
            shutil.copytree(PLUGIN, package)
            for host in (".claude-plugin", ".codex-plugin"):
                path = package / host / "plugin.json"
                manifest = json.loads(path.read_text())
                manifest["version"] = "0.6.0"
                path.write_text(json.dumps(manifest))
            self.assert_manifest_versions_match(package)

    def test_missing_report_entrypoints_give_host_neutral_next_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "installed plugin"
            shutil.copytree(PLUGIN, installed)
            project = Path(tmp) / "consumer project"
            project.mkdir()
            scripts = (
                installed / "skills/show-result/show.py",
                installed / "skills/render-dashboard/render.py",
                installed / "skills/collect-news/export_yaml.py",
            )
            for script in scripts:
                with self.subTest(script=script.name):
                    result = subprocess.run(
                        [sys.executable, str(script)], cwd=project,
                        capture_output=True, text=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("collect-news skill", result.stderr)
                    self.assertNotIn("/tech-radar:", result.stderr)

    def test_show_result_resolves_sibling_exporters_after_move(self):
        data = {"generated_at": "2026-07-23T12:00:00Z", "topics": [],
                "summary": "Recovered Tech Radar"}
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "installed plugin"
            shutil.copytree(PLUGIN, installed)
            project = Path(tmp) / "consumer project"
            project.mkdir()
            reports = project / "reports"
            reports.mkdir()
            report = reports / "radar-2026-07-23-1200.json"
            report.write_text(json.dumps(data))
            for fmt in ("json", "yaml", "html"):
                with self.subTest(format=fmt):
                    result = subprocess.run(
                        [sys.executable, str(installed / "skills/show-result/show.py"),
                         "--format", fmt],
                        cwd=project, capture_output=True, text=True, check=True,
                    )
                    self.assertIn("Recovered Tech Radar", result.stdout)
                    if fmt == "json":
                        self.assertEqual(json.loads(result.stdout), data)
                    elif fmt == "yaml":
                        self.assertIn('summary: "Recovered Tech Radar"', result.stdout)
                    else:
                        self.assertIn("window.__RADAR__", result.stdout)
            self.assertEqual(list(reports.iterdir()), [report])


if __name__ == "__main__":
    unittest.main()
