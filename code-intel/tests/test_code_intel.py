import contextlib
import dataclasses
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import sqlite3
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


class StateTests(ControllerCase):
    def api(self):
        self.assertTrue(hasattr(self.module, "select_data_location"), "state storage is absent")
        return self.module

    def marker(self, status="pending"):
        return self.module.FreshnessMarker(str(self.repo.resolve()), "", {}, "", {}, status)

    def successful_value(self):
        return {
            "root": str(self.repo.resolve()), "head": git(self.repo, "rev-parse", "HEAD"),
            "versions": {"codegraph": "1.6.0", "crg": "2.3.8"},
            "checkout_fingerprint": "a" * 64,
            "index_fingerprints": {"codegraph": "b" * 64, "crg": "c" * 64},
            "status": "success", "schema_version": 2, "crg_candidates": [],
        }

    def test_v2_candidates_roundtrip_and_codegraph_only_null(self):
        m = self.api()
        data = m.select_data_location(os.environ, read_only=False)
        value = self.successful_value()
        value["crg_candidates"] = ["new\nname.py", "raw-\udcff.py", "tab\tname.py"]
        state = m.state_path(self.repo, data)
        state.write_text(json.dumps(value))
        try:
            marker = m.read_marker(self.repo, data)
        except m.CorruptState as exc:
            self.fail(f"valid v2 candidate history was rejected: {exc}")
        self.assertEqual(marker.schema_version, 2)
        self.assertEqual(marker.crg_candidates, ["new\nname.py", "raw-\udcff.py", "tab\tname.py"])
        m.write_marker(self.repo, data, marker)
        self.assertEqual(json.loads(state.read_text()), value)
        value.update(head="", versions={"codegraph": "1.6.0"},
                     index_fingerprints={"codegraph": "b" * 64}, crg_candidates=None)
        state.write_text(json.dumps(value))
        self.assertIsNone(m.read_marker(self.repo, data).crg_candidates)

    def test_invalid_v2_history_is_corrupt_and_preserved(self):
        m = self.api()
        data = m.select_data_location(os.environ, read_only=False)
        state = m.state_path(self.repo, data)
        changes = [
            {"schema_version": 3}, {"schema_version": True},
            {"crg_candidates": ["a.py", "a.py"]}, {"crg_candidates": ["b.py", "a.py"]},
            {"crg_candidates": [1]}, {"crg_candidates": None}, {"crg_candidates": "a.py"},
            {"crg_candidates": [""]}, {"crg_candidates": ["/a.py"]},
            {"crg_candidates": ["../a.py"]}, {"crg_candidates": ["a/./b.py"]},
            {"crg_candidates": ["a\0b.py"]}, {"unexpected": "field"},
            {"root": str(self.base)}, {"root": "relative"}, {"head": "--stat"}, {"head": ""},
        ]
        for change in changes:
            with self.subTest(change=change):
                state.write_text(json.dumps({**self.successful_value(), **change}))
                before = snapshot(self.base)
                with self.assertRaises(m.CorruptState):
                    m.read_marker(self.repo, data)
                self.assertEqual(snapshot(self.base), before)

    def test_valid_legacy_success_is_stale_without_rewriting(self):
        m = self.api()
        directory = self.base / "bin"
        tools = {}
        for name, executable, version, index in (
                ("codegraph", "codegraph", "1.6.0", ".codegraph"),
                ("crg", "code-review-graph", "2.3.8", ".code-review-graph")):
            tools[name] = self.executable(directory, executable, f"print({version!r})\n")
            (self.repo / index).mkdir()
        observed = m.capture(self.repo, tools, time.monotonic() + 10)
        legacy = {name: getattr(observed, name) for name in (
            "root", "head", "versions", "checkout_fingerprint", "index_fingerprints", "status")}
        data = m.select_data_location(os.environ, read_only=False)
        m.state_path(self.repo, data).write_text(json.dumps(legacy))
        before = snapshot(self.base)
        with patch.dict(os.environ, {"PATH": str(directory) + os.pathsep + os.environ["PATH"]}):
            report = m.observe_project(self.repo, deadline=time.monotonic() + 10)
        self.assertFalse(report["healthy"], "legacy history cannot prove rollback readiness")
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(m.read_marker(self.repo, data).schema_version, 1)

    def test_first_nonempty_variable_never_falls_back(self):
        m = self.api()
        occupied = self.base / "occupied"
        occupied.write_text("file")
        for read_only in (True, False):
            with self.subTest(read_only=read_only), self.assertRaises(m.UnusableDataLocation):
                m.select_data_location({"PLUGIN_DATA": str(occupied), "CLAUDE_PLUGIN_DATA": str(self.data)}, read_only=read_only)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_absent_environment_is_unusable_and_empty_primary_uses_secondary(self):
        m = self.api()
        with self.assertRaises(m.UnusableDataLocation):
            m.select_data_location({}, read_only=False)
        data = m.select_data_location({"PLUGIN_DATA": "", "CLAUDE_PLUGIN_DATA": str(self.data)}, read_only=True)
        self.assertEqual((data.source, data.path), ("CLAUDE_PLUGIN_DATA", self.data.resolve()))
        preferred = m.select_data_location({"PLUGIN_DATA": str(self.data), "CLAUDE_PLUGIN_DATA": str(self.base / "other")}, read_only=False)
        self.assertEqual(preferred.source, "PLUGIN_DATA")

    def test_read_only_selection_does_not_create_missing_storage(self):
        m = self.api()
        path = self.base / "absent" / "data"
        before = snapshot(self.base)
        data = m.select_data_location({"PLUGIN_DATA": str(path)}, read_only=True)
        self.assertIsNone(m.read_marker(self.repo, data))
        self.assertEqual(snapshot(self.base), before)
        m.select_data_location({"PLUGIN_DATA": str(path)}, read_only=False)
        self.assertTrue(path.is_dir())

    def test_unwritable_storage_is_not_accepted(self):
        m = self.api()
        self.data.chmod(0o500)
        self.addCleanup(self.data.chmod, 0o700)
        with self.assertRaises(m.UnusableDataLocation):
            m.select_data_location(os.environ, read_only=False)

    def test_worktrees_have_independent_canonical_digest_keys(self):
        m = self.api()
        linked = self.base / "linked"
        git(self.repo, "worktree", "add", "-qb", "other", str(linked))
        data = m.select_data_location(os.environ, read_only=True)
        key = hashlib.sha256(os.fsencode(str(self.repo.resolve()))).hexdigest()
        self.assertEqual(m.state_path(self.repo, data), self.data.resolve() / (key + ".json"))
        self.assertNotEqual(m.state_path(self.repo, data), m.state_path(linked, data))
        alias = self.base / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        self.assertEqual(m.state_path(alias, data), m.state_path(self.repo, data))

    def test_marker_roundtrip_and_atomic_replacement_preserve_open_reader(self):
        m = self.api()
        data = m.select_data_location(os.environ, read_only=False)
        first = self.marker()
        m.write_marker(self.repo, data, first)
        with m.state_path(self.repo, data).open() as reader:
            m.write_marker(self.repo, data, dataclasses.replace(first, status="failed"))
            self.assertEqual(json.load(reader)["status"], "pending")
        self.assertEqual(m.read_marker(self.repo, data).status, "failed")
        self.assertEqual(len(list(self.data.iterdir())), 1)

    def test_corrupt_schema_root_and_symlink_state_are_rejected(self):
        m = self.api()
        data = m.select_data_location(os.environ, read_only=False)
        state = m.state_path(self.repo, data)
        malformed = ["not json", "{}", "[]", json.dumps({**dataclasses.asdict(self.marker()), "root": str(self.base)}), json.dumps({**dataclasses.asdict(self.marker()), "versions": []})]
        for content in malformed:
            state.write_text(content)
            with self.subTest(content=content), self.assertRaises(m.CorruptState):
                m.read_marker(self.repo, data)
        state.unlink()
        target = self.base / "target"
        target.write_text(json.dumps(dataclasses.asdict(self.marker())))
        state.symlink_to(target)
        with self.assertRaises(m.CorruptState):
            m.read_marker(self.repo, data)

    def test_failed_replace_keeps_previous_state_and_removes_temporary_file(self):
        m = self.api()
        data = m.select_data_location(os.environ, read_only=False)
        m.write_marker(self.repo, data, self.marker())
        before = snapshot(self.data)
        with patch.object(m.os, "replace", side_effect=OSError("cannot replace")), self.assertRaises(m.UserError):
            m.write_marker(self.repo, data, self.marker("failed"))
        self.assertEqual(snapshot(self.data), before)

    def test_incomplete_success_marker_is_corrupt_and_cannot_be_repaired(self):
        m = self.api()
        data = m.select_data_location(os.environ, read_only=False)
        m.state_path(self.repo, data).write_text(json.dumps(dataclasses.asdict(self.marker("success"))))
        with self.assertRaises(m.CorruptState):
            m.read_marker(self.repo, data)

    def test_marker_parser_recursion_is_reported_as_corrupt_state(self):
        m = self.api()
        data = m.select_data_location(os.environ, read_only=False)
        m.state_path(self.repo, data).write_text("[" * 2000 + "]" * 2000)
        with patch.object(m.json, "load", side_effect=RecursionError("JSON nesting is too deep")):
            try:
                with self.assertRaises(m.CorruptState):
                    m.read_marker(self.repo, data)
            except RecursionError:
                self.fail("Malformed marker parser recursion escaped CorruptState handling")


class FingerprintTests(ControllerCase):
    def api(self):
        self.assertTrue(hasattr(self.module, "checkout_fingerprint"), "content capture is absent")
        return self.module

    def checkout(self):
        return self.module.checkout_fingerprint(self.repo, time.monotonic() + 10)

    def test_tracked_untracked_edits_and_deletions_change_content_digest(self):
        self.api()
        source = self.repo / "source.py"
        first = self.checkout()
        source.write_text("value = 2\n")
        edited = self.checkout()
        self.assertNotEqual(first, edited)
        source.unlink()
        deleted = self.checkout()
        self.assertNotEqual(edited, deleted)
        self.assertEqual(deleted, self.checkout())
        source.write_text("value = 1\n")
        self.assertEqual(first, self.checkout())
        extra = self.repo / "new\nfile.py"
        extra.write_text("new")
        added = self.checkout()
        self.assertNotEqual(first, added)
        extra.write_text("edit")
        self.assertNotEqual(added, self.checkout())
        extra.unlink()
        self.assertEqual(first, self.checkout())

    def test_git_ignored_files_and_indexes_are_excluded_but_tracked_config_is_not(self):
        self.api()
        (self.repo / ".gitignore").write_text("ignored/\n")
        first = self.checkout()
        for name in ("ignored", ".codegraph", ".code-review-graph"):
            (self.repo / name).mkdir()
            (self.repo / name / "data").write_text("noise")
        self.assertEqual(first, self.checkout())
        (self.repo / "codegraph.config.json").write_text('{"languages": ["python"]}')
        self.assertNotEqual(first, self.checkout())

    def test_raw_non_utf8_paths_remain_distinct(self):
        self.api()
        path = self.repo / os.fsdecode(b"file-\xff")
        try:
            path.write_bytes(b"one")
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EILSEQ):
                raise
            self.skipTest("filesystem does not permit non-UTF-8 filenames")
        before = self.checkout()
        path.rename(self.repo / os.fsdecode(b"file-\xfe"))
        self.assertNotEqual(before, self.checkout())

    def test_git_carriage_return_paths_are_not_normalized_by_child_output(self):
        self.api()
        path = self.repo / "carriage\rreturn"
        path.write_text("one")
        before = self.checkout()
        path.write_text("two")
        self.assertNotEqual(before, self.checkout())

    def test_child_preserves_raw_git_path_bytes(self):
        result = self.module.run_child([sys.executable, "-c", "import os; os.write(1, b'file\\r\\n-\\xff\\x00')"], cwd=self.repo, timeout=5)
        self.assertEqual(os.fsencode(result.stdout), b"file\r\n-\xff\0")

    def test_git_path_bytes_are_independent_of_filesystem_encoding(self):
        m = self.api()
        output = subprocess.CompletedProcess([], 0, "caf\u00e9.py\0raw-\udcff.py\0", "")
        def ascii_fsencode(path):
            value = os.fspath(path)
            return value if isinstance(value, bytes) else value.encode("ascii", "surrogateescape")
        with patch.object(m, "run_child", return_value=output), patch.object(m.os, "fsencode", side_effect=ascii_fsencode):
            try:
                paths = m._git_paths(self.repo, time.monotonic() + 10)
            except UnicodeEncodeError:
                self.fail("Git output was reconstructed through the ASCII filesystem encoding")
        self.assertEqual(paths, [b"caf\xc3\xa9.py", b"raw-\xff.py"])

    def test_symlink_targets_are_hashed_without_traversal(self):
        self.api()
        external = self.base / "external"
        external.mkdir()
        (external / "data").write_text("one")
        link = self.repo / "link"
        link.symlink_to(external, target_is_directory=True)
        before = self.checkout()
        (external / "data").write_text("two")
        self.assertEqual(before, self.checkout())
        link.unlink()
        link.symlink_to(self.base / "missing")
        self.assertNotEqual(before, self.checkout())

    def test_tracked_parent_replaced_by_symlink_is_not_followed(self):
        m = self.api()
        folder = self.repo / "nested"
        folder.mkdir()
        (folder / "file").write_text("inside")
        git(self.repo, "add", "nested/file")
        (folder / "file").unlink()
        folder.rmdir()
        external = self.base / "external"
        external.mkdir()
        (external / "file").write_text("outside")
        folder.symlink_to(external, target_is_directory=True)
        with self.assertRaises(m.UserError):
            self.checkout()

    def test_deadline_unreadable_and_special_files_fail_capture(self):
        m = self.api()
        with self.assertRaises(m.UserError):
            m.checkout_fingerprint(self.repo, time.monotonic() - 1)
        source = self.repo / "source.py"
        source.chmod(0)
        with self.assertRaises(m.UserError):
            self.checkout()
        source.chmod(0o600)
        source.unlink()
        os.mkfifo(source)
        with self.assertRaises(m.UserError):
            self.checkout()

    def test_mutation_during_checkout_read_fails_without_retry(self):
        m = self.api()
        read = os.read
        changed = False
        def mutate(fd, size):
            nonlocal changed
            content = read(fd, size)
            if not changed and os.fstat(fd).st_ino == (self.repo / "source.py").stat().st_ino:
                changed = True
                (self.repo / "source.py").write_text("value = 2\n")
            return content
        with patch.object(m.os, "read", side_effect=mutate), self.assertRaises(m.UserError):
            self.checkout()

    def test_input_added_between_git_enumerations_invalidates_capture(self):
        m = self.api()
        runner = m.run_child
        calls = 0
        def mutate(argv, **kwargs):
            nonlocal calls
            if "ls-files" in argv:
                calls += 1
                if calls == 2:
                    (self.repo / "appeared").write_text("new")
            return runner(argv, **kwargs)
        with patch.object(m, "run_child", side_effect=mutate), self.assertRaises(m.UserError):
            self.checkout()
        self.assertEqual(calls, 2, "mutation must fail this attempt, not restart")

    def test_head_changes_during_checkout_capture_are_rejected(self):
        m = self.api()
        runner = m.run_child
        def mutate(argv, **kwargs):
            result = runner(argv, **kwargs)
            if "ls-files" in argv:
                git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "changed")
            return result
        with patch.object(m, "run_child", side_effect=mutate), self.assertRaises(m.UserError):
            m.capture_checkout(self.repo, time.monotonic() + 10)

    def test_index_content_config_and_journals_are_included_transients_excluded(self):
        m = self.api()
        for engine, name in (("codegraph", ".codegraph"), ("crg", ".code-review-graph")):
            directory = self.repo / name
            directory.mkdir()
            (directory / "graph.db").write_text("db")
            capture = lambda: m.index_fingerprint(self.repo, engine, time.monotonic() + 10)
            for persistent in ("graph.db-wal", "graph.db-journal", "custom.wal", "custom.journal", "config.json", "dependencies.lock"):
                before = capture()
                (directory / persistent).write_text("persistent")
                self.assertNotEqual(before, capture(), persistent)
            before = capture()
            transient = ("daemon.pid", "codegraph.lock", "daemon.sock") if engine == "codegraph" else ()
            for filename in transient:
                (directory / filename).write_text("transient")
            self.assertEqual(before, capture())

    def test_index_mutation_during_read_is_rejected(self):
        m = self.api()
        directory = self.repo / ".codegraph"
        directory.mkdir()
        database = directory / "graph.db"
        database.write_text("one")
        read = os.read
        def mutate(fd, size):
            value = read(fd, size)
            database.write_text("two")
            return value
        with patch.object(m.os, "read", side_effect=mutate), self.assertRaises(m.UserError):
            m.index_fingerprint(self.repo, "codegraph", time.monotonic() + 10)

    def test_missing_and_symlink_index_roots_are_rejected(self):
        m = self.api()
        with self.assertRaises(m.UserError):
            m.index_fingerprint(self.repo, "crg", time.monotonic() + 10)
        (self.repo / ".code-review-graph").symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(m.UserError):
            m.index_fingerprint(self.repo, "crg", time.monotonic() + 10)

    def test_directory_enumeration_checks_deadline_before_consuming_all_entries(self):
        m = self.api()
        directory = self.repo / ".codegraph"
        directory.mkdir()
        for number in range(20):
            (directory / str(number)).write_text("input")
        scandir = os.scandir
        now = 0
        consumed = 0
        @contextlib.contextmanager
        def delayed_entries(fd):
            def iterator(entries):
                nonlocal now, consumed
                for entry in entries:
                    now += 1
                    consumed += 1
                    yield entry
            with scandir(fd) as entries:
                yield iterator(entries)
        with patch.object(m.os, "scandir", delayed_entries), patch.object(m.time, "monotonic", side_effect=lambda: now), self.assertRaises(m.UserError):
            m.index_fingerprint(self.repo, "codegraph", 3)
        self.assertLess(consumed, 20, "enumeration ignored its deadline until after reading everything")

    def test_umbrella_content_has_empty_head_and_excludes_nested_administration(self):
        m = self.api()
        umbrella = self.base / "umbrella"
        child = umbrella / "child"
        child.mkdir(parents=True)
        git(child, "init", "-q")
        (child / "source").write_text("one")
        before = m.capture_checkout(umbrella, time.monotonic() + 10)
        self.assertEqual(before[0], "")
        (child / ".git" / "noise").write_text("ignored")
        (child / ".codegraph").mkdir()
        (child / ".codegraph" / "db").write_text("ignored")
        self.assertEqual(before, m.capture_checkout(umbrella, time.monotonic() + 10))
        (child / "source").write_text("two")
        self.assertNotEqual(before, m.capture_checkout(umbrella, time.monotonic() + 10))


