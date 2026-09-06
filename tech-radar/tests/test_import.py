"""Exercise plugin discovery and relocated report entry points without live services."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parent


class ImportedPluginTests(unittest.TestCase):
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

    def test_show_result_resolves_sibling_exporters_after_move(self):
        data = {"generated_at": "2026-07-23T12:00:00Z", "topics": [],
                "summary": "Recovered Tech Radar"}
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            reports.mkdir()
            report = reports / "radar-2026-07-23-1200.json"
            report.write_text(json.dumps(data))
            for fmt in ("json", "yaml", "html"):
                with self.subTest(format=fmt):
                    result = subprocess.run(
                        [sys.executable, str(PLUGIN / "skills/show-result/show.py"),
                         "--format", fmt],
                        cwd=tmp, capture_output=True, text=True, check=True,
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
