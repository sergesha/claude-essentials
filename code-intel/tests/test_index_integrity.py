"""Real Git and CRG regressions; native tools are optional outside integration CI."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
SCRIPT = PACKAGE / "scripts/code_intel.py"
BRIDGE = PACKAGE / "scripts/crg_bridge.py"
CRG = shutil.which("code-review-graph")
if CRG and Path(CRG).resolve().name == "mise":
    CRG = subprocess.run(["mise", "which", "code-review-graph"], capture_output=True, text=True, check=True).stdout.strip()
CRG_PYTHON = Path(CRG).resolve().with_name("python") if CRG else None
NATIVE_CRG = CRG_PYTHON is not None and CRG_PYTHON.is_file()


def module():
    spec = importlib.util.spec_from_file_location("code_intel_integrity", SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True).stdout


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="index integrity ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / ".git/info/exclude").write_text(".codegraph/\n.code-review-graph/\n")
        self.module = module()
        self.module.HOME = self.base / "home"

    def test_discovery_rejects_invalid_markers_and_prunes_their_subtrees(self):
        for name, marker in [("uv-cache", ""), ("stale-worktree", "gitdir: /nonexistent/worktree\n")]:
            root = self.base / name
            root.mkdir()
            (root / ".git").write_text(marker)
            child = root / "nested-repo"
            child.mkdir()
            git(child, "init", "-q")
        self.assertEqual(self.module.discover_repos(self.base), [self.repo])

    def test_invalid_project_never_runs_an_indexer(self):
        invalid = self.base / "invalid"
        invalid.mkdir()
        (invalid / ".git").write_text("")
        calls = []
        self.assertEqual(self.module.setup_repo(invalid, lambda c: calls.append(c) or 0, "CG", "CRG"), 1)
        self.assertEqual(calls, [])

    def test_failed_rebuild_keeps_existing_artifacts_and_backup(self):
        index = self.repo / ".codegraph"
        index.mkdir()
        (index / "sentinel").write_text("original")
        with patch.object(self.module, "crg_command", return_value=["CRG"]), patch.object(self.module, "run", return_value=1):
            self.assertEqual(self.module.repair_project(self.repo, engine="codegraph"), 1)
        self.assertEqual((index / "sentinel").read_text(), "original")
        self.assertTrue(list(self.module.indexing_cache().glob("backups/*/.codegraph/sentinel")))

    def test_health_rejects_nonexistent_codegraph_paths(self):
        index = self.repo / ".codegraph"
        index.mkdir()
        with closing(sqlite3.connect(index / "codegraph.db")) as connection, connection:
            connection.execute("create table files(path text, content_hash text)")
            connection.execute("insert into files values('outside-missing.py', 'bad')")
        fake = subprocess.CompletedProcess([], 0, '{"expected_files":0,"source_files":[],"issues":[]}', '')
        with patch.object(self.module, "crg_command", return_value=["CRG"]), patch.object(self.module.subprocess, "run", return_value=fake):
            report = self.module.index_health(self.repo, integrity=True)
        self.assertTrue(report["codegraph"]["issues"])

    def bridge(self, *args, check=True):
        return subprocess.run([str(CRG_PYTHON), "-B", str(BRIDGE), *args], cwd=self.repo,
                              env={**os.environ, "HOME": str(self.module.HOME)},
                              text=True, capture_output=True, check=check, timeout=60)

    def assert_healthy(self):
        result = self.bridge("health", str(self.repo), "--integrity")
        report = json.loads(result.stdout)
        self.assertEqual(report["issues"], [])
        return report

    @unittest.skipUnless(NATIVE_CRG, "native CRG integration environment")
    def test_full_build_covers_untracked_sources_without_staging_them(self):
        (self.repo / "noncode.txt").write_text("tracked")
        git(self.repo, "add", "noncode.txt")
        (self.repo / "untracked.py").write_text("def newly_written():\n    return 1\n")
        index = self.repo / ".git/index"
        before = hashlib.sha256(index.read_bytes()).hexdigest()
        self.bridge("build", "--repo", str(self.repo))
        report = self.assert_healthy()
        self.assertEqual(report["expected_files"], 1)
        self.assertEqual(report["indexed_files"], 1)
        self.assertEqual(hashlib.sha256(index.read_bytes()).hexdigest(), before)
        self.assertIn("?? untracked.py", git(self.repo, "status", "--porcelain"))

    @unittest.skipUnless(NATIVE_CRG, "native CRG integration environment")
    def test_incremental_update_discovers_new_untracked_files_and_reverts(self):
        source = self.repo / "tracked.py"
        source.write_text("def first():\n    return 1\n")
        git(self.repo, "add", "tracked.py")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
        self.bridge("build", "--repo", str(self.repo))
        extra = self.repo / "later.py"
        extra.write_text("def later():\n    return 2\n")
        self.bridge("update", "--repo", str(self.repo))
        self.assertEqual(self.assert_healthy()["expected_files"], 2)
        extra.write_text("def edited():\n    return 3\n")
        self.assertNotEqual(self.bridge("health", str(self.repo), check=False).returncode, 0)
        self.bridge("update", "--repo", str(self.repo))
        self.assert_healthy()
        extra.unlink()
        self.bridge("update", "--repo", str(self.repo))
        self.assertEqual(self.assert_healthy()["expected_files"], 1)

    @unittest.skipUnless(NATIVE_CRG, "native CRG integration environment")
    def test_repair_crg_replaces_wrong_root_without_losing_git_staging(self):
        (self.repo / "source.py").write_text("def repaired():\n    return 1\n")
        self.bridge("build", "--repo", str(self.repo))
        db = self.repo / ".code-review-graph/graph.db"
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("update nodes set file_path='/old/root/source.py'")
        self.assertNotEqual(self.bridge("health", str(self.repo), check=False).returncode, 0)
        self.assertEqual(self.module.repair_project(self.repo, engine="crg"), 0)
        self.assert_healthy()
        self.assertIn("?? source.py", git(self.repo, "status", "--porcelain"))

    @unittest.skipUnless(NATIVE_CRG, "native CRG integration environment")
    def test_health_accepts_configuration_that_intentionally_has_no_nodes(self):
        (self.repo / "plain.yml").write_text("unrelated: value\n")
        (self.repo / "source.py").write_text("def actual():\n    return 1\n")
        self.bridge("build", "--repo", str(self.repo))
        self.assert_healthy()

    @unittest.skipUnless(NATIVE_CRG, "native CRG integration environment")
    def test_repair_preserves_external_location_and_reader_snapshot(self):
        (self.repo / "source.py").write_text("def repaired():\n    return 1\n")
        external = self.base / "external data"
        self.bridge("build", "--repo", str(self.repo), "--data-dir", str(external))
        registry = self.module.HOME / ".code-review-graph/registry.json"
        before = registry.read_bytes()
        with closing(sqlite3.connect(external / "graph.db")) as reader:
            reader.execute("pragma journal_mode=WAL")
            with reader:
                reader.execute("update nodes set file_path='/old/root/source.py'")
            reader.execute("begin")
            reader.execute("select count(*) from nodes").fetchone()
            with patch.dict(os.environ, HOME=str(self.module.HOME)):
                self.assertEqual(self.module.repair_project(self.repo, engine="crg"), 0)
            self.assertTrue(reader.execute("select file_path from nodes limit 1").fetchone()[0].startswith("/old/root/"))
            reader.rollback()
            self.assertTrue(reader.execute("select file_path from nodes limit 1").fetchone()[0].startswith(str(self.repo)))
        self.assertEqual(registry.read_bytes(), before)
        self.assertFalse((self.repo / ".code-review-graph/graph.db").exists())
        self.assert_healthy()

    @unittest.skipUnless(NATIVE_CRG, "native CRG integration environment")
    def test_unreadable_crg_is_backed_up_without_unlinking_live_files(self):
        index = self.repo / ".code-review-graph"
        index.mkdir()
        raw = b"not a SQLite database" * 100
        (index / "graph.db").write_bytes(raw)
        with patch.dict(os.environ, HOME=str(self.module.HOME)):
            self.assertEqual(self.module.repair_project(self.repo, engine="crg"), 1)
        self.assertEqual((index / "graph.db").read_bytes(), raw)
        saved = list(self.module.indexing_cache().glob("backups/*/.code-review-graph/graph.db"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), raw)

    @unittest.skipUnless(NATIVE_CRG and shutil.which("codegraph"), "native index integration environment")
    def test_native_codegraph_rebuild_recovers_corrupt_database(self):
        (self.repo / "source.py").write_text("def restored():\n    return 1\n")
        env = {**os.environ, "HOME": str(self.module.HOME)}
        node = self.module.tool_executable("node")
        env["PATH"] = str(Path(node).parent) + os.pathsep + env["PATH"]
        self.module.CODEGRAPH = self.module.tool_executable("codegraph")
        result = subprocess.run([self.module.CODEGRAPH, "init", str(self.repo)], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        db = self.repo / ".codegraph/codegraph.db"
        # Match an actual malformed SQLite index, retaining its file identity.
        # Native CodeGraph deliberately refuses arbitrary non-SQLite files.
        damaged = bytearray(db.read_bytes())
        damaged[100] = 255
        raw = bytes(damaged)
        db.write_bytes(raw)
        with patch.dict(os.environ, env):
            self.assertEqual(self.module.repair_project(self.repo, engine="codegraph"), 0)
        report = self.module.index_health(self.repo, integrity=True)
        self.assertEqual(report["codegraph"]["issues"], [])
        self.assertEqual(report["codegraph"]["indexed_files"], 1)
        saved = list(self.module.indexing_cache().glob("backups/*/.codegraph/codegraph.db"))
        self.assertEqual(saved[0].read_bytes(), raw)

    @unittest.skipUnless(NATIVE_CRG, "native CRG integration environment")
    def test_mcp_keeps_untracked_sources_in_an_unborn_repository(self):
        (self.repo / "noncode.txt").write_text("tracked metadata")
        git(self.repo, "add", "noncode.txt")
        (self.repo / "source.py").write_text("def original():\n    return 1\n")
        program = r'''
import asyncio, os, sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def run():
    params = StdioServerParameters(command=sys.executable, args=[sys.argv[1], sys.argv[3]], env=dict(os.environ), cwd=sys.argv[4])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            if sys.argv[3] == "serve-explicit":
                result = await client.call_tool("query_graph_tool", {"pattern":"file_summary", "target":"source.py"})
                assert result.isError and "repo_root" in str(result), result
            for full in (True, False):
                result = await client.call_tool("build_or_update_graph_tool", {"repo_root":sys.argv[2], "full_rebuild":full, "postprocess":"minimal"})
                assert not result.isError, result
                if full:
                    Path(sys.argv[2], "later.py").write_text("def later():\n    return 2\n")
            result = await client.call_tool("query_graph_tool", {"repo_root":sys.argv[2], "pattern":"file_summary", "target":str(Path(sys.argv[2], "later.py"))})
            assert not result.isError, result
            assert "later" in str(result), result
asyncio.run(run())
'''
        for mode in ("serve", "serve-explicit"):
            with self.subTest(mode=mode):
                subprocess.run([str(CRG_PYTHON), "-B", "-c", program, str(BRIDGE), str(self.repo), mode,
                                str(PACKAGE if mode == "serve-explicit" else self.repo)],
                               env={**os.environ, "HOME": str(self.module.HOME)},
                               check=True, capture_output=True, text=True, timeout=60)
        self.assertEqual(self.assert_healthy()["indexed_files"], 2)
        self.assertIn("?? later.py", git(self.repo, "status", "--porcelain"))
