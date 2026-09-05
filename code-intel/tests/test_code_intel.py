import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
PACKAGE = Path(__file__).resolve().parents[1]


def load_controller():
    spec = importlib.util.spec_from_file_location(
        "code_intel_under_test", PACKAGE / "scripts/code_intel.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()


def snapshot(root):
    return {
        p.relative_to(root).as_posix():
        ("link", os.readlink(p)) if p.is_symlink() else
        ("file", p.read_bytes()) if p.is_file() else ("dir", None)
        for p in root.rglob("*")
    }


class ControllerCase(unittest.TestCase):
    def setUp(self):
        self.assertTrue((PACKAGE / "scripts/code_intel.py").is_file(),
                        "controller has not been implemented")
        self.module = load_controller()
        temp = tempfile.TemporaryDirectory(prefix="code intel ; ")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "source.py").write_text("value = 1\n")
        git(self.repo, "add", "source.py")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "initial")
        self.data = self.base / "data"
        self.data.mkdir()
        env = patch.dict(os.environ, {"PLUGIN_DATA": str(self.data),
            "CLAUDE_PLUGIN_DATA": "", "PYTHONDONTWRITEBYTECODE": "1"})
        env.start()
        self.addCleanup(env.stop)

    def executable(self, directory, name, body):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o755)
        return path