class CRGCandidateTests(ControllerCase):
    def candidates(self, base=None):
        self.assertTrue(hasattr(self.module, "crg_candidate_paths"), "Git candidate history is absent")
        return self.module.crg_candidate_paths(
            self.repo, base or git(self.repo, "rev-parse", "HEAD"), time.monotonic() + 10)

    def test_candidates_keep_deleted_and_both_rename_paths(self):
        (self.repo / "old name.py").write_text("def keep():\n    return 1\n")
        git(self.repo, "add", "old name.py")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "rename fixture")
        head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "mv", "old name.py", "new\nname.py")
        git(self.repo, "rm", "source.py")
        (self.repo / "untracked.py").write_text("value = 1\n")
        self.assertEqual(self.candidates(head), ["new\nname.py", "old name.py", "source.py"])

    def test_staging_existing_untracked_file_changes_candidates_without_content_change(self):
        (self.repo / "added.py").write_text("value = 2\n")
        before = self.module.capture_checkout(self.repo, time.monotonic() + 10)
        self.assertEqual(self.candidates(), [])
        git(self.repo, "add", "added.py")
        self.assertEqual(self.candidates(), ["added.py"])
        self.assertEqual(self.module.capture_checkout(self.repo, time.monotonic() + 10), before)

    def test_literal_nul_records_preserve_native_paths(self):
        head = git(self.repo, "rev-parse", "HEAD")
        vectors = [
            (b"C100\0old.py\0copy.py\0", ["copy.py", "old.py"]),
            (b"M\0tab\tname.py\0", ["tab\tname.py"]),
            (b"D\0gone.py\0", ["gone.py"]), (b"", []),
            (b"M\0raw-\xff.py\0", ["raw-\udcff.py"]),
            (b"M\0carriage\rname.py\0", ["carriage\rname.py"]),
        ]
        for raw, expected in vectors:
            with self.subTest(raw=raw), patch.object(self.module, "run_child",
                    return_value=subprocess.CompletedProcess([], 0, raw.decode("utf-8", "surrogateescape"), "")) as child:
                self.assertEqual(self.candidates(head), expected)
                self.assertEqual(child.call_args.args[0],
                    ["git", "--no-optional-locks", "diff", "--name-status", "-z", head, "--"])

    def test_malformed_diff_and_failed_discovery_never_mean_empty(self):
        head = git(self.repo, "rev-parse", "HEAD")
        for raw in (b"R100\0old.py\0", b"M\0a.py", b"M\0\0", b"M\0/a.py\0",
                    b"M\0../escape.py\0", b"M\0a/./b.py\0", b"Q\0a.py\0", b"R\0a.py\0"):
            with self.subTest(raw=raw), patch.object(self.module, "run_child",
                    return_value=subprocess.CompletedProcess([], 0, raw.decode(), "")):
                with self.assertRaises(self.module.UserError):
                    self.candidates(head)
        with patch.object(self.module, "run_child", side_effect=self.module.UserError("Git diff failed")):
            with self.assertRaisesRegex(self.module.UserError, "Git diff failed"):
                self.candidates(head)

    def test_invalid_base_is_rejected_before_git(self):
        for base in ("--stat", "HEAD", "a" * 39, "A" * 40):
            with self.subTest(base=base), patch.object(self.module, "run_child",
                    side_effect=AssertionError("invalid base reached child process")):
                with self.assertRaises(self.module.UserError):
                    self.candidates(base)


class ProjectCase(ControllerCase):
    def install_fake_tools(self):
        directory = self.base / "bin"
        for engine, name, version, index in (("codegraph", "codegraph", "1.6.0", ".codegraph"), ("crg", "code-review-graph", "2.3.8", ".code-review-graph")):
            self.executable(directory, name,
                "import json, os, sys, time\nfrom pathlib import Path\n"
                f"if sys.argv[1:] == ['--version']:\n print({version!r}); sys.exit(0)\n"
                "root = Path.cwd()\n"
                "if os.environ.get('TOOL_EVENTS'):\n"
                " with open(os.environ['TOOL_EVENTS'], 'a') as log: log.write(json.dumps([str(root), sys.argv[1:]]) + '\\n')\n"
                "if os.environ.get('TOOL_FAIL'): sys.exit(9)\n"
                "if os.environ.get('TOOL_EDIT'): (root / 'source.py').write_text('changed during indexing')\n"
                "if sys.argv[1] == 'sync' and os.environ.get('TOOL_BARRIER'):\n"
                " barrier = Path(os.environ['TOOL_BARRIER']); (barrier / root.name).touch()\n"
                " deadline = time.monotonic() + 5\n"
                " while len(list(barrier.iterdir())) < 2:\n"
                "  if time.monotonic() > deadline: sys.exit(8)\n"
                "  time.sleep(.02)\n"
                f"index = root / {index!r}\n"
                f"database = index / {'codegraph.db' if engine == 'codegraph' else 'graph.db'!r}\n"
                "if sys.argv[1] == 'init' and database.is_file(): sys.exit(0)\n"
                "if sys.argv[1] == 'index' and not database.is_file(): sys.exit('CodeGraph not initialized')\n"
                "index.mkdir(exist_ok=True)\n"
                "if sys.argv[1] == 'index': database.write_text('rebuilt\\n')\n"
                "with database.open('a') as db: db.write('indexed\\n')\n")
        env = patch.dict(os.environ, {"PATH": str(directory) + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)
        return {"codegraph": directory / "codegraph", "crg": directory / "code-review-graph"}

    def require_operations(self):
        self.assertTrue(hasattr(self.module, "mutate_project"), "locked public operations are absent")

    def setup_project(self, path=None):
        self.module.mutate_project(path or self.repo, operation="setup", force=False, deadline=time.monotonic() + 15)

    def cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, "-B", str(PACKAGE / "scripts/code_intel.py"), *map(str, args)], cwd=cwd or self.repo, capture_output=True, text=True, timeout=20)


