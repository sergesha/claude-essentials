import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.dont_write_bytecode = True
PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parent

EXPECTED_DISTRIBUTABLE_FILES = frozenset({
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".mcp.json",
    "hooks/hooks.json",
    "skills/code-intel/SKILL.md",
    "skills/code-intel/agents/openai.yaml",
    "scripts/code_intel.py",
    "tests/test_code_intel.py",
    "tests/test_packaging.py",
    "CHANGELOG.md",
})


def repository_distributable_files():
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "code-intel"],
        cwd=REPO, check=True, capture_output=True, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    ).stdout.splitlines()
    result = set()
    for path in listed:
        relative = Path(path).relative_to("code-intel")
        if "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}:
            continue
        result.add(relative.as_posix())
    return result


def validate_skill(skill_dir):
    errors = []
    text = (skill_dir / "SKILL.md").read_text()
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        return ["SKILL.md must start with YAML frontmatter"]
    end = lines.index("---", 1)
    frontmatter = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator:
            frontmatter[key.strip()] = value.strip().strip("\"'")
    if frontmatter.get("name") != "code-intel":
        errors.append("frontmatter name must be code-intel")
    description = frontmatter.get("description", "")
    if not description or not description.startswith("Use when "):
        errors.append("frontmatter description must state implicit trigger conditions")
    if "disable-model-invocation: true" in text:
        errors.append("the shared skill must allow model invocation")
    policy = (skill_dir / "agents/openai.yaml").read_text()
    if not re.search(r"(?m)^\s*allow_implicit_invocation:\s*true\s*$", policy):
        errors.append("agents/openai.yaml must allow implicit invocation")
    return errors


def stage_installed_copy(destination):
    installed = destination / "installed plugin ; $x"
    for relative in EXPECTED_DISTRIBUTABLE_FILES:
        target = installed / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PACKAGE / relative, target)
    return installed


