"""Exercise the hooks with real Git repositories and controlled CLI processes."""

import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import hashlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/code_intel.py"


def load_module():
    spec = importlib.util.spec_from_file_location("code_intel", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackgroundIndexingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="background index ")
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        shim = '''import json, os, pathlib, sys, time
root = pathlib.Path(os.environ["TEST_INDEX_ROOT"])
engine = pathlib.Path(sys.argv[0]).name
import sqlite3,hashlib
with (root / "calls.jsonl").open("a") as f:
    f.write(json.dumps([engine, *sys.argv[1:]]) + "\\n")
if (root / "block-next").exists():
    (root / "block-next").unlink()
    (root / "started").touch()
    while not (root / "release").exists():
        time.sleep(.02)
if engine == "code-review-graph" and (root / "fail-crg").exists():
    print("controlled CRG failure", flush=True)
    sys.exit(3)
if sys.argv[1] in ("init", "build"):
    repo = pathlib.Path(sys.argv[-1])
    directory = repo / (".codegraph" if engine == "codegraph" else ".code-review-graph")
    directory.mkdir(exist_ok=True)
    (directory / "marker").touch()
if engine == "codegraph":
    repo=pathlib.Path(sys.argv[-1])
    c=sqlite3.connect(repo / ".codegraph/codegraph.db")
    c.execute("create table if not exists files(path text primary key, content_hash text)")
    c.execute("delete from files")
    for source in repo.glob("*.py"):
        c.execute("insert into files values(?,?)",(source.name,hashlib.sha256(source.read_bytes()).hexdigest()))
    c.commit();c.close()
'''
        for name in ("codegraph", "code-review-graph"):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + shim)
            path.chmod(0o755)
        interpreter = self.bin / "python"
        interpreter.write_text(f"#!{sys.executable}\n" + "import os,sys,json,pathlib\n"
                               + "if sys.argv[2] == 'health':\n print(json.dumps({'issues': [], 'expected_files': 0, 'source_files': []})); sys.exit(0)\n"
                               + "command=str(pathlib.Path(__file__).with_name('code-review-graph'))\nos.execv(command,[command,*sys.argv[2:]])\n")
        interpreter.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), TEST_INDEX_ROOT=str(self.root),
                        PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""))
        self.module = load_module()
        self.module.HOME = self.home

    def tearDown(self):
        (self.root / "release").touch()
        try:
            self.wait_idle()
        finally:
            self.temporary.cleanup()

    def repo(self, name="repo", indexed=True):
        repo = self.root / name
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / ".gitignore").write_text(".codegraph/\n.code-review-graph/\n")
        (repo / "example.py").write_text("value = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
        if indexed:
            for name in (".codegraph", ".code-review-graph"):
                (repo / name).mkdir()
                (repo / name / "marker").touch()
        if indexed:
            with sqlite3.connect(repo / ".codegraph/codegraph.db") as c:
                c.execute("create table files(path text primary key, content_hash text)")
                c.execute("insert into files values(?,?)",("example.py",hashlib.sha256((repo/"example.py").read_bytes()).hexdigest()))
        return repo

    def submit(self, cwd, command="pwd", **parts):
        payload = {"cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": command}, **parts}
        return subprocess.run([sys.executable, "-B", str(SCRIPT), "hook-update"],
                              input=json.dumps(payload), text=True, capture_output=True,
                              env=self.env, timeout=3)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail("Background worker did not reach the expected state")

    def wait_idle(self):
        cache = self.module.indexing_cache()

        def idle():
            if list((cache / "requests").glob("*.json")):
                return False
            path = cache / "worker.lock"
            if not path.exists():
                return True
            with path.open("a+b") as stream:
                return self.module.try_lock(stream)

        self.wait_for(idle)

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def states(self):
        return [json.loads(p.read_text()) for p in self.module.indexing_cache().glob("repos/*.json")]

    def test_slow_update_does_not_hold_hook_stdio_and_repeated_events_coalesce(self):
        repo = self.repo()
        (self.root / "block-next").touch()
        start = time.monotonic()
        result = self.submit(repo, command="echo SECRET_NOT_TO_PERSIST")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - start, 2)
        self.wait_for(lambda: (self.root / "started").exists())
        for _ in range(6):
            self.assertEqual(self.submit(repo).returncode, 0)
        self.assertEqual(len(self.calls()), 1)
        for path in self.module.indexing_cache().rglob("*.json"):
            self.assertNotIn("SECRET_NOT_TO_PERSIST", path.read_text())
        (self.root / "release").touch()
        self.wait_idle()
        self.assertEqual([c[1] for c in self.calls()], ["sync", "update"])
        self.assertEqual(self.states()[0]["status"], "success")

    def test_umbrella_read_only_events_skip_processed_dirty_repos(self):
        repos = [self.repo(f"umbrella/{name}") for name in ("one", "two", "three")]
        for repo in repos:
            (repo / "example.py").write_text("value = 2\n")
        umbrella = repos[0].parent
        self.submit(umbrella)
        self.wait_idle()
        self.assertEqual(len(self.calls()), 6)  # Initial catch-up has no receipt yet.
        for _ in range(3):
            self.submit(umbrella)
        self.wait_idle()
        self.assertEqual(len(self.calls()), 6)
        (repos[1] / "example.py").write_text("value = 3\n")
        self.submit(umbrella)
        self.wait_idle()
        self.assertEqual(len(self.calls()), 8)
        self.assertTrue(all(str(repos[1]) in c for c in self.calls()[-2:]))

    def test_edits_during_indexing_receive_a_follow_up_without_another_tool_event(self):
        repo = self.repo()
        (self.root / "block-next").touch()
        self.submit(repo)
        self.wait_for(lambda: (self.root / "started").exists())
        (repo / "example.py").write_text("value = 9\n")
        (self.root / "release").touch()
        self.wait_idle()
        self.assertEqual([c[1] for c in self.calls()], ["sync", "update", "sync", "update"])
        self.assertEqual(self.states()[0]["fingerprint"], self.module.repo_fingerprint(repo))

    def test_reverts_new_untracked_files_and_deletions_refresh_indexes(self):
        repo = self.repo()
        self.submit(repo)
        self.wait_idle()
        source = repo / "example.py"
        source.write_text("value = 2\n")
        self.submit(repo)
        self.wait_idle()
        subprocess.run(["git", "-C", str(repo), "checkout", "--", "example.py"], check=True)
        self.submit(repo)
        self.wait_idle()
        extra = repo / "new.py"
        extra.write_text("pass\n")
        self.submit(repo)
        self.wait_idle()
        extra.unlink()
        source.unlink()
        self.submit(repo, tool_name="Edit", tool_input={"file_path": "example.py"})
        self.wait_idle()
        self.assertEqual(len(self.calls()), 10)
        (repo / ".codegraph" / "marker").write_text("generated update")
        self.submit(repo)
        self.wait_idle()
        self.assertEqual(len(self.calls()), 10)

    def test_failure_is_logged_not_acknowledged_and_retried_on_next_event(self):
        repo = self.repo()
        (self.root / "fail-crg").touch()
        self.submit(repo)
        self.wait_idle()
        self.assertEqual(self.states()[0]["status"], "error")
        self.assertNotIn("fingerprint", self.states()[0])
        self.assertIn("controlled CRG failure", (self.module.indexing_cache() / "worker.log").read_text())
        (self.root / "fail-crg").unlink()
        self.submit(repo)
        self.wait_idle()
        self.assertEqual(self.states()[0]["status"], "success")
        self.assertEqual(len(self.calls()), 4)

    def test_new_repository_is_initialized_in_background(self):
        repo = self.repo(indexed=False)
        self.submit(repo)
        self.wait_idle()
        self.assertFalse(self.module.project_index_gaps(repo, umbrella=False))
        self.assertEqual([c[1] for c in self.calls()], ["init", "build", "register"])

    def test_clean_commit_change_is_not_filtered_out_by_dirty_status(self):
        repo = self.repo()
        self.submit(repo)
        self.wait_idle()
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "next"], check=True)
        self.submit(repo)
        self.wait_idle()
        self.assertEqual(len(self.calls()), 4)

    def test_forced_repair_bypasses_an_unchanged_successful_receipt(self):
        repo = self.repo()
        self.submit(repo)
        self.wait_idle()
        self.assertEqual(len(self.calls()), 2)
        popen = subprocess.Popen
        workers = []

        def track_worker(*args, **kwargs):
            worker = popen(*args, **kwargs)
            workers.append(worker)
            return worker

        with patch.dict(os.environ, self.env), patch.object(self.module.subprocess, "Popen", side_effect=track_worker):
            self.module.queue_index_update({"cwd": str(repo), "paths": [], "discover": False, "force": True})
            self.wait_idle()
            for worker in workers:
                self.assertEqual(worker.wait(timeout=3), 0)
        self.assertEqual(len(self.calls()), 4)
        self.submit(repo)
        self.wait_idle()
        self.assertEqual(len(self.calls()), 4)

    def test_real_startup_lock_wait_is_bounded_before_installers_run(self):
        lock_path = self.home / ".cache/code-intel/toolchain.lock"
        lock_path.parent.mkdir(parents=True)
        original = self.module.run_hook_command

        def short_budget(command, **kwargs):
            return original(command, input=kwargs["input"], timeout=.3)

        with lock_path.open("a+b") as lock:
            lock.write(b"\0")
            lock.flush()
            self.assertTrue(self.module.try_lock(lock))
            with (
                patch.dict(os.environ, self.env),
                patch.object(self.module, "run_hook_command", side_effect=short_budget),
                patch("sys.stdin", io.StringIO("{}")),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                started = time.monotonic()
                self.assertEqual(self.module.hook_start(), 0)
                self.assertLess(time.monotonic() - started, 2)
                self.assertIn("startup activation did not complete", output.getvalue())
        self.assertEqual(self.calls(), [])
        self.assertFalse((lock_path.parent / "toolchain.json").exists())


class BoundedCommandTests(unittest.TestCase):
    def test_startup_activation_has_one_budget_and_preserves_stdin_and_output(self):
        module = load_module()
        payload = '{"cwd":"/repo"}'
        completed = subprocess.CompletedProcess([], 0, "verified\n", "")
        with (
            patch("sys.stdin", io.StringIO(payload)),
            patch("sys.stdout", new_callable=io.StringIO) as output,
            patch.object(module, "run_hook_command", return_value=completed) as command,
        ):
            self.assertEqual(module.hook_start(), 0)
            self.assertEqual(command.call_args.kwargs, {"input": payload, "timeout": 240})
            self.assertEqual(command.call_args.args[0][-1], "hook-activate")
            self.assertEqual(output.getvalue(), "verified\n")

    def test_startup_timeout_is_reported_without_index_initialization(self):
        module = load_module()
        with (
            patch("sys.stdin", io.StringIO("{}")),
            patch("sys.stdout", new_callable=io.StringIO) as output,
            patch.object(module, "run_hook_command", side_effect=subprocess.TimeoutExpired("activation", 240)),
            patch.object(module, "hook_status") as status,
        ):
            self.assertEqual(module.hook_start(), 0)
            status.assert_not_called()
            self.assertIn("startup activation did not complete", output.getvalue())

    def test_prompt_sync_timeout_queues_retry_without_emitting_stale_context(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            with (
                patch.object(module, "git_root", return_value=repo),
                patch.object(module, "project_index_gaps", return_value=()),
                patch.object(module, "_index_ready", return_value=True),
                patch.object(module, "run_hook_command", side_effect=subprocess.TimeoutExpired("sync", 5)) as command,
                patch.object(module, "queue_index_update", return_value=True) as queue,
                patch("sys.stdin", io.StringIO(json.dumps({"cwd": str(repo)}))),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                self.assertEqual(module.hook_prompt(), 0)
                command.assert_called_once()
                queue.assert_called_once()
                self.assertEqual(output.getvalue(), "")

    @unittest.skipIf(os.name == "nt", "POSIX process-group termination")
    def test_timeout_kills_a_descendant_which_ignores_sigterm(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(20)"
            parent = ("import subprocess,sys,time,pathlib; "
                      "p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
                      "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(20)")
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    module.run_hook_command([sys.executable, "-c", parent, str(pid_file), child], timeout=.5)
                pid = int(pid_file.read_text())
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(.02)
                else:
                    self.fail("Timed-out indexer left a live child")
            finally:
                if pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