class ConcurrencyTests(ProjectCase):
    def holder(self, root):
        source = (
            "import runpy, sys, time, os\nfrom pathlib import Path\nfrom dataclasses import replace\n"
            "m = runpy.run_path(sys.argv[1]); root = Path(sys.argv[2]); data = m['select_data_location'](os.environ, read_only=False)\n"
            "with m['root_lock'](root, data, time.monotonic() + 15):\n"
            " marker = m['read_marker'](root, data)\n"
            " if marker: m['write_marker'](root, data, replace(marker, status='pending'))\n"
            " print('locked', flush=True)\n"
            " sys.stdin.readline()\n"
            " if marker: m['write_marker'](root, data, marker)\n"
        )
        process = subprocess.Popen([sys.executable, "-B", "-c", source, str(PACKAGE / "scripts/code_intel.py"), str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        self.assertEqual(process.stdout.readline(), "locked\n")
        return process

    def test_lock_deadline_leaves_holders_marker_untouched_and_exit_releases(self):
        self.require_operations()
        self.install_fake_tools()
        self.setup_project()
        holder = self.holder(self.repo)
        data = self.module.select_data_location(os.environ, read_only=True)
        state = self.module.state_path(self.repo, data)
        before = state.read_bytes()
        started = time.monotonic()
        with self.assertRaises(self.module.UserError):
            self.module.mutate_project(self.repo, operation="update", force=False, deadline=time.monotonic() + .2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(state.read_bytes(), before)
        holder.kill()
        holder.communicate(timeout=5)
        self.module.mutate_project(self.repo, operation="update", force=False, deadline=time.monotonic() + 10)
        self.assertEqual(self.module.read_marker(self.repo, data).status, "success")

    def test_nonfinite_deadline_does_not_create_lock_or_marker(self):
        self.require_operations()
        data = self.module.select_data_location(os.environ, read_only=True)
        before = snapshot(self.base)
        for deadline in (float("inf"), float("nan"), time.monotonic() - 1):
            with self.subTest(deadline=deadline), self.assertRaises(self.module.UserError):
                with self.module.root_lock(self.repo, data, deadline):
                    pass
        self.assertEqual(snapshot(self.base), before)

    def test_pending_process_diagnostics_preserve_filesystem_and_do_not_wait_on_lock(self):
        self.require_operations()
        self.install_fake_tools()
        self.setup_project()
        holder = self.holder(self.repo)
        before = snapshot(self.base)
        report = self.cli("doctor")
        self.assertNotEqual(report.returncode, 0)
        self.assertIn("pending", json.loads(report.stdout)["trust_reason"])
        self.assertEqual(snapshot(self.base), before)
        holder.communicate("release\n", timeout=5)

    def test_explicit_setup_and_update_wait_for_hook_equivalent_root_transaction(self):
        self.require_operations()
        self.install_fake_tools()
        self.setup_project()
        events = self.base / "events"
        for command in ("setup-project", "update-project"):
            holder = self.holder(self.repo)
            events.write_text("")
            process = subprocess.Popen([sys.executable, "-B", str(PACKAGE / "scripts/code_intel.py"), command, str(self.repo)], env={**os.environ, "TOOL_EVENTS": str(events)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                time.sleep(.25)
                self.assertIsNone(process.poll())
                self.assertEqual(events.read_text(), "")
                holder.communicate("release\n", timeout=5)
                out, err = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, err)
                self.assertEqual(out, "")
                self.assertTrue(events.read_text())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_linked_worktree_updates_overlap_without_state_loss(self):
        self.require_operations()
        self.install_fake_tools()
        linked = self.base / "linked"
        git(self.repo, "worktree", "add", "-qb", "linked", str(linked))
        self.setup_project()
        self.setup_project(linked)
        barrier = self.base / "barrier"
        barrier.mkdir()
        processes = [subprocess.Popen([sys.executable, "-B", str(PACKAGE / "scripts/code_intel.py"), "update-project", str(root)], env={**os.environ, "TOOL_BARRIER": str(barrier)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for root in (self.repo, linked)]
        try:
            for process in processes:
                _out, err = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, err)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
        data = self.module.select_data_location(os.environ, read_only=True)
        for root in (self.repo, linked):
            marker = self.module.read_marker(root, data)
            self.assertEqual((marker.status, marker.root), ("success", str(root.resolve())))
        self.assertEqual(len(list(self.data.glob("*.json"))), 2)
        self.assertEqual(len(list(self.data.glob("*.lock"))), 2)


class DoctorTests(ProjectCase):
    def observe(self):
        return self.module.observe_project(self.repo, deadline=time.monotonic() + 10)

    def test_doctor_without_state_is_unhealthy_and_read_only(self):
        self.install_fake_tools()
        before = snapshot(self.base)
        result = self.cli("project-status", self.repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith("{"), "project-status must report JSON")
        report = json.loads(result.stdout)
        self.assertFalse(report["healthy"])
        self.assertEqual(snapshot(self.base), before)

    def test_missing_data_and_tools_still_produce_read_only_json_diagnostics(self):
        self.require_operations()
        before = snapshot(self.base)
        with patch.dict(os.environ, {"PLUGIN_DATA": str(self.base / "absent")}), patch.object(self.module, "resolve_verified_tool", side_effect=self.module.UserError("missing tool")):
            report = self.observe()
        self.assertFalse(report["healthy"])
        self.assertIn("missing tool", report["trust_reason"])
        self.assertEqual(snapshot(self.base), before)

    def test_force_setup_cli_reinitializes_existing_indexes(self):
        self.require_operations()
        self.install_fake_tools()
        self.setup_project()
        events = self.base / "events"
        with patch.dict(os.environ, {"TOOL_EVENTS": str(events)}):
            result = self.cli("setup-project", self.repo, "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [json.loads(line)[1][0] for line in events.read_text().splitlines()]
        self.assertEqual(commands, ["index", "build", "sync", "update"])
        self.assertTrue((self.repo / ".codegraph/codegraph.db").read_text().startswith("rebuilt\n"))

    def test_force_setup_initializes_retained_directory_with_missing_database(self):
        self.install_fake_tools()
        self.setup_project()
        database = self.repo / ".codegraph/codegraph.db"
        database.unlink()
        events = self.base / "events"
        with patch.dict(os.environ, {"TOOL_EVENTS": str(events)}):
            result = self.cli("setup-project", self.repo, "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(database.is_file())
        commands = [json.loads(line)[1][0] for line in events.read_text().splitlines()]
        self.assertEqual(commands, ["init", "build", "sync", "update"])

    def test_capture_is_read_only_and_requires_every_selected_index(self):
        self.require_operations()
        tools = self.install_fake_tools()
        self.setup_project()
        before = snapshot(self.base)
        marker = self.module.capture(self.repo, tools, time.monotonic() + 10)
        self.assertEqual(marker.status, "success")
        self.assertEqual(snapshot(self.base), before)
        shutil.rmtree(self.repo / ".code-review-graph")
        with self.assertRaises(self.module.UserError):
            self.module.capture(self.repo, tools, time.monotonic() + 10)

    def test_mutation_during_second_index_capture_is_rejected(self):
        self.require_operations()
        tools = self.install_fake_tools()
        self.setup_project()
        database = self.repo / ".codegraph" / "codegraph.db"
        inode = database.stat().st_ino
        read = os.read
        reads = 0
        def mutate(fd, size):
            nonlocal reads
            result = read(fd, size)
            if os.fstat(fd).st_ino == inode:
                reads += 1
                if reads == 3:
                    database.write_text("changed on second pass")
            return result
        with patch.object(self.module.os, "read", side_effect=mutate), self.assertRaises(self.module.UserError):
            self.module.capture(self.repo, tools, time.monotonic() + 10)

    def test_checkout_and_head_mutations_during_final_index_pass_are_rejected(self):
        tools = self.install_fake_tools()
        self.setup_project()
        capture_index = self.module.index_fingerprint
        for mutation in ("checkout", "head"):
            passes = 0
            def mutate(root, name, deadline):
                nonlocal passes
                result = capture_index(root, name, deadline)
                if name == "crg":
                    passes += 1
                    if passes == 2:
                        if mutation == "checkout":
                            (root / "source.py").write_text("late edit\n")
                        else:
                            git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                "commit", "--allow-empty", "-qm", "late head")
                return result
            with self.subTest(mutation=mutation), patch.object(self.module, "index_fingerprint", side_effect=mutate):
                with self.assertRaisesRegex(self.module.UserError, "Checkout changed"):
                    self.module.capture(self.repo, tools, time.monotonic() + 10)

    def test_public_umbrella_setup_initializes_children_before_parent(self):
        self.install_fake_tools()
        umbrella = self.base / "umbrella"
        umbrella.mkdir()
        children = [umbrella / name for name in ("a-child", "z-child")]
        for child in children:
            git(self.base, "clone", "-q", str(self.repo), str(child))
        events = self.base / "events"
        with patch.dict(os.environ, {"TOOL_EVENTS": str(events)}):
            result = self.cli("setup-project", umbrella)
        self.assertEqual(result.returncode, 0, result.stderr)
        operations = [(Path(root), args[0]) for root, args in map(json.loads, events.read_text().splitlines())]
        self.assertEqual(operations, [
            (root.resolve(), command) for root, commands in (
                (children[0], ("init", "build", "sync", "update")),
                (children[1], ("init", "build", "sync", "update")),
                (umbrella, ("init", "sync")),
            ) for command in commands
        ])

    def test_healthy_diagnostics_report_facts_without_creating_or_acquiring_locks(self):
        self.require_operations()
        self.install_fake_tools()
        self.setup_project()
        data = self.module.select_data_location(os.environ, read_only=True)
        self.module.state_path(self.repo, data).with_suffix(".lock").unlink()
        before = snapshot(self.base)
        with patch.object(self.module, "root_lock", side_effect=AssertionError("doctor acquired lock")):
            report = self.observe()
        self.assertTrue(report["healthy"], report)
        self.assertEqual(report["scope"]["kind"], "repository")
        self.assertEqual(report["current_head"], git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(report["stored_head"], report["current_head"])
        self.assertEqual(report["data"]["source"], "PLUGIN_DATA")
        self.assertEqual(report["tools"]["codegraph"]["version"], "1.6.0")
        self.assertEqual(report["tools"]["crg"]["version"], "2.3.8")
        self.assertTrue(report["python"]["executable"])
        self.assertEqual(report["plugin_root"], str(PACKAGE.resolve()))
        self.assertIn("mise", report)
        self.assertIn("writable_best_effort", report["data"])
        self.assertEqual(self.cli("doctor").returncode, 0)
        self.assertEqual(snapshot(self.base), before)

    def test_pending_failed_and_corrupt_states_are_unhealthy_without_repair(self):
        self.require_operations()
        self.install_fake_tools()
        self.setup_project()
        data = self.module.select_data_location(os.environ, read_only=True)
        marker = self.module.read_marker(self.repo, data)
        state = self.module.state_path(self.repo, data)
        for content in (json.dumps({**dataclasses.asdict(marker), "status": "pending"}), json.dumps({**dataclasses.asdict(marker), "status": "failed"}), "broken"):
            state.write_text(content)
            before = snapshot(self.base)
            self.assertFalse(self.observe()["healthy"])
            self.assertEqual(snapshot(self.base), before)

    def test_marker_parser_recursion_emits_unhealthy_json_without_writes(self):
        self.require_operations()
        self.install_fake_tools()
        self.setup_project()
        before = snapshot(self.base)
        output = io.StringIO()
        with patch.object(self.module.json, "load", side_effect=RecursionError("JSON nesting is too deep")), contextlib.redirect_stdout(output):
            try:
                rc = self.module.main(["project-status", str(self.repo)])
            except RecursionError:
                self.fail("project-status raised parser recursion instead of reporting unhealthy JSON")
        self.assertNotEqual(rc, 0)
        report = json.loads(output.getvalue())
        self.assertFalse(report["healthy"])
        self.assertIn("JSON nesting is too deep", report["trust_reason"])
        self.assertEqual(snapshot(self.base), before)

    def test_same_head_offline_edits_and_index_changes_are_unhealthy(self):
        self.require_operations()
        self.install_fake_tools()
        self.setup_project()
        original_head = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "source.py").write_text("offline edit")
        self.assertFalse(self.observe()["healthy"])
        (self.repo / "source.py").write_text("value = 1\n")
        self.assertTrue(self.observe()["healthy"])
        (self.repo / ".codegraph" / "codegraph.db").write_text("changed index")
        self.assertFalse(self.observe()["healthy"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), original_head)

    def test_marker_changed_during_observation_is_unhealthy(self):
        self.require_operations()
        self.install_fake_tools()
        self.setup_project()
        capture = self.module.capture
        data = self.module.select_data_location(os.environ, read_only=True)
        def concurrent(*args, **kwargs):
            observation = capture(*args, **kwargs)
            self.module.write_marker(self.repo, data, dataclasses.replace(observation, status="pending"))
            return observation
        with patch.object(self.module, "capture", side_effect=concurrent):
            report = self.observe()
        self.assertFalse(report["healthy"])
        self.assertIn("changed", report["trust_reason"].lower())

    def test_failure_and_checkout_mutation_publish_failed_state(self):
        self.require_operations()
        self.install_fake_tools()
        self.setup_project()
        for flag in ("TOOL_FAIL", "TOOL_EDIT"):
            with patch.dict(os.environ, {flag: "1"}), self.assertRaises(self.module.UserError):
                self.module.mutate_project(self.repo, operation="update", force=False, deadline=time.monotonic() + 10)
            data = self.module.select_data_location(os.environ, read_only=True)
            self.assertEqual(self.module.read_marker(self.repo, data).status, "failed")

    def test_corrupt_state_prevents_indexing_and_missing_update_indexes_are_not_created(self):
        self.require_operations()
        self.install_fake_tools()
        result = self.cli("update-project", self.repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.repo / ".codegraph").exists())
        self.assertFalse((self.repo / ".code-review-graph").exists())
        data = self.module.select_data_location(os.environ, read_only=True)
        state = self.module.state_path(self.repo, data)
        state.write_text("broken")
        before = snapshot(self.base)
        self.assertNotEqual(self.cli("setup-project", self.repo).returncode, 0)
        self.assertEqual(snapshot(self.base), before)

    def test_failed_state_write_reports_original_index_error_too(self):
        self.require_operations()
        self.install_fake_tools()
        self.setup_project()
        write = self.module.write_marker
        def fail(root, data, marker):
            if marker.status == "failed":
                raise self.module.UserError("failed state write denied")
            write(root, data, marker)
        with patch.object(self.module, "write_marker", side_effect=fail), patch.dict(os.environ, {"TOOL_FAIL": "1"}), self.assertRaisesRegex(self.module.UserError, "status 9.*failed state write denied"):
            self.module.mutate_project(self.repo, operation="update", force=False, deadline=time.monotonic() + 10)

    def test_batch_setup_and_update_mark_children_and_codegraph_only_umbrella(self):
        self.require_operations()
        self.install_fake_tools()
        umbrella = self.base / "umbrella"
        umbrella.mkdir()
        child = umbrella / "child"
        git(self.base, "clone", "-q", str(self.repo), str(child))
        self.assertEqual(self.cli("setup-batch", umbrella).returncode, 0)
        self.assertEqual(self.cli("update-batch", umbrella).returncode, 0)
        self.assertTrue((umbrella / ".codegraph").is_dir())
        self.assertFalse((umbrella / ".code-review-graph").exists())
        data = self.module.select_data_location(os.environ, read_only=True)
        marker = self.module.read_marker(umbrella, data)
        self.assertEqual(marker.head, "")
        self.assertEqual(set(marker.index_fingerprints), {"codegraph"})
        self.assertTrue(self.module.observe_project(umbrella, deadline=time.monotonic() + 10)["healthy"])


class HookCase(ProjectCase):
    def setUp(self):
        super().setUp()
        self.events = self.base / "hook-events"
        self.events.write_text("")
        self.prompt_input = self.base / "prompt-input"
        directory = self.base / "hook-bin"
        for name, version, index in (("codegraph", "1.6.0", ".codegraph"),
                                      ("code-review-graph", "2.3.8", ".code-review-graph")):
            self.executable(directory, name,
                "import json, os, signal, subprocess, sys, time\nfrom pathlib import Path\n"
                "if sys.argv[1:] == ['--version']:\n"
                f" version = os.environ.get('HOOK_VERSION', {version!r})\n"
                f" if os.environ.get('HOOK_WRONG_VERSION_TOOL') == {name!r} and str(Path.cwd()) == os.environ.get('HOOK_WRONG_VERSION_CWD'): version = '9.9.9'\n"
                " print(version); sys.exit(0)\n"
                "root = Path.cwd(); command = sys.argv[1]\n"
                "with open(os.environ['HOOK_EVENTS'], 'a') as log: log.write(json.dumps([str(root), sys.argv[1:]]) + '\\n')\n"
                "mode = os.environ.get('HOOK_MODE', '')\n"
                "if sys.argv[1:4] == ['update', '--base', '4b825dc642cb6eb9a060e54bf8d69288fbee4904']:\n"
                " if mode == 'repair-fail': sys.exit(9)\n"
                " if mode == 'repair-edit': (root / 'a.py').write_text('changed during repair')\n"
                " if mode == 'repair-head': subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'during repair'], check=True)\n"
                " if mode == 'repair-stage': subprocess.run(['git', 'add', 'local.py'], check=True)\n"
                " if mode == 'repair-descendant':\n"
                "  writer = 'import os,signal,sys,time\\nfrom pathlib import Path\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\nPath(sys.argv[1]).write_text(str(os.getpid()))\\nwhile True:\\n Path(sys.argv[2]).write_text(str(time.monotonic_ns()))\\n time.sleep(.01)\\n'\n"
                "  subprocess.Popen([sys.executable, '-c', writer, os.environ['HOOK_PID'], str(root / '.code-review-graph/writer')])\n"
                "  time.sleep(30)\n"
                "if command == 'sync':\n"
                " if mode == 'sync-fail': sys.exit(9)\n"
                " if mode == 'sync-edit': (root / 'source.py').write_text('changed during sync')\n"
                " if mode == 'sync-head': subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'during sync'], check=True)\n"
                "if command == 'prompt-hook':\n"
                " Path(os.environ['HOOK_INPUT']).write_text(sys.stdin.read())\n"
                " if mode == 'prompt-edit': (root / 'source.py').write_text('changed during prompt')\n"
                " if mode == 'prompt-index': (root / '.codegraph' / 'graph.db').write_text('changed during prompt')\n"
                " if mode == 'prompt-head': subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'during prompt'], check=True)\n"
                " if mode == 'prompt-malformed': print('unstructured diagnostic'); sys.exit(0)\n"
                " if mode == 'prompt-empty': sys.exit(0)\n"
                " print('<codegraph_context note=\"Structural context from CodeGraph for this prompt\">\\nFRESH PROMPT CONTEXT\\n</codegraph_context>', flush=True)\n"
                " if mode == 'prompt-fail': sys.exit(9)\n"
                " if mode == 'prompt-sleep': time.sleep(10)\n"
                " if mode == 'prompt-descendant':\n"
                "  child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)'])\n"
                "  Path(os.environ['HOOK_PID']).write_text(str(child.pid)); time.sleep(10)\n"
                " if mode == 'prompt-barrier':\n"
                "  with open(os.environ['HOOK_NOTIFY'], 'w') as notify: notify.write('prompt\\n'); notify.flush()\n"
                "  with open(os.environ['HOOK_RELEASE']) as release: release.readline()\n"
                " sys.exit(0)\n"
                f"index = root / {index!r}; index.mkdir(exist_ok=True)\n"
                "with (index / 'graph.db').open('a') as db: db.write('indexed\\n')\n")
        env = patch.dict(os.environ, {"PATH": str(directory) + os.pathsep + os.environ["PATH"],
            "HOOK_EVENTS": str(self.events), "HOOK_INPUT": str(self.prompt_input)})
        env.start()
        self.addCleanup(env.stop)

    def require_readiness(self):
        self.assertTrue(hasattr(self.module, "ensure_ready"), "readiness transaction is absent")

    def require_hooks(self):
        self.assertTrue(hasattr(self.module, "handle_hook"), "lifecycle adapters are absent")

    def commands(self):
        return [json.loads(line)[1][0] for line in self.events.read_text().splitlines()]

    def marker(self, root=None):
        data = self.module.select_data_location(os.environ, read_only=True)
        return self.module.read_marker(root or self.repo, data)

    def ready(self, root=None, force=False, deadline=None):
        return self.module.ensure_ready(root or self.repo, force_sync=force,
            deadline=deadline if deadline is not None else time.monotonic() + 10)

    def warm(self):
        with self.ready():
            pass
        self.events.write_text("")

    def hook(self, command="hook-prompt", *, root=None, **extra):
        payload = {"cwd": str(root or self.repo), "prompt": "explain source", **extra}
        return self.module.handle_hook(command, payload)

    def assert_fallback(self, response, event="UserPromptSubmit"):
        self.assertEqual(set(response), {"hookSpecificOutput"})
        output = response["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], event)
        self.assertIn("normal file/search tools", output["additionalContext"])
        self.assertNotIn("FRESH PROMPT CONTEXT", json.dumps(response))
        self.assertNotIn("use codegraph first", json.dumps(response).lower())
        self.assertFalse(output.get("block", False))


class RollbackReadinessTests(HookCase):
    EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

    def fixture(self, name="fixture", *, warm=True):
        self.repo = self.base / name / "repo"
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "-q")
        (self.repo / "a.py").write_text("def stable_a():\n    return 1\n")
        (self.repo / "b.py").write_text("def stable_b():\n    return 2\n")
        git(self.repo, "add", "a.py", "b.py")
        self.commit("fixture")
        self.events.write_text("")
        if warm:
            self.warm()

    def commit(self, message):
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", message)

    def edit(self, *names):
        for name in names:
            with (self.repo / name).open("a") as stream:
                stream.write("\nvalue = 9\n")

    def sync(self):
        response = self.hook("hook-update", tool_name="Bash")
        self.assertNotIn("Code intelligence unavailable", json.dumps(response))

    def prepare_restore(self, name="fixture"):
        self.fixture(name)
        self.edit("a.py", "b.py")
        self.sync()
        self.assertEqual(self.marker().crg_candidates, ["a.py", "b.py"])
        git(self.repo, "restore", "--", "a.py")
        self.events.write_text("")

    @contextlib.contextmanager
    def trace_children(self):
        runner = self.module.run_child
        calls = []
        def traced(argv, **kwargs):
            calls.append((list(argv), kwargs))
            if len(argv) > 1 and argv[1] == "update":
                self.assertEqual(self.marker().status, "pending")
            return runner(argv, **kwargs)
        with patch.object(self.module, "run_child", side_effect=traced):
            yield calls

    def assert_update(self, base, candidates):
        rows = map(json.loads, self.events.read_text().splitlines())
        crg_update_argv = [row[1] for row in rows if row[1][0] == "update"]
        self.assertEqual(crg_update_argv, [[
            "update", *([] if base is None else ["--base", base]),
            "--skip-flows", "--repo", str(self.repo.resolve()),
        ]])
        self.assertEqual(self.marker().schema_version, 2)
        self.assertEqual(self.marker().crg_candidates, candidates)
        self.assertEqual(self.marker().status, "success")

    def assert_no_empty_tree_write(self, calls):
        self.assertEqual([argv for argv, _ in calls if "hash-object" in argv], [])

    def test_ordinary_edits_and_commit_use_previous_head(self):
        for transition in ("clean-edit", "dirty-edit", "dirty-commit"):
            with self.subTest(transition=transition):
                self.fixture(transition)
                previous_head = self.marker().head
                self.edit("a.py")
                if transition != "clean-edit":
                    self.sync()
                    self.events.write_text("")
                    if transition == "dirty-edit":
                        self.edit("a.py")
                    else:
                        git(self.repo, "add", "a.py")
                        self.commit("indexed bytes")
                with self.trace_children() as calls:
                    self.sync()
                self.assert_update(previous_head, [] if transition == "dirty-commit" else ["a.py"])
                self.assert_no_empty_tree_write(calls)
                self.assertNotIn("build", self.commands())

    def test_restore_reset_and_restored_deletion_use_empty_tree(self):
        for transition in ("restore", "reset", "deletion"):
            with self.subTest(transition=transition):
                self.fixture(transition)
                head = self.marker().head
                if transition == "deletion":
                    git(self.repo, "rm", "a.py")
                else:
                    self.edit("a.py")
                self.sync()
                self.assertEqual(self.marker().crg_candidates, ["a.py"])
                if transition == "deletion":
                    git(self.repo, "restore", "--source=HEAD", "--staged", "--worktree", "--", "a.py")
                elif transition == "reset":
                    git(self.repo, "reset", "--hard", "HEAD")
                else:
                    git(self.repo, "restore", "--", "a.py")
                self.events.write_text("")
                self.sync()
                self.assert_update(self.EMPTY_TREE, [])
                self.assertEqual(self.marker().head, head)
                self.assertNotIn("build", self.commands())

    def test_branch_switch_base_uses_previous_head_and_omitted_paths(self):
        for transition, changed_file, expected_base in (
                ("clean", "b.py", "previous"),
                ("restored-a", "b.py", self.EMPTY_TREE),
                ("committed-a", "a.py", "previous")):
            with self.subTest(transition=transition):
                self.fixture(transition)
                previous_head = self.marker().head
                branch = git(self.repo, "branch", "--show-current")
                git(self.repo, "switch", "-qc", "other")
                (self.repo / changed_file).write_text("def other_branch():\n    return 3\n")
                git(self.repo, "add", changed_file)
                self.commit("other branch")
                git(self.repo, "switch", "-q", branch)
                if transition != "clean":
                    self.edit("a.py")
                    self.sync()
                    git(self.repo, "restore", "--", "a.py")
                git(self.repo, "switch", "-q", "other")
                self.events.write_text("")
                with self.trace_children() as calls:
                    self.sync()
                self.assert_update(previous_head if expected_base == "previous" else expected_base, [])
                self.assertNotEqual(self.marker().head, previous_head)
                if expected_base == "previous":
                    self.assert_no_empty_tree_write(calls)

    def test_partial_restore_repairs_every_mutating_entrypoint_before_prompt(self):
        for command, tool in (("hook-status", None), ("hook-prompt", None),
                ("hook-update", "Bash"), ("hook-update", "Write"),
                ("update-project", None), ("update-batch", None)):
            with self.subTest(command=command, tool=tool):
                self.fixture(command + str(tool))
                if command == "update-batch":
                    self.setup_project(self.repo.parent)
                self.edit("a.py", "b.py")
                self.sync()
                git(self.repo, "restore", "--", "a.py")
                self.events.write_text("")
                with self.trace_children() as calls:
                    if command.startswith("hook"):
                        response = self.hook(command, **({"tool_name": tool} if tool else {}))
                        self.assertNotIn("Code intelligence unavailable", json.dumps(response))
                    else:
                        target = self.repo.parent if command == "update-batch" else self.repo
                        self.assertEqual(self.module.main([command, str(target)]), 0)
                self.assert_update(self.EMPTY_TREE, ["b.py"])
                empty_commands = [(argv, kwargs) for argv, kwargs in calls if "hash-object" in argv]
                self.assertEqual([argv for argv, _ in empty_commands],
                    [["git", "hash-object", "-t", "tree", "-w", "--stdin"]])
                self.assertEqual(empty_commands[0][1]["input_text"], "")
                self.assertNotIn("build", self.commands())
                if command == "hook-prompt":
                    self.assertEqual(self.commands(), ["sync", "update", "prompt-hook"])

    def test_matching_session_prompt_reuse_but_bash_forces_ordinary_update(self):
        self.fixture()
        head = self.marker().head
        for command in ("hook-status", "hook-prompt"):
            with self.subTest(command=command), self.trace_children() as calls:
                self.events.write_text("")
                self.hook(command)
                self.assertEqual(self.commands(), [] if command == "hook-status" else ["prompt-hook"])
                self.assert_no_empty_tree_write(calls)
        self.events.write_text("")
        with self.trace_children() as calls:
            self.sync()
        self.assert_update(head, [])
        self.assert_no_empty_tree_write(calls)

    def test_missing_legacy_failed_pending_history_repairs_existing_crg(self):
        for history in ("missing", "legacy", "failed", "pending"):
            with self.subTest(history=history):
                self.fixture(history)
                data = self.module.select_data_location(os.environ, read_only=True)
                state = self.module.state_path(self.repo, data)
                if history == "missing":
                    state.unlink()
                elif history == "legacy":
                    value = json.loads(state.read_text())
                    del value["schema_version"], value["crg_candidates"]
                    state.write_text(json.dumps(value))
                else:
                    self.module.write_marker(self.repo, data, self.module.FreshnessMarker(
                        str(self.repo.resolve()), "", {}, "", {}, history))
                self.sync()
                self.assert_update(self.EMPTY_TREE, [])
                self.assertEqual(self.commands(), ["sync", "update"])

    def test_new_or_forced_crg_build_skips_redundant_repair(self):
        for forced in (False, True):
            with self.subTest(forced=forced):
                self.fixture(str(forced), warm=forced)
                with self.trace_children() as calls:
                    if forced:
                        self.assertEqual(self.module.main(["setup-project", str(self.repo), "--force"]), 0)
                    else:
                        self.hook("hook-status")
                self.assert_update(None, [])
                self.assertEqual(self.commands(), ["init", "build", "sync", "update"])
                self.assert_no_empty_tree_write(calls)

    def test_explicit_update_missing_crg_fails_without_initialization_or_tree_write(self):
        self.fixture()
        shutil.rmtree(self.repo / ".code-review-graph")
        with self.trace_children() as calls, contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(self.module.main(["update-project", str(self.repo)]), 0)
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.marker().status, "failed")
        self.assert_no_empty_tree_write(calls)
        self.assertFalse((self.repo / ".code-review-graph").exists())

    def test_umbrella_root_never_runs_crg_discovery_or_repair(self):
        self.fixture()
        umbrella = self.repo.parent.resolve()
        with self.trace_children() as calls:
            self.setup_project(umbrella)
        at_root = [(argv, kwargs) for argv, kwargs in calls if kwargs.get("cwd") == umbrella]
        self.assertFalse(any("--name-status" in argv or "hash-object" in argv
                             or Path(argv[0]).name == "code-review-graph" for argv, _ in at_root))
        self.assertFalse((umbrella / ".git").exists())
        self.assertIsNone(self.marker(umbrella).crg_candidates)
        self.assertEqual(self.marker(umbrella).status, "success")

    def test_repair_child_failure_invalidates_hooks_and_explicit_update_then_retries(self):
        for command in ("hook-prompt", "update-project"):
            with self.subTest(command=command):
                self.prepare_restore(command)
                with patch.dict(os.environ, {"HOOK_MODE": "repair-fail"}):
                    if command == "hook-prompt":
                        response = self.hook("hook-prompt")
                        self.assert_fallback(response)
                    else:
                        result = self.cli("update-project", self.repo)
                        self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.marker().status, "failed")
                self.assertIsNone(self.marker().crg_candidates)
                self.assertEqual(self.commands(), ["sync", "update"])
                self.assertNotIn("prompt-hook", self.commands())
                self.events.write_text("")
                self.sync()
                self.assert_update(self.EMPTY_TREE, ["b.py"])

    def test_repair_timeout_reaps_writer_before_next_root_operation(self):
        self.prepare_restore()
        pid_path = self.base / "repair-writer.pid"
        runner = self.module.run_child
        def bounded(argv, **kwargs):
            if argv[1:4] == ["update", "--base", self.EMPTY_TREE]:
                kwargs["timeout"] = min(kwargs["timeout"], .5)
            return runner(argv, **kwargs)
        with patch.dict(os.environ, {"HOOK_MODE": "repair-descendant", "HOOK_PID": str(pid_path)}), \
                patch.object(self.module, "run_child", side_effect=bounded):
            response = self.hook("hook-prompt")
        self.assert_fallback(response)
        self.assertIn("timed out", json.dumps(response))
        self.assertEqual(self.marker().status, "failed")
        self.assertEqual(self.commands(), ["sync", "update"])
        self.assertNotIn("prompt-hook", self.commands())
        self.assertTrue(pid_path.exists(), "repair writer did not start before timeout")
        pid = int(pid_path.read_text())
        self.addCleanup(lambda: self.kill_if_alive(pid))
        ToolContractTests.assert_process_exited(self, pid)
        writer = self.repo / ".code-review-graph/writer"
        before = writer.read_bytes()
        self.events.write_text("")
        self.sync()
        self.assert_update(self.EMPTY_TREE, ["b.py"])
        self.assertEqual(writer.read_bytes(), before)

    @staticmethod
    def kill_if_alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)

    def test_failed_candidate_and_empty_tree_commands_invalidate_and_retry(self):
        cases = ("candidate-error", "candidate-malformed", "tree-error", "tree-malformed", "tree-wrong-format")
        for failure in cases:
            for command in ("hook-prompt", "update-project"):
                with self.subTest(failure=failure, command=command):
                    self.prepare_restore(failure + command)
                    runner = self.module.run_child
                    def fail(argv, **kwargs):
                        candidate = "--name-status" in argv
                        tree = "hash-object" in argv
                        if (candidate and failure == "candidate-error") or (tree and failure == "tree-error"):
                            raise self.module.UserError("injected Git command failure")
                        if candidate and failure == "candidate-malformed":
                            return subprocess.CompletedProcess(argv, 0, "R100\0old.py\0", "")
                        if tree and failure in ("tree-malformed", "tree-wrong-format"):
                            return subprocess.CompletedProcess(argv, 0,
                                "not-an-object\n" if failure == "tree-malformed" else "a" * 64 + "\n", "")
                        return runner(argv, **kwargs)
                    with patch.object(self.module, "run_child", side_effect=fail), contextlib.redirect_stderr(io.StringIO()):
                        if command == "hook-prompt":
                            response = self.hook("hook-prompt")
                            self.assert_fallback(response)
                        else:
                            self.assertNotEqual(self.module.main([command, str(self.repo)]), 0)
                    self.assertEqual(self.marker().status, "failed")
                    self.assertNotIn("prompt-hook", self.commands())
                    self.assertEqual(self.commands(), [])
                    self.events.write_text("")
                    self.sync()
                    self.assert_update(self.EMPTY_TREE, ["b.py"])

    def test_unavailable_previous_head_fails_then_retries_with_empty_tree(self):
        self.prepare_restore()
        data = self.module.select_data_location(os.environ, read_only=True)
        self.module.write_marker(self.repo, data, dataclasses.replace(self.marker(), head="1" * 40))
        response = self.hook("hook-prompt")
        self.assert_fallback(response)
        self.assertEqual(self.marker().status, "failed")
        self.assertNotIn("prompt-hook", self.commands())
        self.assertEqual(self.commands(), [])
        self.sync()
        self.assert_update(self.EMPTY_TREE, ["b.py"])

    def test_file_head_or_index_only_change_during_repair_prevents_success(self):
        for mode in ("repair-edit", "repair-head", "repair-stage"):
            for command in ("hook-prompt", "update-project"):
                with self.subTest(mode=mode, command=command):
                    self.prepare_restore(mode + command)
                    (self.repo / "local.py").write_text("value = 5\n")
                    before = self.module.capture_checkout(self.repo, time.monotonic() + 10)
                    with patch.dict(os.environ, {"HOOK_MODE": mode}), contextlib.redirect_stderr(io.StringIO()):
                        if command == "hook-prompt":
                            response = self.hook("hook-prompt")
                            self.assert_fallback(response)
                        else:
                            self.assertNotEqual(self.module.main([command, str(self.repo)]), 0)
                    self.assertEqual(self.marker().status, "failed")
                    self.assertNotIn("prompt-hook", self.commands())
                    self.assertEqual(self.commands(), ["sync", "update"])
                    if mode == "repair-stage":
                        self.assertEqual(self.module.capture_checkout(self.repo, time.monotonic() + 10), before)
                        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "local.py")

    def test_capture_rejects_staging_between_candidate_observations(self):
        self.fixture()
        (self.repo / "local.py").write_text("value = 5\n")
        runner = self.module.run_child
        staged = False
        def stage_after_diff(argv, **kwargs):
            nonlocal staged
            result = runner(argv, **kwargs)
            if "--name-status" in argv and not staged:
                staged = True
                git(self.repo, "add", "local.py")
            return result
        with patch.object(self.module, "run_child", side_effect=stage_after_diff):
            response = self.hook("hook-prompt")
        self.assert_fallback(response)
        self.assertEqual(self.marker().status, "failed")
        self.assertEqual(self.commands(), [])

    def test_status_and_doctor_observe_rollback_legacy_and_repair_without_writes(self):
        self.prepare_restore()
        data = self.module.select_data_location(os.environ, read_only=True)
        state = self.module.state_path(self.repo, data)
        for phase, healthy in (("rollback", False), ("legacy", False), ("repaired", True)):
            with self.subTest(phase=phase):
                if phase == "legacy":
                    value = json.loads(state.read_text())
                    del value["schema_version"], value["crg_candidates"]
                    state.write_text(json.dumps(value))
                elif phase == "repaired":
                    self.sync()
                self.events.write_text("")
                state.with_suffix(".lock").unlink(missing_ok=True)
                before = snapshot(self.base)
                for command in ("project-status", "doctor"):
                    args = [command, str(self.repo)] if command == "project-status" else [command]
                    result = self.cli(*args)
                    self.assertEqual(result.returncode, 0 if healthy else 1, result.stderr)
                    self.assertIs(json.loads(result.stdout)["healthy"], healthy)
                    self.assertEqual(snapshot(self.base), before)
                with self.trace_children() as calls:
                    report = self.module.observe_project(self.repo, deadline=time.monotonic() + 10)
                self.assertIs(report["healthy"], healthy)
                self.assert_no_empty_tree_write(calls)
                self.assertEqual(snapshot(self.base), before)

    def test_repair_contender_leaves_current_lock_owners_marker_untouched(self):
        self.prepare_restore()
        holder = ConcurrencyTests.holder(self, self.repo)
        data = self.module.select_data_location(os.environ, read_only=True)
        state = self.module.state_path(self.repo, data)
        before = state.read_bytes()
        with self.assertRaises(self.module.UserError):
            with self.ready(deadline=time.monotonic() + .2):
                self.fail("contender acquired readiness")
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(self.commands(), [])
        holder.kill()
        holder.communicate(timeout=5)
        self.sync()
        self.assert_update(self.EMPTY_TREE, ["b.py"])


class ReadinessTests(HookCase):
    def test_final_revalidation_rejects_checkout_edit_during_last_index_hash(self):
        self.warm()
        capture_index = self.module.index_fingerprint
        passes = 0
        def mutate(root, name, deadline):
            nonlocal passes
            result = capture_index(root, name, deadline)
            if name == "crg":
                passes += 1
                if passes == 4:
                    (root / "source.py").write_text("late context-exit edit\n")
            return result
        with patch.object(self.module, "index_fingerprint", side_effect=mutate), self.assertRaisesRegex(self.module.UserError, "Checkout changed"):
            with self.ready():
                pass
        self.assertEqual(self.marker().status, "failed")

    def test_initializes_in_dependency_order_and_publishes_only_after_context_exit(self):
        self.require_readiness()
        with self.ready() as ready:
            self.assertEqual(self.marker().status, "pending")
            self.assertEqual(ready.root, self.repo.resolve())
            self.assertEqual(set(ready.tools), {"codegraph", "crg"})
            self.assertEqual(ready.marker.status, "success")
            with self.assertRaises(dataclasses.FrozenInstanceError):
                ready.root = self.base
        self.assertEqual(self.commands(), ["init", "build", "sync", "update"])
        self.assertEqual(self.marker(), ready.marker)

    def test_matching_marker_reuses_indexes_but_final_capture_still_rejects_edits(self):
        self.require_readiness()
        self.warm()
        with self.ready():
            pass
        self.assertEqual(self.commands(), [])
        with self.assertRaisesRegex(self.module.UserError, "changed"):
            with self.ready():
                (self.repo / "source.py").write_text("late edit")
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.marker().status, "failed")

    def test_force_sync_updates_even_with_identical_clean_checkout(self):
        self.require_readiness()
        self.warm()
        head = git(self.repo, "rev-parse", "HEAD")
        with self.ready(force=True):
            pass
        self.assertEqual(self.commands(), ["sync", "update"])
        self.assertEqual(self.marker().head, head)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_offline_edits_index_changes_and_nonmatching_markers_resynchronize(self):
        self.require_readiness()
        self.warm()
        data = self.module.select_data_location(os.environ, read_only=True)
        for change in ("source", "index", "pending", "failed", "version"):
            with self.subTest(change=change):
                self.events.write_text("")
                if change == "source":
                    (self.repo / "source.py").write_text("offline edit")
                elif change == "index":
                    (self.repo / ".codegraph" / "graph.db").write_text("offline index")
                else:
                    marker = self.marker()
                    marker = dataclasses.replace(marker, versions={"codegraph": "0.0.1", "crg": "2.3.8"}) if change == "version" else dataclasses.replace(marker, status=change)
                    self.module.write_marker(self.repo, data, marker)
                with self.ready():
                    pass
                self.assertEqual(self.commands(), ["sync", "update"])
                self.assertEqual(self.marker().status, "success")

    def test_deleted_indexes_are_recreated(self):
        self.require_readiness()
        self.warm()
        for index, expected in ((".codegraph", ["init", "sync", "update"]),
                                (".code-review-graph", ["build", "sync", "update"])):
            with self.subTest(index=index):
                shutil.rmtree(self.repo / index)
                self.events.write_text("")
                with self.ready():
                    pass
                self.assertEqual(self.commands(), expected)
                self.assertEqual(self.marker().status, "success")

    def test_clean_branch_switch_resynchronizes(self):
        self.require_readiness()
        self.warm()
        previous = self.marker().head
        git(self.repo, "checkout", "-qb", "other")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "other")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        with self.ready():
            pass
        self.assertNotEqual(previous, self.marker().head)
        self.assertEqual(self.commands(), ["sync", "update"])

    def test_checkout_or_head_change_during_sync_leaves_failed_state(self):
        self.require_readiness()
        self.warm()
        for mode in ("sync-edit", "sync-head", "sync-fail"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"HOOK_MODE": mode}), self.assertRaises(self.module.UserError):
                with self.ready(force=True):
                    self.fail("unstable indexing yielded readiness")
            self.assertEqual(self.marker().status, "failed")

    def test_tool_verification_failure_invalidates_writable_success(self):
        self.require_readiness()
        self.warm()
        with patch.dict(os.environ, {"HOOK_VERSION": "9.9.9"}), self.assertRaisesRegex(self.module.UserError, "version"):
            with self.ready():
                self.fail("unverified tools yielded readiness")
        self.assertEqual(self.marker().status, "failed")
        self.assertEqual(self.commands(), [])

    def test_corrupt_state_is_preserved_without_index_commands(self):
        self.require_readiness()
        self.warm()
        data = self.module.select_data_location(os.environ, read_only=True)
        path = self.module.state_path(self.repo, data)
        path.write_text("broken state")
        with self.assertRaises(self.module.CorruptState):
            with self.ready():
                pass
        self.assertEqual(path.read_text(), "broken state")
        self.assertEqual(self.commands(), [])

    def test_fingerprint_failure_invalidates_writable_success(self):
        self.require_readiness()
        self.warm()
        index = self.repo / ".codegraph" / "graph.db"
        index.chmod(0)
        self.addCleanup(index.chmod, 0o600)
        with self.assertRaisesRegex(self.module.UserError, "Unreadable"):
            with self.ready():
                pass
        self.assertEqual(self.marker().status, "failed")

    def test_context_deadline_expires_without_success_publication(self):
        self.require_readiness()
        self.warm()
        deadline = time.monotonic() + 2
        with self.assertRaisesRegex(self.module.UserError, "(?i)deadline"):
            with self.ready(deadline=deadline):
                time.sleep(max(0, deadline - time.monotonic()) + .01)
        self.assertEqual(self.marker().status, "failed")

    def test_invalidation_error_preserves_original_diagnostic(self):
        self.require_readiness()
        self.warm()
        write = self.module.write_marker
        def fail(root, data, marker):
            if marker.status == "failed":
                raise self.module.UserError("failed state write denied")
            write(root, data, marker)
        with patch.object(self.module, "write_marker", side_effect=fail), patch.dict(os.environ, {"HOOK_MODE": "sync-fail"}), self.assertRaisesRegex(self.module.UserError, "status 9.*failed state write denied"):
            with self.ready(force=True):
                pass