class ToolContractTests(ControllerCase):
    def test_exact_versions_use_path_before_mise(self):
        module = self.module
        for engine, output in (("codegraph", "1.6.0\n"),
                               ("codegraph", "codegraph 1.6.0\n"),
                               ("crg", "code-review-graph 2.3.8\n"),
                               ("crg", "2.3.8\n")):
            with self.subTest(engine=engine, output=output):
                with patch.object(module.shutil, "which", return_value="/tools/tool;$x") as which, patch.object(
                    module, "run_child", return_value=subprocess.CompletedProcess([], 0, output, "")
                ) as child:
                    actual = module.resolve_verified_tool(module.TOOLS[engine], deadline=time.monotonic() + 5)
                self.assertEqual(actual, Path("/tools/tool;$x"))
                which.assert_called_once_with(module.TOOLS[engine].executable)
                self.assertEqual(child.call_args.args[0], ["/tools/tool;$x", "--version"])
                self.assertIsNone(child.call_args.kwargs["cwd"])
                self.assertGreater(child.call_args.kwargs["timeout"], 0)
                self.assertLessEqual(child.call_args.kwargs["timeout"], 5)

    def test_standard_mise_shim_fallback_retains_executable_name(self):
        module = self.module
        shim_dir = self.base / ".local/share/mise/shims"
        target = self.executable(self.base, "mise-real", "print('1.6.0')\n")
        shim_dir.mkdir(parents=True)
        shim = shim_dir / "codegraph"
        shim.symlink_to(target)
        with patch.object(module.Path, "home", return_value=self.base), patch.dict(os.environ, {"PATH": ""}):
            actual = module.resolve_verified_tool(module.TOOLS["codegraph"], deadline=time.monotonic() + 5)
        self.assertEqual(actual, shim)

    def test_wrong_and_unparseable_versions_rejected_without_fallback(self):
        module = self.module
        cases = (("codegraph", "1.6.1"), ("crg", "code-review-graph 2.3.9"),
                 ("codegraph", "1.6.0-beta"), ("codegraph", "node 1.6.0"),
                 ("crg", "warning\ncode-review-graph 2.3.8"), ("crg", ""))
        for engine, output in cases:
            with self.subTest(output=output), patch.object(module.shutil, "which", return_value="/tools/found") as which, patch.object(
                module, "run_child", return_value=subprocess.CompletedProcess([], 0, output, "")
            ):
                with self.assertRaises(module.UserError):
                    module.resolve_verified_tool(module.TOOLS[engine], deadline=time.monotonic() + 5)
                which.assert_called_once_with(module.TOOLS[engine].executable)

    def test_missing_binary_and_expired_deadline_do_not_launch_children(self):
        module = self.module
        for found, deadline in ((None, time.monotonic() + 5), ("/tool", time.monotonic() - 1)):
            with self.subTest(found=found), patch.object(module.shutil, "which", return_value=found), patch.object(module, "run_child") as child:
                with self.assertRaises(module.UserError):
                    module.resolve_verified_tool(module.TOOLS["codegraph"], deadline=deadline)
                child.assert_not_called()

    def test_install_tools_uses_exact_pins(self):
        module = self.module
        with patch.object(module, "run_child", return_value=subprocess.CompletedProcess([], 0, "", "")) as child, patch.object(module, "resolve_verified_tool", return_value=Path("/tools/verified")) as verify, patch.object(module.shutil, "which", return_value="/tools/mise"):
            rc = module.main(["install-tools"])
        self.assertEqual([call.args[0] for call in child.call_args_list], [
            ["/tools/mise", "use", "-g", "npm:@colbymchenry/codegraph@1.6.0"],
            ["/tools/mise", "use", "-g", "pipx:code-review-graph@2.3.8"]])
        self.assertEqual([call.args[0].name for call in verify.call_args_list], ["codegraph", "crg"])
        self.assertTrue(all(call.kwargs == {"cwd": None, "timeout": 300} for call in child.call_args_list))
        self.assertEqual(rc, 0)

    def test_missing_mise_reports_one_actionable_error_on_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(self.module.shutil, "which", return_value=None), patch.object(self.module, "run_child") as child, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.module.main(["install-tools"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertIn("mise", err.getvalue())
        self.assertIn("install-tools", err.getvalue())
        child.assert_not_called()

    def test_install_verification_failure_is_nonzero(self):
        with patch.object(self.module.shutil, "which", return_value="/mise"), patch.object(self.module, "run_child"), patch.object(self.module, "resolve_verified_tool", side_effect=self.module.UserError("wrong version")), contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(self.module.main(["install-tools"]), 0)

    def test_serve_preserves_cwd_and_never_uses_shell(self):
        module = self.module
        original = Path.cwd()
        for engine, args in (("codegraph", ["serve", "--mcp"]), ("crg", ["serve"])):
            with self.subTest(engine=engine), patch.object(module, "resolve_verified_tool", return_value=Path("/tools/code graph;$x")), patch.object(module.os, "execv") as execute:
                module.main(["serve", engine])
            execute.assert_called_once_with("/tools/code graph;$x", ["/tools/code graph;$x", *args])
            self.assertEqual(Path.cwd(), original)

    def test_installed_hostile_path_preserves_protocol_stdout_and_cwd(self):
        installed = self.base / "plugin space;$(touch UNEXPECTED)$x" / "scripts"
        installed.mkdir(parents=True)
        shutil.copy2(PACKAGE / "scripts/code_intel.py", installed / "code_intel.py")
        tools = self.base / "bin space;$x"
        for engine, executable, version, expected in (
            ("codegraph", "codegraph", "1.6.0", ["serve", "--mcp"]),
            ("crg", "code-review-graph", "2.3.8", ["serve"]),
        ):
            self.executable(tools, executable,
                "import json, os, sys\n"
                f"if sys.argv[1:] == ['--version']: print({version!r})\n"
                "else:\n"
                " print(json.dumps({'cwd': os.getcwd(), 'args': sys.argv[1:]}))\n"
                " print('server diagnostic', file=sys.stderr)\n")
            result = subprocess.run(
                [sys.executable, "-B", str(installed / "code_intel.py"), "serve", engine],
                cwd=self.repo, env={**os.environ, "PATH": str(tools)},
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"cwd": str(self.repo.resolve()), "args": expected})
            self.assertEqual(result.stderr, "server diagnostic\n")
        self.assertFalse((self.repo / "UNEXPECTED").exists())

    def test_version_and_exec_errors_keep_stdout_empty(self):
        for version in ("1.6.1", "not a version"):
            tools = self.base / "bad tools"
            self.executable(tools, "codegraph", f"print({version!r})\n")
            result = subprocess.run(
                [sys.executable, "-B", str(PACKAGE / "scripts/code_intel.py"), "serve", "codegraph"],
                env={**os.environ, "PATH": str(tools)}, cwd=self.repo,
                capture_output=True, text=True, timeout=5,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(len(result.stderr.splitlines()), 1)
            self.assertIn("install-tools", result.stderr)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(self.module, "resolve_verified_tool", return_value=Path("/missing")), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertNotEqual(self.module.main(["serve", "crg"]), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(err.getvalue().splitlines()), 1)

    def test_child_captures_output_cwd_and_literal_arguments(self):
        payload = "literal;$(touch UNEXPECTED) $HOME `touch ALSO_UNEXPECTED`"
        result = self.module.run_child(
            [sys.executable, "-c", "import json, os, sys; print(json.dumps([os.getcwd(), sys.argv[1]])); print('err', file=sys.stderr)", payload],
            cwd=self.repo, timeout=5,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), [str(self.repo.resolve()), payload])
        self.assertEqual(result.stderr, "err\n")
        self.assertFalse((self.repo / "UNEXPECTED").exists())

    def test_child_rejects_shell_strings_nonzero_and_spawn_failure(self):
        module = self.module
        for argv in ("echo bad", [], ["/missing-code-intel-tool"],
                     [sys.executable, "-c", "import sys; print('bad'); sys.exit(7)"]):
            with self.subTest(argv=argv), self.assertRaises(module.UserError):
                module.run_child(argv, cwd=None, timeout=5)

    def test_child_refuses_expired_timeout_before_spawning(self):
        marker = self.base / "started"
        with self.assertRaises(self.module.UserError):
            self.module.run_child([sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)], cwd=None, timeout=0)
        self.assertFalse(marker.exists())

    def assert_process_exited(self, pid):
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.stderr, "", f"cannot inspect writer {pid}: {result.stderr}")
        self.assertIn(result.returncode, (0, 1), f"ps inspection failed: {result.returncode}")
        absent = result.returncode == 1 and not result.stdout.strip()
        zombie = result.returncode == 0 and result.stdout.strip().startswith("Z")
        self.assertTrue(absent or zombie,
                        f"writer {pid} still running: {result.stdout}")

    def test_linux_group_inspection_handles_hidden_processes_conservatively(self):
        proc = self.base / "proc"
        (proc / "42001").mkdir(parents=True)
        (proc / "42002").mkdir()
        (proc / "42002/stat").write_text("42002 (writer) Z 1 42000 42000")
        read_text = Path.read_text

        def read_stat(path, *args, **kwargs):
            if path == proc / "42001/stat":
                raise PermissionError("hidden by hidepid=1")
            return read_text(path, *args, **kwargs)

        for membership, running in (
            (90000, False),  # An inaccessible unrelated process is harmless.
            (42000, True),  # An inaccessible target is not proof of exit.
            (PermissionError("cannot inspect group"), True),
            (ProcessLookupError("process exited"), False),
        ):
            with self.subTest(membership=membership), patch.object(
                self.module.sys, "platform", "linux"
            ), patch.object(self.module.os, "killpg"), patch.object(
                self.module, "Path", return_value=proc
            ), patch.object(Path, "read_text", read_stat), patch.object(
                self.module.os, "getpgid",
                side_effect=membership if isinstance(membership, Exception) else None,
                return_value=membership,
            ):
                self.assertEqual(self.module._group_running(42000), running)

    def test_process_exit_assertion_rejects_inspection_errors(self):
        for rc, out, err in ((1, "", "ps: operation not permitted"),
                             (1, "", "ps: unsupported option"),
                             (2, "", ""), (-9, "", ""), (0, "", ""),
                             (0, "S\n", "")):
            with self.subTest(rc=rc, out=out, err=err), patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], rc, out, err)
            ):
                with self.assertRaises(AssertionError):
                    self.assert_process_exited(42000)
        for rc, out in ((1, ""), (0, "Z\n")):
            with self.subTest(rc=rc, out=out), patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], rc, out, "")
            ):
                self.assert_process_exited(42000)

    def check_descendant_cleanup(self, parent_exits):
        pid_file = self.base / "writer.pid"
        output = self.base / "index"
        writer = (
            "import os, sys, time\nfrom pathlib import Path\n"
            "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
            "while True:\n Path(sys.argv[2]).write_text(str(time.monotonic_ns()))\n time.sleep(.01)\n"
        )
        parent = (
            "import subprocess, sys, time\nfrom pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', sys.argv[1], *sys.argv[2:]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "while not Path(sys.argv[2]).exists(): time.sleep(.005)\n"
            + ("print('done')\n" if parent_exits else "time.sleep(30)\n")
        )
        started = time.monotonic()
        try:
            if parent_exits:
                result = self.module.run_child([sys.executable, "-c", parent, writer, str(pid_file), str(output)], cwd=self.repo, timeout=3)
                self.assertEqual(result.stdout, "done\n")
            else:
                with self.assertRaisesRegex(self.module.UserError, "timed out"):
                    self.module.run_child([sys.executable, "-c", parent, writer, str(pid_file), str(output)], cwd=self.repo, timeout=.5)
            self.assertLess(time.monotonic() - started, 4)
            self.assertTrue(pid_file.exists(), "writer did not start before deadline")
            self.assert_process_exited(int(pid_file.read_text()))
            before = output.read_bytes()
            time.sleep(.05)
            self.assertEqual(output.read_bytes(), before)
        finally:
            if pid_file.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)

    @unittest.skipUnless(os.name == "posix", "POSIX process supervision")
    def test_timeout_kills_and_reaps_writer_descendants(self):
        self.check_descendant_cleanup(parent_exits=False)

    @unittest.skipUnless(os.name == "posix", "POSIX process supervision")
    def test_successful_parent_exit_leaves_no_writer_descendants(self):
        self.check_descendant_cleanup(parent_exits=True)


if __name__ == "__main__":
    unittest.main()
