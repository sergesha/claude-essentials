import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import ANY, patch


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "code_intel.py"


def load_module():
    spec = importlib.util.spec_from_file_location("code_intel", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CodeIntelTests(unittest.TestCase):
    def setUp(self):
        tools = tempfile.TemporaryDirectory(prefix="code intel test tools ")
        self.addCleanup(tools.cleanup)
        for name in ("codegraph", "code-review-graph"):
            executable = Path(tools.name) / name
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        environment = patch.dict(os.environ, {"PATH": tools.name + os.pathsep + os.environ.get("PATH", "")})
        environment.start()
        self.addCleanup(environment.stop)

    def test_atomic_write_does_not_touch_identical_file(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "same.txt"
            path.write_text("same\n")
            os.utime(path, ns=(1_000_000_000, 1_000_000_000))
            before = path.stat().st_mtime_ns
            module.atomic_write(path, "same\n")
            self.assertEqual(path.stat().st_mtime_ns, before)

    def test_discover_repos_supports_worktree_git_file(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "worktree"
            repo.mkdir()
            (repo / ".git").write_text("gitdir: /tmp/example\n")
            self.assertEqual(module.discover_repos(base), [repo.resolve()])

    def test_setup_repo_stops_after_build_failure(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git").mkdir()
            calls = []

            def runner(command):
                calls.append(command)
                return 1 if command[:2] == ["CRG", "build"] else 0

            result = module.setup_repo(repo, runner, "CG", "CRG")
            self.assertEqual(result, 1)
            resolved = str(repo.resolve())
            self.assertEqual(calls, [["CG", "init", resolved], ["CRG", "build", "--repo", resolved]])

    def test_setup_repo_adds_indexes_to_local_git_exclude(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git" / "info").mkdir(parents=True)
            result = module.setup_repo(repo, lambda _command: 0, "CG", "CRG")
            self.assertEqual(result, 0)
            exclude = (repo / ".git" / "info" / "exclude").read_text()
            self.assertEqual(exclude.count(".codegraph/"), 1)
            self.assertEqual(exclude.count(".code-review-graph/"), 1)

    def test_update_repo_never_initializes_missing_indexes(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git" / "info").mkdir(parents=True)
            calls = []
            self.assertEqual(module.update_repo(repo, lambda command: calls.append(command) or 0, "CG", "CRG"), 1)
            self.assertEqual(calls, [])

            (repo / ".codegraph").mkdir()
            (repo / ".code-review-graph").mkdir()
            self.assertEqual(module.update_repo(repo, lambda command: calls.append(command) or 0, "CG", "CRG"), 0)
            resolved = str(repo.resolve())
            self.assertEqual(calls, [["CG", "sync", resolved], ["CRG", "update", "--skip-flows", "--repo", resolved]])

    def test_update_repo_reports_git_exclude_decode_error(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git" / "info").mkdir(parents=True)
            (repo / ".codegraph").mkdir()
            (repo / ".git" / "info" / "exclude").write_bytes(b"\xff")
            calls = []
            self.assertEqual(module.update_repo(repo, lambda command: calls.append(command) or 0, "CG", "CRG"), 1)
            self.assertEqual(calls, [])

    def test_hook_update_initializes_a_missing_worktree_index(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git").write_text("gitdir: /tmp/worktrees/example\n")
            with (
                patch.object(module, "hook_repos", return_value={repo}),
                patch.object(module, "setup_project", return_value=0) as setup,
                patch.object(module.subprocess, "run") as run,
            ):
                with patch("sys.stdin", io.StringIO(json.dumps({"cwd": str(repo)}))):
                    module.hook_update()
                setup.assert_called_once_with(repo.resolve(), runner=ANY)
                run.assert_not_called()

    def test_hook_update_only_updates_an_existing_crg_index(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git").mkdir()
            (repo / ".codegraph").mkdir()
            (repo / ".code-review-graph").mkdir()
            with patch.object(module, "hook_repos", return_value={repo}), patch.object(module.subprocess, "run") as run:
                with patch("sys.stdin", io.StringIO(json.dumps({"cwd": str(repo)}))):
                    module.hook_update()
                run.assert_called_once()

    def test_hook_update_fails_open_when_crg_executable_is_missing(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            module.subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".codegraph").mkdir()
            (repo / ".code-review-graph").mkdir()
            source = repo / "example.py"
            source.write_text("value = 1\n")
            payload = {"cwd": str(repo), "tool_name": "Write", "tool_input": {"file_path": str(source)}}
            with (
                patch.object(module, "CRG", str(repo / "missing-code-review-graph")),
                patch("sys.stdin", io.StringIO(json.dumps(payload))),
            ):
                self.assertEqual(module.hook_update(), 0)

    def test_hook_update_is_fail_open_for_non_object_payload_parts(self):
        module = load_module()
        payloads = (
            {"cwd": "/private/tmp", "tool_name": "Bash", "tool_input": {}, "tool_response": "ok"},
            {"cwd": "/private/tmp", "tool_name": "Write", "tool_input": "path", "tool_response": []},
            [],
        )
        for payload in payloads:
            with (
                self.subTest(payload=payload),
                patch.object(module, "hook_repos", return_value=set()),
                patch("sys.stdin", io.StringIO(json.dumps(payload))),
            ):
                self.assertEqual(module.hook_update(), 0)

    def test_hook_status_initializes_a_missing_worktree_index(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git").write_text("gitdir: /tmp/worktrees/example\n")

            def initialize(path, **_kwargs):
                self.assertEqual(path, repo.resolve())
                (repo / ".codegraph").mkdir()
                (repo / ".code-review-graph").mkdir()
                return 0

            stdout = io.StringIO()
            with (
                patch.object(module, "git_root", return_value=repo.resolve()),
                patch.object(module, "setup_project", side_effect=initialize) as setup,
                patch("sys.stdin", io.StringIO(json.dumps({"cwd": str(repo)}))),
                redirect_stdout(stdout),
            ):
                self.assertEqual(module.hook_status(), 0)
            setup.assert_called_once_with(repo.resolve(), runner=ANY)
            self.assertEqual(stdout.getvalue(), f"Code intelligence: initialized — {repo.resolve()}\n")

    def test_prompt_hook_initializes_missing_current_worktree_before_query(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git").write_text("gitdir: /tmp/worktrees/example\n")
            completed = module.subprocess.CompletedProcess(
                ["CG", "prompt-hook"], 0,
                stdout="<codegraph_context>ctx</codegraph_context>\n", stderr="",
            )
            payload = json.dumps({"prompt": "find callers", "cwd": str(repo)})
            with (
                patch.object(module, "git_root", return_value=repo.resolve()),
                patch.object(module, "setup_project", return_value=0) as setup,
                patch("sys.stdin", io.StringIO(payload)),
                patch.object(module.subprocess, "run", return_value=completed) as run,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(module.hook_prompt("CG"), 0)
            setup.assert_called_once_with(repo.resolve(), runner=ANY)
            self.assertEqual(run.call_args.kwargs["input"], payload)

    def test_hook_status_does_not_reinitialize_an_initialized_repo(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git").mkdir()
            (repo / ".codegraph").mkdir()
            (repo / ".code-review-graph").mkdir()
            with patch.object(module, "git_root", return_value=repo), patch.object(module, "setup_project") as setup:
                with patch("sys.stdin", io.StringIO(json.dumps({"cwd": str(repo)}))):
                    self.assertEqual(module.hook_status(), 0)
                setup.assert_not_called()

    def test_hook_status_reports_failed_repo_initialization(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git").mkdir()
            (repo / ".code-review-graph").mkdir()
            stdout = io.StringIO()
            with (
                patch.object(module, "git_root", return_value=repo.resolve()),
                patch.object(module, "setup_project", return_value=1) as setup,
                patch("sys.stdin", io.StringIO(json.dumps({"cwd": str(repo)}))),
                redirect_stdout(stdout),
            ):
                self.assertEqual(module.hook_status(), 0)
            setup.assert_called_once_with(repo.resolve(), runner=ANY)
            message = stdout.getvalue()
            self.assertIn("automatic initialization failed", message)
            self.assertIn("CodeGraph", message)
            self.assertIn(str(repo.resolve()), message)

    def test_hook_status_reports_repo_initialized_only_when_both_indexes_exist(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git").mkdir()
            (repo / ".codegraph").mkdir()
            (repo / ".code-review-graph").mkdir()
            stdout = io.StringIO()
            with (
                patch.object(module, "git_root", return_value=repo.resolve()),
                patch("sys.stdin", io.StringIO(json.dumps({"cwd": str(repo)}))),
                redirect_stdout(stdout),
            ):
                self.assertEqual(module.hook_status(), 0)
            self.assertEqual(
                stdout.getvalue(),
                f"Code intelligence: initialized — {repo.resolve()}\n",
            )

    def test_hook_status_requests_umbrella_initialization_for_nested_gap(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            umbrella = Path(directory)
            (umbrella / "AGENTS.md").write_text("umbrella\n")
            (umbrella / ".codegraph").mkdir()
            complete = umbrella / "complete"
            incomplete = umbrella / "incomplete"
            for repo in (complete, incomplete):
                (repo / ".git").mkdir(parents=True)
                (repo / ".codegraph").mkdir()
            (complete / ".code-review-graph").mkdir()
            stdout = io.StringIO()
            with (
                patch("sys.stdin", io.StringIO(json.dumps({"cwd": str(umbrella)}))),
                redirect_stdout(stdout),
            ):
                self.assertEqual(module.hook_status(), 0)
            message = stdout.getvalue()
            self.assertIn("umbrella", message)
            self.assertIn(f"{incomplete.resolve()}: CRG", message)
            self.assertIn("eligible nested Git repositories", message)
            self.assertIn("Ask the user whether to initialize", message)

    def test_project_status_is_read_only_for_repo_and_umbrella(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / ".codegraph").mkdir()
            (repo / ".code-review-graph").mkdir()
            with patch.object(module.subprocess, "run") as run, redirect_stdout(io.StringIO()):
                self.assertEqual(module.project_status(repo), 0)
                run.assert_not_called()

            umbrella = base / "umbrella"
            (umbrella / ".codegraph").mkdir(parents=True)
            nested = umbrella / "nested"
            (nested / ".git").mkdir(parents=True)
            (nested / ".codegraph").mkdir()
            (nested / ".code-review-graph").mkdir()
            with patch.object(module.subprocess, "run") as run, redirect_stdout(io.StringIO()):
                self.assertEqual(module.project_status(umbrella), 0)
                run.assert_not_called()

    def test_batch_keeps_exact_repo_then_umbrella_sequence(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            umbrella = base / "workspace"
            umbrella.mkdir()
            (umbrella / "AGENTS.md").write_text("workspace\n")
            repos = [umbrella / "api", umbrella / "web"]
            for repo in repos:
                (repo / ".git" / "info").mkdir(parents=True)
                for index in range(5):
                    (repo / f"file{index}.py").write_text("pass\n")
            calls = []
            self.assertEqual(module.setup_batch(base, lambda command: calls.append(command) or 0, "CG", "CRG"), 0)
            expected = []
            for repo in repos:
                resolved = str(repo.resolve())
                expected.extend((["CG", "init", resolved], ["CRG", "build", "--repo", resolved], ["CRG", "register", resolved, "--alias", repo.name]))
            expected.append(["CG", "init", str(umbrella.resolve())])
            self.assertEqual(calls, expected)

    def test_prompt_hook_wraps_codegraph_context_in_shared_json_contract(self):
        module = load_module()
        completed = module.subprocess.CompletedProcess(
            ["CG", "prompt-hook"], 0, stdout="<codegraph_context>ctx</codegraph_context>\n", stderr=""
        )
        stdout = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO('{"prompt":"find callers","cwd":"/repo"}')),
            patch.object(module, "git_root", return_value=None),
            patch.object(module.subprocess, "run", return_value=completed) as run,
            redirect_stdout(stdout),
        ):
            self.assertEqual(module.hook_prompt("CG"), 0)
        run.assert_called_once()
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "<codegraph_context>ctx</codegraph_context>\n",
                }
            },
        )

    def test_prompt_hook_fails_open_when_codegraph_fails(self):
        module = load_module()
        completed = module.subprocess.CompletedProcess(
            ["CG", "prompt-hook"], 1, stdout="", stderr="failure"
        )
        stdout = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO("{}")),
            patch.object(module, "git_root", return_value=None),
            patch.object(module.subprocess, "run", return_value=completed),
            redirect_stdout(stdout),
        ):
            self.assertEqual(module.hook_prompt("CG"), 0)
        self.assertEqual(stdout.getvalue(), "")



if __name__ == "__main__":
    unittest.main()