class HookTests(HookCase):
    def test_raw_prompt_context_and_empty_noop_preserve_shared_routing(self):
        for mode, prefix in (("", '<codegraph_context note="Structural context from CodeGraph for this prompt">\nFRESH PROMPT CONTEXT\n</codegraph_context>\n\n'), ("prompt-empty", "")):
            with self.subTest(mode=mode), patch.dict(os.environ, {"HOOK_MODE": mode}):
                response = self.hook()
            self.assertEqual(response, {"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": prefix + self.module.ROUTING_CONTEXT,
            }})
            self.assertEqual(self.marker().status, "success")

    def test_directory_sensitive_shims_reject_wrong_versions_at_payload_root(self):
        host_cwd = self.base / "host-cwd"
        host_cwd.mkdir()
        for executable in ("codegraph", "code-review-graph"):
            with self.subTest(executable=executable):
                self.events.write_text("")
                result = subprocess.run(
                    [sys.executable, "-B", str(PACKAGE / "scripts/code_intel.py"), "hook-prompt"],
                    input=json.dumps({"cwd": str(self.repo), "prompt": "explain source"}),
                    cwd=host_cwd, env={**os.environ,
                        "HOOK_WRONG_VERSION_TOOL": executable,
                        "HOOK_WRONG_VERSION_CWD": str(self.repo.resolve())},
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_fallback(json.loads(result.stdout))
                self.assertIn(executable + " version 9.9.9", result.stdout)
                self.assertEqual(self.commands(), [])
                self.assertEqual(self.marker().status, "failed")
                self.assertFalse(self.prompt_input.exists())
                self.assertFalse((self.repo / ".codegraph").exists())

    def test_directory_sensitive_shims_accept_pins_at_canonical_payload_root(self):
        host_cwd = self.base / "host-cwd"
        host_cwd.mkdir()
        nested = self.repo / "nested"
        nested.mkdir()
        alias = self.base / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        for executable in ("codegraph", "code-review-graph"):
            with self.subTest(executable=executable):
                self.events.write_text("")
                result = subprocess.run(
                    [sys.executable, "-B", str(PACKAGE / "scripts/code_intel.py"), "hook-prompt"],
                    input=json.dumps({"cwd": str(alias / "nested"), "prompt": "explain source"}),
                    cwd=host_cwd, env={**os.environ,
                        "HOOK_WRONG_VERSION_TOOL": executable,
                        "HOOK_WRONG_VERSION_CWD": str(host_cwd.resolve())},
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("FRESH PROMPT CONTEXT", result.stdout)
                self.assertEqual(self.marker().status, "success")
                self.assertEqual(self.marker().root, str(self.repo.resolve()))
                self.assertTrue(all(json.loads(line)[0] == str(self.repo.resolve())
                    for line in self.events.read_text().splitlines()))

    def test_session_initializes_and_prompt_preserves_original_payload_and_shared_json(self):
        self.require_hooks()
        response = self.module.handle_hook("hook-status", {}, cwd=self.repo)
        self.assertEqual(response["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("CodeGraph", response["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.commands(), ["init", "build", "sync", "update"])
        payload = {"cwd": str(self.repo), "hook_event_name": "UserPromptSubmit", "prompt": "explain source", "session_id": "original"}
        response = self.module.handle_hook("hook-prompt", payload)
        self.assertEqual(json.loads(self.prompt_input.read_text()), payload)
        self.assertEqual(response["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        text = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("FRESH PROMPT CONTEXT", text)
        self.assertIn("CodeGraph", text)
        self.assertIn("code-review-graph", text)
        self.assertNotIn("UNTRUSTED FIELD", json.dumps(response))
        self.assertEqual(self.commands(), ["init", "build", "sync", "update", "prompt-hook"])
        self.assertEqual(self.marker().status, "success")

    def test_prompt_bash_and_write_discover_new_worktrees_from_effective_cwd(self):
        self.require_hooks()
        for position, (command, tool) in enumerate((("hook-prompt", None), ("hook-update", "Bash"), ("hook-update", "Write"))):
            with self.subTest(command=command, tool=tool):
                linked = self.base / ("linked-" + str(position))
                git(self.repo, "worktree", "add", "-qb", "branch-" + str(position), str(linked))
                (linked / "nested").mkdir()
                self.events.write_text("")
                response = self.hook(command, root=linked / "nested", **({"toolName": tool} if tool else {}))
                self.assertNotIn("unavailable", json.dumps(response).lower())
                self.assertEqual(self.marker(linked).root, str(linked.resolve()))
                self.assertEqual(self.marker(linked).status, "success")
                expected = ["init", "build", "sync", "update"] + (["prompt-hook"] if tool is None else [])
                self.assertEqual(self.commands(), expected)
                self.assertFalse((linked / "nested" / ".codegraph").exists())

    def test_every_bash_and_supported_write_forces_sync_for_same_head_and_clean_checkout(self):
        self.require_hooks()
        self.hook("hook-status")
        head = git(self.repo, "rev-parse", "HEAD")
        for tool in ("Bash", "Write", "Edit", "NotebookEdit", "apply_patch"):
            with self.subTest(tool=tool):
                self.events.write_text("")
                response = self.hook("hook-update", tool_name=tool, tool_input={"command": "true"})
                self.assertEqual(response["hookSpecificOutput"]["hookEventName"], "PostToolUse")
                self.assertEqual(self.commands(), ["sync", "update"])
                self.assertEqual(self.marker().head, head)
                self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_bash_restore_reset_and_arbitrary_mutation_resynchronize(self):
        self.require_hooks()
        self.hook("hook-status")
        head = self.marker().head
        for command in (("restore", "source.py"), ("reset", "--hard", "HEAD"), None):
            with self.subTest(command=command):
                (self.repo / "source.py").write_text("modified")
                if command:
                    git(self.repo, *command)
                self.events.write_text("")
                self.hook("hook-update", tool_name="Bash", tool_input={"command": "arbitrary script"})
                self.assertEqual(self.commands(), ["sync", "update"])
                self.assertEqual(self.marker().head, head)

    def test_unsupported_post_tool_is_empty_without_indexing(self):
        self.require_hooks()
        response = self.hook("hook-update", tool_name="Read")
        self.assertEqual(response, {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": ""}})
        self.assertEqual(self.commands(), [])
        self.assertFalse((self.repo / ".codegraph").exists())

    def test_non_git_umbrella_returns_authorization_guidance_without_initializing(self):
        self.require_hooks()
        response = self.hook(root=self.base)
        self.assert_fallback(response)
        self.assertIn("setup-project", json.dumps(response))
        self.assertIn("authorization", json.dumps(response))
        self.assertEqual(self.commands(), [])
        self.assertFalse((self.base / ".codegraph").exists())
        self.assertFalse((self.repo / ".codegraph").exists())

    def test_unready_prompt_does_not_invoke_prompt_hook(self):
        self.require_hooks()
        self.hook("hook-status")
        self.events.write_text("")
        with patch.dict(os.environ, {"HOOK_VERSION": "9.9.9"}):
            response = self.hook()
        self.assert_fallback(response)
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.marker().status, "failed")

    def test_prompt_mutation_failed_exit_and_bad_output_discard_provisional_context(self):
        self.require_hooks()
        self.hook("hook-status")
        for mode in ("prompt-edit", "prompt-index", "prompt-head", "prompt-fail", "prompt-malformed"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"HOOK_MODE": mode}):
                response = self.hook()
                self.assert_fallback(response)
                self.assertEqual(self.marker().status, "failed")

    def test_prompt_timeout_discards_stdout_and_reaps_descendants(self):
        self.require_hooks()
        self.hook("hook-status")
        runner = self.module.run_child
        def bounded(argv, **kwargs):
            if "prompt-hook" in argv:
                kwargs["timeout"] = min(kwargs["timeout"], .3)
            return runner(argv, **kwargs)
        pid_path = self.base / "descendant-pid"
        for mode in ("prompt-sleep", "prompt-descendant"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"HOOK_MODE": mode, "HOOK_PID": str(pid_path)}), patch.object(self.module, "run_child", side_effect=bounded):
                response = self.hook()
                self.assert_fallback(response)
                self.assertIn("timed out", json.dumps(response))
                self.assertEqual(self.marker().status, "failed")
        pid = int(pid_path.read_text())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            if sys.platform.startswith("linux"):
                self.assertIn((Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0], {"Z", "X"})
            else:
                self.fail("prompt descendant survived cleanup")

    def test_all_hook_children_share_the_finite_overall_deadline(self):
        self.require_hooks()
        runner = self.module.run_child
        start = time.monotonic()
        expiries = []
        def inspect(argv, **kwargs):
            expiries.append(time.monotonic() + kwargs["timeout"])
            return runner(argv, **kwargs)
        with patch.object(self.module, "run_child", side_effect=inspect):
            response = self.hook()
        self.assertIn("FRESH PROMPT CONTEXT", json.dumps(response))
        self.assertGreater(len(expiries), 10)
        self.assertTrue(all(start + 44 < end < start + 46 for end in expiries))
        self.assertLess(max(expiries) - min(expiries), .1)

    def test_malformed_claude_codex_cli_payloads_exit_zero_with_fallback_json(self):
        for command, event in (("hook-status", "SessionStart"), ("hook-prompt", "UserPromptSubmit"), ("hook-update", "PostToolUse")):
            for payload in ("{broken", "[]", "null", '{"cwd": null}', '{"cwd": 42}', '{"cwd": "\\u0000"}'):
                with self.subTest(command=command, payload=payload):
                    result = subprocess.run([sys.executable, "-B", str(PACKAGE / "scripts/code_intel.py"), command], input=payload, cwd=self.repo, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assert_fallback(json.loads(result.stdout), event)
        self.assertEqual(self.commands(), [])

    def test_prompt_context_rejects_unstructured_or_json_output(self):
        self.require_hooks()
        self.hook("hook-status")
        runner = self.module.run_child
        for value in ([], {}, {"hookSpecificOutput": []}, {"hookSpecificOutput": {"additionalContext": 42}}):
            def output(argv, **kwargs):
                result = runner(argv, **kwargs)
                return subprocess.CompletedProcess(argv, 0, json.dumps(value), "") if "prompt-hook" in argv else result
            with self.subTest(value=value), patch.object(self.module, "run_child", side_effect=output):
                self.assert_fallback(self.hook())
                self.assertEqual(self.marker().status, "failed")

    def test_real_hook_cli_holds_lock_until_prompt_completion_against_explicit_commands(self):
        self.require_hooks()
        self.hook("hook-status")
        for command in ("setup-project", "update-project"):
            with self.subTest(command=command):
                notify = self.base / (command + "-notify")
                release = self.base / (command + "-release")
                os.mkfifo(notify)
                os.mkfifo(release)
                notify_fd = os.open(notify, os.O_RDWR | os.O_NONBLOCK)
                release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
                env = {**os.environ, "HOOK_MODE": "prompt-barrier", "HOOK_NOTIFY": str(notify), "HOOK_RELEASE": str(release)}
                hook = subprocess.Popen([sys.executable, "-B", str(PACKAGE / "scripts/code_intel.py"), "hook-prompt"], cwd=self.repo, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                updater = None
                try:
                    hook.stdin.write(json.dumps({"cwd": str(self.repo), "prompt": "explain"}))
                    hook.stdin.close()
                    hook.stdin = None
                    self.assertTrue(select.select([notify_fd], [], [], 10)[0], "prompt did not enter barrier")
                    self.assertEqual(os.read(notify_fd, 100), b"prompt\n")
                    self.events.write_text("")
                    updater = subprocess.Popen([sys.executable, "-B", str(PACKAGE / "scripts/code_intel.py"), command, str(self.repo)], cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    time.sleep(.25)
                    self.assertIsNone(updater.poll())
                    self.assertEqual(self.commands(), [])
                    data = self.module.select_data_location(os.environ, read_only=True)
                    state = self.module.state_path(self.repo, data)
                    before = state.read_bytes()
                    with self.assertRaises(self.module.UserError):
                        with self.ready(deadline=time.monotonic() + .2):
                            pass
                    self.assertEqual(state.read_bytes(), before)
                    os.write(release_fd, b"release\n")
                    out, err = hook.communicate(timeout=15)
                    self.assertEqual(hook.returncode, 0, err)
                    self.assertIn("FRESH PROMPT CONTEXT", json.dumps(json.loads(out)))
                    _out, err = updater.communicate(timeout=15)
                    self.assertEqual(updater.returncode, 0, err)
                    self.assertEqual(self.commands(), ["sync", "update"])
                    self.assertEqual(self.marker().status, "success")
                finally:
                    os.close(notify_fd)
                    os.close(release_fd)
                    for process in (hook, updater):
                        if process is not None:
                            if process.poll() is None:
                                process.kill()
                            process.communicate(timeout=5)


class ExplicitTargetTests(HookCase):
    def test_explicit_commands_reject_wrong_versions_at_target_root(self):
        self.warm()
        for executable in ("codegraph", "code-review-graph"):
            for command in ("setup-project", "setup-batch", "update-project", "update-batch", "project-status"):
                self.events.write_text("")
                with self.subTest(executable=executable, command=command), patch.dict(os.environ, {
                    "HOOK_WRONG_VERSION_TOOL": executable,
                    "HOOK_WRONG_VERSION_CWD": str(self.repo.resolve()),
                }):
                    result = self.cli(command, self.repo, cwd=self.base)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(executable + " version 9.9.9", result.stdout + result.stderr)
                self.assertEqual(self.commands(), [])

    def test_explicit_commands_use_canonical_target_when_caller_has_wrong_version(self):
        alias = self.base / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        (self.repo / "nested").mkdir()
        for executable in ("codegraph", "code-review-graph"):
            for command in ("setup-project", "setup-batch", "update-project", "update-batch", "project-status"):
                with self.subTest(executable=executable, command=command), patch.dict(os.environ, {
                    "HOOK_WRONG_VERSION_TOOL": executable,
                    "HOOK_WRONG_VERSION_CWD": str(self.base.resolve()),
                }):
                    result = self.cli(command, alias / "nested", cwd=self.base)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.marker().versions, {"codegraph": "1.6.0", "crg": "2.3.8"})

    def test_capture_does_not_stamp_configured_pins_over_observed_versions(self):
        self.warm()
        tools = {name: self.module.resolve_verified_tool(spec, deadline=time.monotonic() + 5,
                  cwd=self.repo) for name, spec in self.module.TOOLS.items()}
        with patch.dict(os.environ, {"HOOK_VERSION": "9.9.9"}), self.assertRaisesRegex(self.module.UserError, "version 9.9.9"):
            self.module.capture(self.repo, tools, time.monotonic() + 10)


class NoInstallSideEffectsTests(HookCase):
    def setUp(self):
        super().setUp()
        directory = Path(os.environ["PATH"].split(os.pathsep)[0])
        existing = (directory / "codegraph").read_text().split("\n", 1)[1]
        self.executable(directory, "codegraph",
            "import json, os, sys\nfrom pathlib import Path\n"
            "if os.environ.get('NO_INSTALL_PROBE'):\n"
            " probe = Path(os.environ['NO_INSTALL_PROBE'])\n"
            " if not os.environ.get('CODEGRAPH_NO_DOWNLOAD'): (probe / 'download-attempt').touch()\n"
            " cache = Path(os.environ.get('CODEGRAPH_INSTALL_DIR') or str(probe / 'fallback'))\n"
            " old = cache / 'bundles' / 'old-version'\n"
            " if old.is_file(): old.unlink()\n"
            " if os.environ.get('NODE_DISABLE_COMPILE_CACHE') != '1':\n"
            "  compiled = Path(os.environ['NODE_COMPILE_CACHE']); compiled.mkdir(exist_ok=True)\n"
            "  (compiled / 'compiled-bytecode').touch()\n"
            "if sys.argv[1:2] == ['serve']:\n"
            " print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {}})); sys.exit(0)\n"
            + existing)
        self.probe = self.base / "protected"
        (self.probe / "fallback/bundles").mkdir(parents=True)
        self.probe_env = {
            "NO_INSTALL_PROBE": str(self.probe),
            "CODEGRAPH_NO_DOWNLOAD": "",
            "CODEGRAPH_INSTALL_DIR": str(self.probe / "fallback"),
            "NODE_DISABLE_COMPILE_CACHE": "",
            "NODE_COMPILE_CACHE": str(self.probe / "compile-cache"),
        }

    def test_all_noninstall_entrypoints_prevent_launcher_cache_mutations(self):
        self.warm()
        cases = [
            ("doctor",), ("project-status", str(self.repo)),
            ("setup-project", str(self.repo)), ("setup-batch", str(self.repo)),
            ("update-project", str(self.repo)), ("update-batch", str(self.repo)),
            ("hook-status",), ("hook-prompt",), ("hook-update",), ("serve", "codegraph"),
        ]
        for args in cases:
            (self.probe / "download-attempt").unlink(missing_ok=True)
            (self.probe / "compile-cache/compiled-bytecode").unlink(missing_ok=True)
            if (self.probe / "compile-cache").exists():
                (self.probe / "compile-cache").rmdir()
            (self.probe / "fallback/bundles/old-version").write_text("keep cached fallback")
            before = snapshot(self.probe)
            with self.subTest(command=args[0]):
                result = subprocess.run(
                    [sys.executable, "-B", str(PACKAGE / "scripts/code_intel.py"), *args],
                    cwd=self.repo, env={**os.environ, **self.probe_env},
                    input=json.dumps({"cwd": str(self.repo), "prompt": "explain source", "tool_name": "Write"}),
                    capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(snapshot(self.probe), before)
                if args[0] == "serve":
                    self.assertEqual(json.loads(result.stdout), {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_explicit_install_tools_allows_dependency_installation(self):
        directory = Path(os.environ["PATH"].split(os.pathsep)[0])
        self.executable(directory, "mise", "pass\n")
        with patch.dict(os.environ, self.probe_env):
            self.assertEqual(self.module.install_tools(), 0)
        self.assertTrue((self.probe / "download-attempt").is_file())


class RealCodeGraphTests(ControllerCase):
    def setUp(self):
        super().setUp()
        selected = os.environ.get("CODE_INTEL_REAL_CODEGRAPH")
        if not selected:
            self.skipTest("set CODE_INTEL_REAL_CODEGRAPH to the installed pinned 1.6.0 npm launcher")
        self.launcher = Path(selected).resolve()
        self.metadata = self.launcher.parent / "package.json"
        self.assertEqual(json.loads(self.metadata.read_text())["version"], "1.6.0")

    def test_real_missing_optional_bundle_never_downloads_or_prunes_cached_fallback(self):
        # Exercise the unmodified 1.6.0 npm shim outside its optional dependency.
        directory = self.base / "isolated launcher"
        directory.mkdir()
        binary = directory / "codegraph"
        shutil.copy2(self.launcher, binary)
        shutil.copy2(self.metadata, directory / "package.json")
        probe = self.base / "protected cache"
        probe.mkdir()
        network_guard = self.base / "deny-network.js"
        network_guard.write_text(
            "require('https').get = function () { require('fs').writeFileSync("
            + json.dumps(str(probe / "network-attempt"))
            + ", 'blocked'); throw new Error('test blocked network'); };\n")
        target = subprocess.run(["node", "-p", "process.platform + '-' + process.arch"],
            check=True, capture_output=True, text=True).stdout.strip()
        for cached in (False, True):
            cache = probe / ("cached" if cached else "uncached")
            cache.mkdir()
            if cached:
                self.executable(cache / f"bundles/{target}-1.6.0/bin", "codegraph", "print('1.6.0')\n")
                (cache / f"bundles/{target}-1.5.0").mkdir()
                (cache / f"bundles/{target}-1.5.0/keep").write_text("old bundle must survive")
            env = {**os.environ, "PATH": str(directory) + os.pathsep + os.environ["PATH"],
                "CODEGRAPH_NO_DOWNLOAD": "", "CODEGRAPH_INSTALL_DIR": str(cache),
                "NODE_DISABLE_COMPILE_CACHE": "", "NODE_COMPILE_CACHE": str(probe / "compile-cache"),
                "NODE_OPTIONS": "--require=" + json.dumps(str(network_guard)), "NODE_PATH": ""}
            before = snapshot(probe)
            with self.subTest(cached=cached):
                result = subprocess.run([sys.executable, "-B", str(PACKAGE / "scripts/code_intel.py"),
                    "serve", "codegraph"], cwd=self.repo, env=env, capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("network fallback is disabled", result.stderr)
                self.assertIn("install-tools", result.stderr)
                self.assertEqual(snapshot(probe), before)

    def test_real_installed_version_check_does_not_write_node_compile_cache(self):
        compile_cache = self.base / "compile-cache"
        before = snapshot(self.base)
        with patch.dict(os.environ, {"NODE_COMPILE_CACHE": str(compile_cache)}):
            os.environ.pop("NODE_DISABLE_COMPILE_CACHE", None)
            result = self.module.run_child([str(self.launcher), "--version"], cwd=self.repo, timeout=15)
        self.assertEqual(result.stdout.strip(), "1.6.0")
        self.assertEqual(snapshot(self.base), before)

    def test_real_raw_prompt_and_noop_adapter_contract(self):
        (self.repo / "source.py").write_text("def traced_function(value):\n    return value + 1\n")
        self.module.run_child([str(self.launcher), "init", str(self.repo)], cwd=self.repo, timeout=30)
        directory = self.base / "real-codegraph-bin"
        self.executable(directory, "code-review-graph", "import sys\nfrom pathlib import Path\n"
            "if sys.argv[1:] == ['--version']: print('2.3.8')\n"
            "else: (Path.cwd() / '.code-review-graph').mkdir(exist_ok=True)\n")
        (directory / "codegraph").symlink_to(self.launcher)
        for prompt, expected_raw in (("trace callers of traced_function", True), ("", False)):
            result = self.module.run_child([str(self.launcher), "prompt-hook"], cwd=self.repo, timeout=20,
                input_text=json.dumps({"cwd": str(self.repo), "prompt": prompt}))
            with self.subTest(prompt=prompt):
                self.assertEqual(bool(result.stdout), expected_raw)
                context = self.module.extract_prompt_context(result.stdout)
                self.assertEqual(context, result.stdout.strip())
                if expected_raw:
                    self.assertIn("<codegraph_context ", context)
                    self.assertIn("traced_function", context)
                with patch.dict(os.environ, {"PATH": str(directory) + os.pathsep + os.environ["PATH"]}):
                    response = self.module.handle_hook("hook-prompt", {"cwd": str(self.repo), "prompt": prompt})
                adapted = response["hookSpecificOutput"]["additionalContext"]
                self.assertIn("Use CodeGraph first", adapted)
                self.assertEqual("<codegraph_context " in adapted, expected_raw)

    def test_real_force_rebuild_recreates_existing_database(self):
        self.module.run_child([str(self.launcher), "init", str(self.repo)], cwd=self.repo, timeout=30)
        database = self.repo / ".codegraph/codegraph.db"
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE review_sentinel (value TEXT)")
        self.module.initialize_indexes_locked(self.repo, {"codegraph": self.launcher},
            force=True, deadline=time.monotonic() + 30)
        with contextlib.closing(sqlite3.connect(database)) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("review_sentinel", names)
        self.assertIn("nodes", names)

    def test_real_force_setup_initializes_retained_directory_without_database(self):
        (self.repo / ".codegraph").mkdir()
        try:
            self.module.initialize_indexes_locked(self.repo, {"codegraph": self.launcher},
                force=True, deadline=time.monotonic() + 30)
        except self.module.UserError as exc:
            self.fail(str(exc))
        self.assertTrue((self.repo / ".codegraph/codegraph.db").is_file())


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
        self.assertTrue(all(call.kwargs == {"cwd": None, "timeout": 300, "allow_install": True} for call in child.call_args_list))
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
            with self.subTest(engine=engine), patch.object(module, "resolve_verified_tool", return_value=Path("/tools/code graph;$x")), patch.object(module.os, "execve") as execute:
                module.main(["serve", engine])
            self.assertEqual(execute.call_args.args[:2], ("/tools/code graph;$x", ["/tools/code graph;$x", *args]))
            self.assertEqual(execute.call_args.args[2]["CODEGRAPH_NO_DOWNLOAD"], "1")
            self.assertEqual(execute.call_args.args[2]["NODE_DISABLE_COMPILE_CACHE"], "1")
            self.assertEqual(execute.call_args.args[2]["CODEGRAPH_INSTALL_DIR"], os.devnull)
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


class DiscoveryTests(ControllerCase):
    def make_repo(self, relative):
        root = self.base / relative
        root.mkdir(parents=True)
        git(root, "init", "-q")
        return root

    def test_normal_repository_uses_canonical_checkout_root(self):
        nested = self.repo / "nested"
        nested.mkdir()

        scope = self.module.discover_scope(
            nested / "..", deadline=time.monotonic() + 10
        )

        self.assertEqual(scope.kind, "repository")
        self.assertEqual(scope.root, self.repo.resolve())
        self.assertEqual(scope.repositories, (self.repo.resolve(),))

    def test_linked_worktree_keeps_its_own_identity(self):
        worktree = self.base / "linked worktree;$x"
        git(self.repo, "worktree", "add", "-qb", "linked-test", str(worktree))

        scope = self.module.discover_scope(
            worktree, deadline=time.monotonic() + 10
        )

        self.assertEqual(scope.kind, "worktree")
        self.assertEqual(scope.root, worktree.resolve())
        self.assertEqual(scope.repositories, (worktree.resolve(),))
        self.assertNotEqual(scope.root, (self.repo / ".git").resolve())

    def test_umbrella_setup_is_read_only_sorted_and_codegraph_only_at_root(self):
        first = self.make_repo("a repo;$(touch UNEXPECTED)")
        last = self.make_repo("z repo;$HOME")
        (self.base / "AGENTS.md").write_text("umbrella\n")
        before = snapshot(self.base)

        scope = self.module.discover_scope(
            self.base, deadline=time.monotonic() + 10
        )

        expected_repositories = tuple(
            sorted((first.resolve(), self.repo.resolve(), last.resolve()))
        )
        self.assertEqual(scope.kind, "umbrella")
        self.assertEqual(scope.root, self.base.resolve())
        self.assertEqual(scope.repositories, expected_repositories)
        self.assertEqual(
            self.module.setup_roots(scope),
            tuple((root, ("codegraph", "crg")) for root in expected_repositories)
            + ((self.base.resolve(), ("codegraph",)),),
        )
        self.assertEqual(snapshot(self.base), before)
        self.assertFalse((self.base / "UNEXPECTED").exists())

    def test_single_nested_repository_is_an_umbrella(self):
        parent = self.base / "single parent"
        child = self.make_repo("single parent/child")
        (parent / "AGENTS.md").write_text("umbrella\n")

        scope = self.module.discover_scope(
            parent, deadline=time.monotonic() + 10
        )

        self.assertEqual(scope.kind, "umbrella")
        self.assertEqual(scope.repositories, (child.resolve(),))

    def test_umbrella_does_not_require_an_ai_marker(self):
        parent = self.base / "markerless"
        first = self.make_repo("markerless/first")
        second = self.make_repo("markerless/second")

        scope = self.module.discover_scope(
            parent, deadline=time.monotonic() + 10
        )

        self.assertEqual(scope.kind, "umbrella")
        self.assertEqual(
            scope.repositories, tuple(sorted((first.resolve(), second.resolve())))
        )

    def test_deeply_nested_repository_is_discovered(self):
        parent = self.base / "deep parent"
        child = self.make_repo("deep parent/a/b/c/d/child")
        (parent / "AGENTS.md").write_text("umbrella\n")

        scope = self.module.discover_scope(
            parent, deadline=time.monotonic() + 10
        )

        self.assertEqual(scope.kind, "umbrella")
        self.assertEqual(scope.repositories, (child.resolve(),))

    def test_discovery_does_not_descend_into_repository_internals(self):
        parent = self.base / "outer parent"
        outer = self.make_repo("outer parent/repository")
        self.make_repo("outer parent/repository/vendor-source")

        scope = self.module.discover_scope(
            parent, deadline=time.monotonic() + 10
        )

        self.assertEqual(scope.kind, "umbrella")
        self.assertEqual(scope.repositories, (outer.resolve(),))

    def test_unrelated_non_git_directory_has_no_scope(self):
        unrelated = self.base / "unrelated"
        unrelated.mkdir()

        scope = self.module.discover_scope(
            unrelated, deadline=time.monotonic() + 10
        )

        self.assertEqual(
            scope,
            self.module.RepoScope("none", unrelated.resolve(), ()),
        )
        with self.assertRaisesRegex(self.module.UserError, "No Git repository"):
            self.module.setup_roots(scope)

    def test_discovery_prunes_generated_and_symlink_directories(self):
        candidate = self.base / "candidate"
        candidate.mkdir()
        (candidate / "AGENTS.md").write_text("umbrella\n")
        hidden = candidate / ".codegraph" / "hidden"
        hidden.mkdir(parents=True)
        git(hidden, "init", "-q")
        (candidate / "linked").symlink_to(self.repo, target_is_directory=True)

        scope = self.module.discover_scope(
            candidate, deadline=time.monotonic() + 10
        )

        self.assertEqual(scope.kind, "none")
        self.assertEqual(scope.repositories, ())

    def test_discovery_refuses_an_expired_deadline(self):
        with self.assertRaisesRegex(self.module.UserError, "deadline"):
            self.module.discover_scope(
                self.repo, deadline=time.monotonic() - 1
            )


class IndexCommandTests(ControllerCase):
    def test_initialization_runs_codegraph_before_crg_with_literal_argv(self):
        module = self.module
        tools = {
            "crg": Path("/tools/crg $HOME"),
            "codegraph": Path("/tools/codegraph;$(touch UNEXPECTED)"),
        }
        with patch.object(module, "ensure_local_excludes") as excludes, patch.object(
            module,
            "run_child",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as child:
            module.initialize_indexes_locked(
                self.repo, tools, force=False, deadline=time.monotonic() + 10
            )

        self.assertEqual(
            [call.args[0] for call in child.call_args_list],
            [
                [str(tools["codegraph"]), "init", str(self.repo.resolve())],
                [str(tools["crg"]), "build", "--repo", str(self.repo.resolve())],
            ],
        )
        self.assertTrue(
            all(call.kwargs["cwd"] == self.repo.resolve() for call in child.call_args_list)
        )
        self.assertTrue(
            all(0 < call.kwargs["timeout"] <= 10 for call in child.call_args_list)
        )
        excludes.assert_called_once_with(
            self.repo.resolve(), deadline=unittest.mock.ANY
        )
        self.assertFalse((self.repo / "UNEXPECTED").exists())

    def test_nonforced_initialization_skips_existing_indexes_but_force_rebuilds(self):
        module = self.module
        (self.repo / ".codegraph").mkdir()
        (self.repo / ".codegraph/codegraph.db").write_text("existing index")
        (self.repo / ".code-review-graph").mkdir()
        tools = {"codegraph": Path("/cg"), "crg": Path("/crg")}
        with patch.object(module, "ensure_local_excludes"), patch.object(
            module,
            "run_child",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as child:
            module.initialize_indexes_locked(
                self.repo, tools, force=False, deadline=time.monotonic() + 10
            )
            self.assertEqual(child.call_args_list, [])
            module.initialize_indexes_locked(
                self.repo, tools, force=True, deadline=time.monotonic() + 10
            )

        self.assertEqual(
            [call.args[0] for call in child.call_args_list],
            [
                ["/cg", "index", str(self.repo.resolve())],
                ["/crg", "build", "--repo", str(self.repo.resolve())],
            ],
        )

    def test_codegraph_only_umbrella_never_writes_git_excludes(self):
        module = self.module
        umbrella = self.base / "umbrella"
        umbrella.mkdir()
        with patch.object(module, "ensure_local_excludes") as excludes, patch.object(
            module,
            "run_child",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as child:
            module.initialize_indexes_locked(
                umbrella,
                {"codegraph": Path("/cg")},
                force=False,
                deadline=time.monotonic() + 10,
            )

        child.assert_called_once()
        self.assertEqual(
            child.call_args.args[0], ["/cg", "init", str(umbrella.resolve())]
        )
        excludes.assert_not_called()

    def test_update_project_refuses_missing_indexes(self):
        for tools in ({}, {"codegraph": Path("/cg")}, {"crg": Path("/crg")}):
            with self.subTest(tools=tools), patch.object(
                self.module, "run_child"
            ) as child:
                with self.assertRaisesRegex(self.module.UserError, "setup-project"):
                    self.module.update_indexes_locked(
                        self.repo, tools, deadline=time.monotonic() + 10
                    )
                child.assert_not_called()

    def test_update_runs_codegraph_then_incremental_crg(self):
        module = self.module
        (self.repo / ".codegraph").mkdir()
        (self.repo / ".code-review-graph").mkdir()
        tools = {"crg": Path("/crg space;$x"), "codegraph": Path("/cg")}
        with patch.object(module, "ensure_local_excludes"), patch.object(
            module,
            "run_child",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as child:
            module.update_indexes_locked(
                self.repo, tools, deadline=time.monotonic() + 10
            )

        self.assertEqual(
            [call.args[0] for call in child.call_args_list],
            [
                ["/cg", "sync", str(self.repo.resolve())],
                [
                    "/crg space;$x",
                    "update",
                    "--skip-flows",
                    "--repo",
                    str(self.repo.resolve()),
                ],
            ],
        )

    def test_crg_only_update_is_incremental(self):
        module = self.module
        (self.repo / ".code-review-graph").mkdir()
        with patch.object(module, "ensure_local_excludes"), patch.object(
            module,
            "run_child",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as child:
            module.update_indexes_locked(
                self.repo,
                {"crg": Path("/crg")},
                deadline=time.monotonic() + 10,
            )

        child.assert_called_once()
        self.assertEqual(
            child.call_args.args[0],
            [
                "/crg",
                "update",
                "--skip-flows",
                "--repo",
                str(self.repo.resolve()),
            ],
        )

    def test_local_excludes_preserve_existing_content_and_are_idempotent(self):
        gitignore = self.repo / ".gitignore"
        gitignore.write_text("tracked-global-rule\n")
        exclude_output = git(self.repo, "rev-parse", "--git-path", "info/exclude")
        exclude = Path(exclude_output)
        if not exclude.is_absolute():
            exclude = self.repo / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = "# existing local rule\nlocal-only"
        exclude.write_text(existing)

        self.module.ensure_local_excludes(
            self.repo, deadline=time.monotonic() + 10
        )
        once = exclude.read_text()
        self.module.ensure_local_excludes(
            self.repo, deadline=time.monotonic() + 10
        )

        self.assertTrue(once.startswith(existing + "\n"))
        self.assertEqual(once.count(".codegraph/\n"), 1)
        self.assertEqual(once.count(".code-review-graph/\n"), 1)
        self.assertEqual(exclude.read_text(), once)
        self.assertEqual(gitignore.read_text(), "tracked-global-rule\n")

    def test_local_excludes_preserve_non_utf8_bytes(self):
        exclude_output = git(self.repo, "rev-parse", "--git-path", "info/exclude")
        exclude = Path(exclude_output)
        if not exclude.is_absolute():
            exclude = self.repo / exclude
        prefix = b"\xff\xfe# arbitrary local bytes\nlocal-only"
        exclude.write_bytes(prefix)

        try:
            self.module.ensure_local_excludes(
                self.repo, deadline=time.monotonic() + 10
            )
        except self.module.UserError as exc:
            self.fail(f"valid Git exclude bytes were rejected: {exc}")
        once = exclude.read_bytes()
        self.module.ensure_local_excludes(
            self.repo, deadline=time.monotonic() + 10
        )

        self.assertEqual(
            once,
            prefix + b"\n.codegraph/\n.code-review-graph/\n",
        )
        self.assertEqual(exclude.read_bytes(), once)

    def test_remaining_refuses_expired_deadline(self):
        self.assertGreater(
            self.module.remaining(time.monotonic() + 1), 0
        )
        with self.assertRaisesRegex(self.module.UserError, "deadline"):
            self.module.remaining(time.monotonic() - 1)


class RealCRGRollbackTests(ControllerCase):
    def setUp(self):
        super().setUp()
        selected = os.environ.get("CODE_INTEL_REAL_CRG")
        if not selected:
            self.skipTest("set CODE_INTEL_REAL_CRG to the installed pinned 2.3.8 executable")
        selected = str(Path(selected).absolute())  # Preserve a supplied mise shim's name.
        self.repo = self.repo.resolve()
        from test_packaging import stage_installed_copy
        self.installed_root = stage_installed_copy(self.base)
        directory = self.base / "real-crg-bin"
        self.executable(directory, "code-review-graph",
            f"import os, sys\nos.execv({selected!r}, [{selected!r}, *sys.argv[1:]])\n")
        self.executable(directory, "codegraph",
            "import sys\nfrom pathlib import Path\n"
            "if sys.argv[1:] == ['--version']: print('1.6.0')\n"
            "elif sys.argv[1] in ('init', 'index'): (Path.cwd() / '.codegraph').mkdir(exist_ok=True)\n"
            "elif sys.argv[1] not in ('sync', 'prompt-hook'): sys.exit(9)\n")
        env = patch.dict(os.environ, {
            "PATH": str(directory) + os.pathsep + os.environ["PATH"],
            "CRG_HOME": str(self.base / "crg-home"),
            "CRG_DATA_DIR": "", "CRG_SERIAL_PARSE": "1",
            "XDG_CONFIG_HOME": str(self.base / "xdg-config"),
            "XDG_DATA_HOME": str(self.base / "xdg-data"),
            "XDG_CACHE_HOME": str(self.base / "xdg-cache"),
            "XDG_STATE_HOME": str(self.base / "xdg-state"),
        })
        env.start()
        self.addCleanup(env.stop)
        version = self.module.run_child([selected, "--version"], cwd=self.repo, timeout=15)
        self.assertEqual(version.stdout.strip(), "code-review-graph 2.3.8")

    def installed(self, command, *, payload=None):
        args = [sys.executable, "-B", str(self.installed_root / "scripts/code_intel.py"), command]
        if command in ("update-project", "project-status"):
            args.append(str(self.repo))
        outer_timeout = {
            "hook-status": 75, "hook-prompt": 75, "hook-update": 75,
            "update-project": 330, "project-status": 330,
        }[command]
        return self.module.run_child(
            args, cwd=self.repo, timeout=outer_timeout,
            input_text=None if payload is None else json.dumps(payload),
        )

    def hook_sync(self, command="hook-update"):
        result = self.installed(command, payload={
            "cwd": str(self.repo), "tool_name": "Bash", "prompt": "",
        })
        self.assertNotIn("Code intelligence unavailable", result.stdout)
        return result

    def sql(self, statement, args=()):
        database = self.repo / ".code-review-graph/graph.db"
        wal = database.with_name(database.name + "-wal")
        self.assertFalse(wal.exists() and wal.stat().st_size,
                         "immutable SQL fixture would ignore nonempty WAL")
        with contextlib.closing(sqlite3.connect(
                database.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            return connection.execute(statement, args).fetchall()

    def count_symbol(self, name):
        return self.sql("SELECT COUNT(*) FROM nodes WHERE name = ?", (name,))[0][0]

    def fixture(self):
        (self.repo / "a.py").write_text("def stable_a():\n    return 1\n")
        (self.repo / "b.py").write_text("def stable_b():\n    return 2\n")
        git(self.repo, "add", "a.py", "b.py")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "rollback fixture")
        self.hook_sync("hook-status")

    def dirty_a(self):
        (self.repo / "a.py").write_text("def stable_a():\n    return 1\n\ndef ghost():\n    return 9\n")

    def assert_current_a(self):
        self.assertEqual(self.count_symbol("ghost"), 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM nodes_fts WHERE name = ?", ("ghost",)), [(0,)])
        self.assertEqual(self.count_symbol("stable_a"), 1)
        self.assertEqual(self.sql("SELECT DISTINCT file_hash FROM nodes WHERE file_path = ?",
                                 (str(self.repo / "a.py"),)),
                         [(hashlib.sha256((self.repo / "a.py").read_bytes()).hexdigest(),)])

    def partial_restore(self):
        before_head = git(self.repo, "rev-parse", "HEAD")
        self.dirty_a()
        (self.repo / "b.py").write_text("def stable_b():\n    return 2\n\ndef dirty_b():\n    return 8\n")
        self.hook_sync()
        self.assertEqual(self.count_symbol("ghost"), 1)
        self.assertEqual(self.count_symbol("dirty_b"), 1)
        git(self.repo, "restore", "--", "a.py")
        self.hook_sync()
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before_head)
        self.assert_current_a()
        self.assertEqual(self.count_symbol("dirty_b"), 1)
        report = json.loads(self.installed("project-status").stdout)
        self.assertIs(report["healthy"], True)

    def test_installed_partial_restore_removes_ghost(self):
        self.fixture()
        self.partial_restore()

    def test_installed_full_restore_and_reset(self):
        for operation in (("restore", "--", "a.py"), ("reset", "--hard", "HEAD")):
            with self.subTest(operation=operation):
                self.repo = (self.base / operation[0]).resolve()
                self.repo.mkdir()
                git(self.repo, "init", "-q")
                self.fixture()
                head = git(self.repo, "rev-parse", "HEAD")
                self.dirty_a()
                self.hook_sync()
                self.assertEqual(self.count_symbol("ghost"), 1)
                git(self.repo, *operation)
                self.hook_sync()
                self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
                self.assert_current_a()

    def test_installed_restored_tracked_deletion(self):
        self.fixture()
        self.assertEqual(self.count_symbol("stable_a"), 1)
        git(self.repo, "rm", "a.py")
        self.hook_sync()
        self.assertEqual(self.count_symbol("stable_a"), 0)
        git(self.repo, "restore", "--source=HEAD", "--staged", "--worktree", "--", "a.py")
        self.installed("update-project")
        self.assert_current_a()

    def test_installed_restore_survives_branch_switch(self):
        self.fixture()
        branch = git(self.repo, "branch", "--show-current")
        git(self.repo, "switch", "-qc", "other")
        (self.repo / "b.py").write_text("def other_b():\n    return 3\n")
        git(self.repo, "add", "b.py")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "other B")
        git(self.repo, "switch", "-q", branch)
        self.dirty_a()
        self.hook_sync()
        self.assertEqual(self.count_symbol("ghost"), 1)
        git(self.repo, "restore", "--", "a.py")
        git(self.repo, "switch", "-q", "other")
        self.hook_sync("hook-prompt")
        self.assert_current_a()
        self.assertEqual(self.count_symbol("other_b"), 1)
        self.assertEqual(self.count_symbol("stable_b"), 0)

    def test_installed_repair_preserves_indexed_untracked(self):
        self.fixture()
        local = self.repo / "local.py"
        local.write_text("def untracked_only():\n    return 4\n")
        git(self.repo, "add", "local.py")
        self.module.run_child(["code-review-graph", "update", "--skip-flows", "--repo", str(self.repo)],
                              cwd=self.repo, timeout=300)
        git(self.repo, "reset", "--", "local.py")
        row = self.sql("SELECT * FROM nodes WHERE name = ?", ("untracked_only",))
        self.assertEqual(len(row), 1)
        before = local.read_bytes()
        self.partial_restore()
        self.assertEqual(self.sql("SELECT * FROM nodes WHERE name = ?", ("untracked_only",)), row)
        self.assertEqual(git(self.repo, "ls-files", "--others", "--exclude-standard"), "local.py")
        self.assertEqual(local.read_bytes(), before)

    def test_installed_sha256_restore(self):
        self.repo = (self.base / "sha256").resolve()
        self.repo.mkdir()
        result = subprocess.run(["git", "init", "-q", "--object-format=sha256", str(self.repo)],
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            self.skipTest("Git rejects SHA-256 initialization: " + result.stderr)
        self.fixture()
        self.assertEqual(len(git(self.repo, "rev-parse", "HEAD")), 64)
        self.partial_restore()


if __name__ == "__main__":
    unittest.main()