def write_fake_tool(bin_dir, executable, version):
    path = bin_dir / executable
    path.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json
        import os
        from pathlib import Path
        import sys

        NAME = {executable!r}
        VERSION = {version!r}
        args = sys.argv[1:]
        operation_log = os.environ.get("FAKE_TOOL_LOG")
        if operation_log and args != ["--version"] and args[:1] != ["serve"]:
            with open(operation_log, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({{"tool": NAME, "args": args}}) + "\\n")
        if args == ["--version"]:
            print(NAME + " " + VERSION)
        elif NAME == "codegraph" and args[:1] == ["init"]:
            (Path(args[1]) / ".codegraph").mkdir(exist_ok=True)
        elif NAME == "code-review-graph" and args[:1] == ["build"]:
            root = Path(args[args.index("--repo") + 1])
            (root / ".code-review-graph").mkdir(exist_ok=True)
        elif args[:1] in (["sync"], ["update"]):
            pass
        elif args[:1] == ["prompt-hook"]:
            print(json.dumps({{"hookSpecificOutput": {{
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "fake prompt context from fresh indexes",
            }}}}))
        elif args[:1] == ["serve"]:
            Path(os.environ["FAKE_SERVER_LOG"]).write_text(os.getcwd())
        else:
            raise SystemExit("unexpected fake-tool argv: " + repr(args))
        """))
    path.chmod(0o755)
    return path


def read_json(relative):
    return json.loads((REPO / relative).read_text())


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.claude = read_json("code-intel/.claude-plugin/plugin.json")
        self.codex = read_json("code-intel/.codex-plugin/plugin.json")

    def setUp_installed_copy(self):
        temp = tempfile.TemporaryDirectory(prefix="code intel installed ; ")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.installed = stage_installed_copy(self.base)
        self.consumer_repo = self.base / "unrelated consumer"
        self.consumer_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.consumer_repo, check=True)
        subprocess.run(["git", "config", "user.name", "Smoke Test"], cwd=self.consumer_repo, check=True)
        subprocess.run(["git", "config", "user.email", "smoke@example.invalid"], cwd=self.consumer_repo, check=True)
        (self.consumer_repo / "source.py").write_text("value = 1\n")
        subprocess.run(["git", "add", "source.py"], cwd=self.consumer_repo, check=True)
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "initial"], cwd=self.consumer_repo, check=True)
        self.bin_dir = self.base / "fake bin"
        self.bin_dir.mkdir()
        write_fake_tool(self.bin_dir, "codegraph", "1.6.0")
        write_fake_tool(self.bin_dir, "code-review-graph", "2.3.8")
        self.data_dir = self.base / "plugin data"
        self.data_dir.mkdir()
        self.operation_log = self.base / "fake-tool-operations.jsonl"

    def run_installed_copy(self, *args, payload=None, cwd=None, extra_env=None):
        env = {
            **os.environ,
            "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "PLUGIN_DATA": str(self.data_dir),
            "CLAUDE_PLUGIN_DATA": "",
            "FAKE_TOOL_LOG": str(self.operation_log),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, "-B", str(self.installed / "scripts/code_intel.py"), *args],
            cwd=cwd or self.consumer_repo, input=None if payload is None else json.dumps(payload),
            capture_output=True, text=True, timeout=20, env=env,
        )

    def test_exact_distributable_file_set(self):
        self.assertEqual(repository_distributable_files(), EXPECTED_DISTRIBUTABLE_FILES)

    def test_repository_owned_skill_validation(self):
        self.assertTrue((PACKAGE / "skills/code-intel/SKILL.md").is_file())
        self.assertTrue((PACKAGE / "skills/code-intel/agents/openai.yaml").is_file())
        self.assertEqual(validate_skill(PACKAGE / "skills/code-intel"), [])

    def test_skill_instruction_contract(self):
        self.assertTrue((PACKAGE / "skills/code-intel/SKILL.md").is_file())
        skill = (PACKAGE / "skills/code-intel/SKILL.md").read_text()
        for required in (
            "npm:@colbymchenry/codegraph@1.6.0",
            "pipx:code-review-graph@2.3.8",
            "install-tools",
            "Restart the host after installation.",
            "Request authorization before setup-project on a non-Git umbrella.",
            "Use CodeGraph first for symbol source, callers/callees, call paths, and dynamic dispatch.",
            "Use code-review-graph first for review, blast radius, impact, and affected flows.",
            "Use code-review-graph first for architecture, communities, semantic search, and refactoring.",
            "If the selected graph cannot answer, fall back to normal file/search tools.",
        ):
            self.assertIn(required, skill)
        for prohibited in (
            "code-intel-setup", "edit CLAUDE.md", "edit AGENTS.md",
            "edit user MCP configuration", "edit user hook configuration",
        ):
            self.assertNotIn(prohibited, skill)

    def test_installed_layout_smoke(self):
        self.assertEqual(repository_distributable_files(), EXPECTED_DISTRIBUTABLE_FILES)
        self.setUp_installed_copy()
        doctor = self.run_installed_copy("doctor")
        self.assertNotEqual(doctor.returncode, 0)
        json.loads(doctor.stdout)

        for engine in ("codegraph", "crg"):
            log = self.base / (engine + ".cwd")
            completed = self.run_installed_copy(
                "serve", engine, extra_env={"FAKE_SERVER_LOG": str(log)})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(log.read_text(), str(self.consumer_repo))

        status = self.run_installed_copy(
            "hook-status", payload={"hook_event_name": "SessionStart", "cwd": str(self.consumer_repo)})
        self.assertEqual(status.returncode, 0, status.stderr)
        json.loads(status.stdout)
        self.assertTrue((self.consumer_repo / ".codegraph").is_dir())
        self.assertTrue((self.consumer_repo / ".code-review-graph").is_dir())

        markers = [json.loads(path.read_text()) for path in self.data_dir.glob("*.json")]
        self.assertTrue(any(marker.get("root") == str(self.consumer_repo.resolve())
                            and marker.get("status") == "success" for marker in markers))
        trusted = self.run_installed_copy("project-status", str(self.consumer_repo))
        self.assertEqual(trusted.returncode, 0, trusted.stderr)
        json.loads(trusted.stdout)

        prompt = self.run_installed_copy(
            "hook-prompt", payload={"hook_event_name": "UserPromptSubmit",
                                    "prompt": "trace callers", "cwd": str(self.consumer_repo)})
        self.assertEqual(prompt.returncode, 0, prompt.stderr)
        prompt_response = json.loads(prompt.stdout)
        prompt_context = prompt_response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("fake prompt context from fresh indexes", prompt_context)

        def operations():
            if not self.operation_log.exists():
                return []
            return [json.loads(line) for line in self.operation_log.read_text().splitlines()]

        initial_operations = operations()
        for executable, operation in (("codegraph", "init"), ("codegraph", "sync"),
                                      ("code-review-graph", "build"), ("code-review-graph", "update")):
            self.assertIn((executable, operation),
                          {(item["tool"], item["args"][0]) for item in initial_operations})

        for tool_name, tool_input in (("Write", {"file_path": "source.py"}),
                                      ("Bash", {"command": "true"})):
            before = operations()
            completed = self.run_installed_copy(
                "hook-update", payload={"hook_event_name": "PostToolUse",
                                        "tool_name": tool_name, "tool_input": tool_input,
                                        "cwd": str(self.consumer_repo)})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            json.loads(completed.stdout)
            after = operations()
            for executable, operation in (("codegraph", "sync"),
                                          ("code-review-graph", "update")):
                self.assertEqual(
                    sum(item["tool"] == executable and item["args"][0] == operation
                        for item in after),
                    sum(item["tool"] == executable and item["args"][0] == operation
                        for item in before) + 1,
                    (tool_name, executable, operation, before, after),
                )

        self.assertEqual(
            {p.relative_to(self.installed).as_posix() for p in self.installed.rglob("*") if p.is_file()},
            EXPECTED_DISTRIBUTABLE_FILES,
        )

    def test_manifest_identity_and_shared_entrypoints(self):
        for field in (
            "name",
            "version",
            "description",
            "author",
            "homepage",
            "repository",
            "license",
        ):
            self.assertEqual(self.claude[field], self.codex[field])
        self.assertEqual(self.codex["name"], "code-intel")
        self.assertEqual(self.codex["skills"], "./skills/")
        self.assertEqual(self.codex["hooks"], "./hooks/hooks.json")
        self.assertEqual(self.codex["mcpServers"], "./.mcp.json")
        self.assertEqual(self.claude["hooks"], "./hooks/hooks.json")

    def test_mcp_servers_launch_both_engines_through_the_dispatcher(self):
        expected = {
            "codegraph": {
                "command": "python3",
                "args": [
                    "${CLAUDE_PLUGIN_ROOT}/scripts/code_intel.py",
                    "serve",
                    "codegraph",
                ],
            },
            "code-review-graph": {
                "command": "python3",
                "args": [
                    "${CLAUDE_PLUGIN_ROOT}/scripts/code_intel.py",
                    "serve",
                    "crg",
                ],
            },
        }
        self.assertEqual(self.claude["mcpServers"], expected)
        self.assertEqual(read_json("code-intel/.mcp.json"), {"mcpServers": expected})

    def test_hook_events_are_finite_and_update_after_mutating_tools(self):
        hooks = read_json("code-intel/hooks/hooks.json")["hooks"]
        self.assertEqual(
            set(hooks), {"SessionStart", "UserPromptSubmit", "PostToolUse"}
        )
        self.assertEqual(
            hooks["PostToolUse"][0]["matcher"],
            "Bash|Write|Edit|NotebookEdit|apply_patch",
        )
        commands = {
            "SessionStart": "hook-status",
            "UserPromptSubmit": "hook-prompt",
            "PostToolUse": "hook-update",
        }
        for event, command in commands.items():
            command_hooks = hooks[event][0]["hooks"]
            self.assertEqual(
                command_hooks,
                [
                    {
                        "type": "command",
                        "command": (
                            'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/code_intel.py" '
                            + command
                        ),
                        "timeout": 55,
                    }
                ],
            )

    def test_marketplaces_register_only_the_package_source(self):
        claude_marketplace = read_json(".claude-plugin/marketplace.json")
        codex_marketplace = read_json(".agents/plugins/marketplace.json")
        claude_entry = next(
            p for p in claude_marketplace["plugins"] if p["name"] == "code-intel"
        )
        codex_entry = next(
            p for p in codex_marketplace["plugins"] if p["name"] == "code-intel"
        )
        self.assertEqual(claude_entry["source"], "./code-intel")
        self.assertEqual(
            codex_entry["source"], {"source": "local", "path": "./code-intel"}
        )
        self.assertEqual(
            [p for p in claude_marketplace["plugins"] if p["name"] != "code-intel"],
            [
                {
                    "name": "continuous-learning",
                    "description": "Capture runtime surprises as they happen, periodically promote them into a project's own versioned skills/docs/commands, then forget them — git is the only permanent record.",
                    "source": "./continuous-learning",
                    "category": "developer-tools",
                    "dependencies": ["redis-memory"],
                },
                {
                    "name": "redis-memory",
                    "description": "Persistent cross-session memory for AI agents. Two storage modes: semantic vector search (mem_*) for knowledge found by meaning, and key-value store (kv_*) for instant lookup. Auto-expiry via TTL + volatile-lru eviction.",
                    "source": "./redis-memory-mcp",
                    "category": "memory",
                },
                {
                    "name": "lockstep",
                    "description": "Flow-enforcement engine for coding agents: declarative yamlgraph recipes, durable runs, deterministic evidence gates",
                    "source": "./lockstep",
                    "category": "developer-tools",
                },
                {
                    "name": "speciflow",
                    "description": "Coordinated specification-driven workflow guidance for coding agents.",
                    "source": "./speciflow",
                    "category": "developer-tools",
                },
            ],
        )
        self.assertEqual(
            [p for p in codex_marketplace["plugins"] if p["name"] != "code-intel"],
            [
                {
                    "name": "speciflow",
                    "source": {"source": "local", "path": "./speciflow"},
                    "category": "Productivity",
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                }
            ],
        )

    def test_release_please_updates_both_manifest_versions(self):
        release_config = read_json("release-please-config.json")
        release_state = read_json(".release-please-manifest.json")
        extra = release_config["packages"]["code-intel"]["extra-files"]
        self.assertEqual(
            {item["path"] for item in extra},
            {".claude-plugin/plugin.json", ".codex-plugin/plugin.json"},
        )
        self.assertEqual(release_state["code-intel"], "0.1.0")
        self.assertTrue(
            all(
                item["type"] == "json" and item["jsonpath"] == "$.version"
                for item in extra
            )
        )
        self.assertEqual(
            release_config["packages"]["code-intel"]["changelog-path"],
            "CHANGELOG.md",
        )

    def test_ci_runs_for_every_packaging_input(self):
        workflow = (REPO / ".github/workflows/code-intel.yml").read_text()
        expected_paths = {
            "code-intel/**",
            ".claude-plugin/marketplace.json",
            ".agents/plugins/marketplace.json",
            "release-please-config.json",
            ".release-please-manifest.json",
            ".github/workflows/code-intel.yml",
        }
        for event in ("push", "pull_request"):
            with self.subTest(event=event):
                block = re.search(rf"(?ms)^  {event}:\n(.*?)(?=^\S|^  \w|\Z)", workflow)
                self.assertIsNotNone(block)
                actual_paths = {
                    line.strip()[2:].strip('"')
                    for line in block.group(1).splitlines()
                    if line.strip().startswith('- "')
                }
                self.assertTrue(expected_paths.issubset(actual_paths), actual_paths)

    def test_ci_runs_repository_and_host_validation(self):
        workflow = (REPO / ".github/workflows/code-intel.yml").read_text()
        for command in (
            'python-version: ["3.11", "3.12"]',
            'node-version: "22"',
            "npm install --global @anthropic-ai/claude-code @openai/codex",
            "command -v claude", "command -v codex",
            "python3 -B code-intel/tests/test_code_intel.py -v",
            "python3 -B code-intel/tests/test_packaging.py -v",
            "python3 -B code-intel/tests/test_packaging.py PackagingTests.test_repository_owned_skill_validation -v",
            "claude plugin validate code-intel",
            'codex plugin marketplace add "$GITHUB_WORKSPACE" --json',
            "codex plugin add code-intel@claude-essentials --json",
        ):
            self.assertIn(command, workflow)


if __name__ == "__main__":
    unittest.main()
