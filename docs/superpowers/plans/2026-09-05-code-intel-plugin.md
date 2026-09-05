# Code Intel Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a distributable `code-intel` plugin that gives Claude Code and Codex the same pinned CodeGraph/CRG setup, checkout-scoped index lifecycle, fail-open hooks, diagnostics, and routing guidance.

**Architecture:** A single standard-library Python program owns tool installation and dispatch, Git/worktree discovery, checkout/index fingerprinting, per-worktree state and locking, index setup/update, diagnostics, and the three hook adapters. Thin shared manifests, hooks, and skill instructions expose that program to both hosts; packaging tests enforce host parity, release metadata, validators, and installed-layout behavior.

**Tech Stack:** Python 3 standard library (`argparse`, `dataclasses`, `hashlib`, `json`, `pathlib`, `subprocess`, `tempfile`, platform file locking), `unittest`, JSON plugin/MCP/hook manifests, YAML skill metadata, GitHub Actions, release-please, `mise`, CodeGraph 1.6.0, code-review-graph 2.3.8.

**Spec:** `docs/superpowers/specs/2026-09-05-code-intel-plugin-design.md`

## Global Constraints

- The plugin name is `code-intel`; both host manifests and `code-intel/CHANGELOG.md` start at version `0.1.0` and share identity and descriptive metadata.
- The only supported install path is `mise use -g npm:@colbymchenry/codegraph@1.6.0` plus `mise use -g pipx:code-review-graph@2.3.8`; hooks and MCP dispatch never install or upgrade tools.
- `code-intel/scripts/code_intel.py` is Python-standard-library-only; child commands use argv arrays without a shell. Every hook child has a finite timeout inside the overall hook deadline; the MCP server itself is a long-running protocol process.
- Resolve binaries from `PATH` first and the standard mise shim directory second; require CodeGraph `1.6.0` and code-review-graph `2.3.8`, and keep MCP stdout exclusively for protocol traffic.
- Indexes are owned by each canonical checkout/worktree root, never by branch or `git-common-dir`; simultaneous branch snapshots require linked worktrees.
- Select the first non-empty `PLUGIN_DATA`, otherwise the first non-empty `CLAUDE_PLUGIN_DATA`; never fall back from an unusable first selection, and never put state/locks in the plugin or checkout.
- Hooks automatically initialize only normal Git repositories and linked worktrees; non-Git umbrella indexing requires explicit `setup-project` authorization.
- Every resolvable `PostToolUse` Bash event and every supported write event forces synchronization; no command classifier, dirty-only check, timestamp, Git cleanliness, or `HEAD`-only shortcut can establish freshness.
- Fingerprints cover sorted tracked and non-ignored untracked paths, types, contents or symlink targets, indexing configuration, and persistent index contents; exclude Git administrative data, generated index directories, and transient lock/process files.
- All lifecycle failures are fail-open, invalidate trust when writable, publish no stale graph guidance, leave no child writer alive, and never block the user's host operation.
- Initialization writes `.codegraph/` and `.code-review-graph/` idempotently to the checkout's local Git exclude only; it never edits `.gitignore` or user-level `CLAUDE.md`, `AGENTS.md`, MCP configuration, or hooks.
- Do not detect, migrate, repair, remove, alias, or symlink any older `code-intel-setup` installation.
- Keep the exact distributable tree in the spec: two manifests, `.mcp.json`, shared hooks, one skill plus `agents/openai.yaml`, one Python program, two test files, and `CHANGELOG.md`.
- Retain supported Codex `hooks` metadata even if a generic validator rejects it; repository schema tests and the Codex installed-layout smoke test are authoritative.
- Do not perform marketplace normalization or unrelated marketplace cleanup; add only the `code-intel` entries required by this plugin.

---

## Execution Conventions and File Responsibilities

This plan supplies executable test foundations, exact interfaces, critical implementation
skeletons, and concrete validation commands; it deliberately does not reproduce the
entire final controller. Expand each named behavioral case with the supplied fixtures
before implementing that case. Run RED/GREEN for one case at a time within each task;
the six tasks below are review/commit gates, not single implementation steps.

`scripts/code_intel.py` owns the CLI and lifecycle implementation. `test_code_intel.py`
owns temporary repositories, process fixtures, and behavioral tests. `test_packaging.py`
owns distribution, both host schemas, and the repository-owned skill validator.
Manifests/hooks contain host declarations only; the skill describes user operations.
The workflow invokes repository tests and isolated host validation. No extra product
modules, global validators, marketplace migrations, or legacy-install discovery are added.

Run every Python command below with `PYTHONDONTWRITEBYTECODE=1` exported. Both test
entrypoints also set `sys.dont_write_bytecode = True` before loading the controller.
RED must fail on the named missing behavior, not a fixture NameError or a test runner
that discovered zero tests. Both test files end with `if __name__ == "__main__": unittest.main()`.

All paths in commands are relative to the repository root. The package root is
`Path(__file__).resolve().parents[1]` in either test file; the repository root is
`Path(__file__).resolve().parents[2]`. Never overwrite `HOME`; temporary host runs
set `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, and all three XDG directory variables explicitly.

### Task 1: Package Identity, Host Manifests, and Release Registration

**Files:**
- Create: `code-intel/.claude-plugin/plugin.json`
- Create: `code-intel/.codex-plugin/plugin.json`
- Create: `code-intel/.mcp.json`
- Create: `code-intel/hooks/hooks.json`
- Create: `code-intel/CHANGELOG.md`
- Create: `code-intel/tests/test_packaging.py`
- Create: `.github/workflows/code-intel.yml`
- Modify: `.claude-plugin/marketplace.json`
- Modify: `.agents/plugins/marketplace.json`
- Modify: `release-please-config.json`
- Modify: `.release-please-manifest.json`

**Interfaces:**
- Consumes: repository marketplace and release-please schemas already used at the listed root paths.
- Produces: Claude and Codex package metadata for `code-intel` version `0.1.0`; MCP commands `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/code_intel.py serve codegraph|crg`; hook commands for `hook-status`, `hook-prompt`, and `hook-update`.

- [ ] **Step 1: Write failing packaging-contract tests**

Start `test_packaging.py` with the following foundation, then add the methods below
inside `PackagingTests`. Tests read package files only when invoked, so missing files
produce the intended RED result.

```python
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parent

def read_json(relative):
    return json.loads((REPO / relative).read_text())

class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.claude = read_json("code-intel/.claude-plugin/plugin.json")
        self.codex = read_json("code-intel/.codex-plugin/plugin.json")
```

```python
def test_manifest_identity_and_shared_entrypoints(self):
    for field in ("name", "version", "description", "author", "homepage", "repository", "license"):
        self.assertEqual(self.claude[field], self.codex[field])
    self.assertEqual(self.codex["name"], "code-intel")
    self.assertEqual(self.codex["skills"], "./skills/")
    self.assertEqual(self.codex["hooks"], "./hooks/hooks.json")
    self.assertEqual(self.codex["mcpServers"], "./.mcp.json")

def test_marketplaces_register_only_the_package_source(self):
    claude_entry = next(p for p in read_json(".claude-plugin/marketplace.json")["plugins"] if p["name"] == "code-intel")
    codex_entry = next(p for p in read_json(".agents/plugins/marketplace.json")["plugins"] if p["name"] == "code-intel")
    self.assertEqual(claude_entry["source"], "./code-intel")
    self.assertEqual(codex_entry["source"], {"source": "local", "path": "./code-intel"})

def test_release_please_updates_both_manifest_versions(self):
    release_config = read_json("release-please-config.json")
    release_state = read_json(".release-please-manifest.json")
    extra = release_config["packages"]["code-intel"]["extra-files"]
    self.assertEqual({item["path"] for item in extra}, {
        ".claude-plugin/plugin.json", ".codex-plugin/plugin.json"
    })
    self.assertEqual(release_state["code-intel"], "0.1.0")
    self.assertTrue(all(item["type"] == "json" and item["jsonpath"] == "$.version" for item in extra))
    self.assertEqual(release_config["packages"]["code-intel"]["changelog-path"], "CHANGELOG.md")
```

Also assert two MCP server entries, exactly three hook event keys, `PostToolUse` matching `Bash|Write|Edit|NotebookEdit|apply_patch`, workflow path filters covering `code-intel/**`, both marketplaces, both release files, and its own workflow, and no unrelated marketplace entry changes.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 code-intel/tests/test_packaging.py -v`

Expected: FAIL because `code-intel` manifests and registrations do not exist.

- [ ] **Step 3: Add the minimum valid package and release metadata**

Use identical identity fields in both manifests. Make Claude declare its hooks and two MCP commands directly, make Codex point to the three shared paths, and make `.mcp.json` launch the Python dispatcher from `${CLAUDE_PLUGIN_ROOT}` while preserving the caller's working directory. Define fail-open command hooks with finite timeouts. Add only one new entry to each marketplace and one package entry to each release-please file. The dedicated workflow runs `python3 code-intel/tests/test_packaging.py -v` on the path filters asserted above.

Use this concrete manifest/MCP/hook construction as the metadata skeleton (write
the resulting JSON documents with `apply_patch`, not a generator added to the package):

```python
identity = {"name": "code-intel", "version": "0.1.0",
    "description": "Shared CodeGraph and code-review-graph setup and checkout indexing.",
    "author": {"name": "sergesha"}, "license": "MIT",
    "homepage": "https://github.com/sergesha/claude-essentials",
    "repository": "https://github.com/sergesha/claude-essentials"}
dispatcher = "${CLAUDE_PLUGIN_ROOT}/scripts/code_intel.py"
servers = {name: {"command": "python3", "args": [dispatcher, "serve", engine]}
           for name, engine in (("codegraph", "codegraph"), ("code-review-graph", "crg"))}
claude = dict(identity, hooks="./hooks/hooks.json", mcpServers=servers)
codex = dict(identity, skills="./skills/", hooks="./hooks/hooks.json", mcpServers="./.mcp.json")
mcp = {"mcpServers": servers}
hooks = {"hooks": {event: [{**({"matcher": "Bash|Write|Edit|NotebookEdit|apply_patch"}
        if event == "PostToolUse" else {}), "hooks": [{"type": "command",
        "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/code_intel.py" ' + command,
        "timeout": 55}]}] for event, command in (
        ("SessionStart", "hook-status"), ("UserPromptSubmit", "hook-prompt"),
        ("PostToolUse", "hook-update"))}}
release_package = {"package-name": "code-intel", "changelog-path": "CHANGELOG.md",
    "initial-version": "0.1.0", "extra-files": [
    {"type": "json", "path": path, "jsonpath": "$.version"}
    for path in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")]}
```

The Claude marketplace entry is `{"name":"code-intel","source":"./code-intel"}`;
the Codex entry is `{"name":"code-intel","source":{"source":"local","path":"./code-intel"},"category":"Productivity","policy":{"installation":"AVAILABLE","authentication":"ON_INSTALL"}}`.
Preserve existing entries byte-for-byte outside the insertion. Confirm with
`git diff -- .claude-plugin/marketplace.json .agents/plugins/marketplace.json` at this gate.
Initialize the changelog with `# Changelog` and `## 0.1.0`.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run: `python3 code-intel/tests/test_packaging.py -v`

Expected: PASS for identity, entrypoints, hook schema, marketplace registration, release coverage, and CI path coverage.

- [ ] **Step 5: Commit**

```bash
git add code-intel/.claude-plugin/plugin.json code-intel/.codex-plugin/plugin.json code-intel/.mcp.json code-intel/hooks/hooks.json code-intel/CHANGELOG.md code-intel/tests/test_packaging.py .claude-plugin/marketplace.json .agents/plugins/marketplace.json release-please-config.json .release-please-manifest.json .github/workflows/code-intel.yml
git commit -m "feat: register code intelligence plugin package"
```

### Task 2: Tool Contract, Safe Process Runner, and MCP Dispatch

**Files:**
- Create: `code-intel/scripts/code_intel.py`
- Create: `code-intel/tests/test_code_intel.py`

**Interfaces:**
- Consumes: `PLUGIN_ROOT = Path(__file__).resolve().parents[1]`, caller environment, and subprocess argv arrays.
- Produces: frozen `ToolSpec(name: str, executable: str, version: str, mise_package: str)`, `TOOLS: dict[str, ToolSpec]`, `UserError(Exception)`, `run_child(argv: Sequence[str], *, cwd: Path | None, timeout: float) -> subprocess.CompletedProcess[str]`, `resolve_verified_tool(spec: ToolSpec, *, deadline: float) -> Path`, `install_tools() -> int`, `serve(engine: str) -> int`, and `main(argv: Sequence[str] | None = None) -> int` with `install-tools` and `serve {codegraph,crg}`. Doctor is implemented in Task 4 once its dependencies exist.

- [ ] **Step 1: Write failing tool and diagnostic tests**

Create this shared harness in `test_code_intel.py`; all test classes below inherit
`ControllerCase`. The repository fixture uses real Git, with no global Git config
changes. `load_controller` registers the module before execution for dataclasses.

```python
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
PACKAGE = Path(__file__).resolve().parents[1]

def load_controller():
    spec = importlib.util.spec_from_file_location("code_intel_under_test", PACKAGE / "scripts/code_intel.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, timeout=10).stdout.strip()

def snapshot(root):
    return {p.relative_to(root).as_posix():
            ("link", os.readlink(p)) if p.is_symlink() else
            ("file", p.read_bytes()) if p.is_file() else ("dir", None)
            for p in root.rglob("*")}

class ControllerCase(unittest.TestCase):
    def setUp(self):
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
```

Add exact tests for pinned `mise use -g` argv; PATH-before-mise resolution; exact,
wrong, and unparseable versions; missing `mise`; installed-plugin paths containing
spaces and shell metacharacters; caller-CWD preservation; server stdout passthrough;
and dispatcher diagnostics exclusively on stderr. Use `patch.object(module,
"run_child")` for command contracts, and real executable fixtures/processes for
stdout, timeout, and descendant-lifetime behavior.

```python
def test_install_tools_uses_exact_pins(self):
    module = self.module
    with patch.object(module, "run_child", return_value=subprocess.CompletedProcess([], 0, "", "")) as child, patch.object(module, "resolve_verified_tool", return_value=Path("/tools/verified")), patch.object(module.shutil, "which", return_value="/tools/mise"):
        rc = module.main(["install-tools"])
    self.assertEqual([call.args[0] for call in child.call_args_list], [
        ["/tools/mise", "use", "-g", "npm:@colbymchenry/codegraph@1.6.0"],
        ["/tools/mise", "use", "-g", "pipx:code-review-graph@2.3.8"]])
    self.assertEqual(rc, 0)

def test_serve_preserves_cwd_and_never_uses_shell(self):
    module = self.module
    original = Path.cwd()
    with patch.object(module, "resolve_verified_tool", return_value=Path("/tools/code graph;$x")), patch.object(module.os, "execv") as execute:
        module.main(["serve", "codegraph"])
    execute.assert_called_once_with("/tools/code graph;$x", ["/tools/code graph;$x", "serve", "--mcp"])
    self.assertEqual(Path.cwd(), original)
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 -B code-intel/tests/test_code_intel.py ToolContractTests -v`

Expected: FAIL because the controller and its commands do not exist.

- [ ] **Step 3: Implement only tool resolution, child supervision, dispatch, and read-only reporting**

Define the immutable specs, exact version parsers, a single argv-only child runner
that terminates and reaps its process group on timeout, and one-diagnostic error
paths. Implement resolution and version tests before the install/serve orchestration.
Use this orchestration skeleton after `resolve_verified_tool` passes those tests:

```python
TOOLS = {
    "codegraph": ToolSpec("codegraph", "codegraph", "1.6.0", "npm:@colbymchenry/codegraph@1.6.0"),
    "crg": ToolSpec("crg", "code-review-graph", "2.3.8", "pipx:code-review-graph@2.3.8"),
}

def install_tools():
    mise = shutil.which("mise")
    if not mise:
        raise UserError("Install mise, then invoke install-tools explicitly.")
    for spec in TOOLS.values():
        run_child([mise, "use", "-g", spec.mise_package], cwd=None, timeout=300)
    for spec in TOOLS.values():
        resolve_verified_tool(spec, deadline=time.monotonic() + 10)
    return 0

def serve(engine):
    binary = str(resolve_verified_tool(TOOLS[engine], deadline=time.monotonic() + 10))
    args = [binary, "serve"] + (["--mcp"] if engine == "codegraph" else [])
    os.execv(binary, args)
    return 0
```

`run_child` checks nonzero status, captures output, starts a separate process group
on POSIX, and uses `communicate(timeout=timeout)`. On timeout kill the entire group,
call `communicate()` to reap the direct child, and ensure descendants have exited
before raising `UserError`. Test this with a real child that spawns an index writer
and a deadline short enough to observe timeout; record the writer PID and verify
it no longer runs after return. Successful direct-child exit with surviving group
members must not leave writers behind either. Platform-specific supervision must
offer the same guarantee on supported platforms. Never feed protocol output through
the captured-output runner: `serve` uses `execv`, preserves CWD, and inherits stdout.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run: `python3 -B code-intel/tests/test_code_intel.py ToolContractTests -v`

Expected: PASS, including hostile path/CWD cases, version rejection, and process cleanup.

- [ ] **Step 5: Commit**

```bash
git add code-intel/scripts/code_intel.py code-intel/tests/test_code_intel.py
git commit -m "feat: add pinned tool dispatch and diagnostics"
```

### Task 3: Repository Discovery and Internal Index Operations

**Files:**
- Modify: `code-intel/scripts/code_intel.py`
- Modify: `code-intel/tests/test_code_intel.py`

**Interfaces:**
- Consumes: `run_child(...)` and verified `ToolSpec` instances from Task 2.
- Produces: frozen `RepoScope(kind: Literal["repository", "worktree", "umbrella", "none"], root: Path, repositories: tuple[Path, ...])`, `discover_scope(path: Path, *, deadline: float) -> RepoScope`, `setup_roots(scope: RepoScope) -> tuple[tuple[Path, tuple[str, ...]], ...]`, `ensure_local_excludes(root: Path, *, deadline: float) -> None`, `initialize_indexes_locked(root: Path, tools: Mapping[str, Path], *, force: bool, deadline: float) -> None`, and `update_indexes_locked(root: Path, tools: Mapping[str, Path], *, deadline: float) -> None`. Internal `_locked` functions never acquire locks; Task 4 supplies their public locked callers and exposes the setup/update CLI commands.

- [ ] **Step 1: Write failing discovery/setup/update tests**

Add `DiscoveryTests` and `IndexCommandTests` covering a normal repository, linked worktree, non-Git umbrella with nested repositories, unrelated non-Git directory, canonical paths with spaces/metacharacters, deterministic nested repository ordering, dependency order (CodeGraph before CRG), missing-index refusal for explicit updates, incremental CRG update, and idempotent writes to `git rev-parse --git-path info/exclude` without touching `.gitignore`.

```python
def test_umbrella_setup_initializes_children_then_codegraph_umbrella(self):
    scope = self.module.discover_scope(self.base, deadline=time.monotonic() + 10)
    self.assertEqual(scope.kind, "umbrella")
    self.assertEqual(self.module.setup_roots(scope), (
        (self.repo.resolve(), ("codegraph", "crg")),
        (self.base.resolve(), ("codegraph",))))

def test_update_project_refuses_missing_indexes(self):
    with self.assertRaisesRegex(self.module.UserError, "setup-project"):
        self.module.update_indexes_locked(self.repo, {}, deadline=time.monotonic() + 10)
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 code-intel/tests/test_code_intel.py DiscoveryTests IndexCommandTests -v`

Expected: FAIL because discovery and index commands are absent.

- [ ] **Step 3: Implement discovery and explicit index commands**

Resolve canonical worktree roots with Git and distinguish linked worktrees without
using the common directory as identity. Discovery recognizes umbrellas read-only
for every caller; authorization controls initialization, not recognition. Return
`none` for a non-Git directory with no nested repositories. Bound traversal and Git
children by the supplied deadline; do not follow symlink directories or recurse
inside `.git` and generated index directories. Canonicalize, deduplicate, and sort
nested repository roots. Use this scope/command skeleton:

```python
def setup_roots(scope):
    if scope.kind in ("repository", "worktree"):
        return ((scope.root, ("codegraph", "crg")),)
    if scope.kind == "umbrella":
        return tuple((root, ("codegraph", "crg")) for root in sorted(scope.repositories)) + ((scope.root, ("codegraph",)),)
    raise UserError("No Git repository or umbrella scope at this path.")

def update_indexes_locked(root, tools, *, deadline):
    required = {"codegraph": ".codegraph", "crg": ".code-review-graph"}
    if any(not (root / required[name]).is_dir() for name in tools) or not tools:
        raise UserError("Missing index; authorize setup-project first.")
    if "codegraph" in tools:
        run_child([str(tools["codegraph"]), "sync", str(root)], cwd=root,
                  timeout=remaining(deadline))
    if "crg" in tools:
        run_child([str(tools["crg"]), "update", "--skip-flows", "--repo", str(root)],
                  cwd=root, timeout=remaining(deadline))
```

`remaining(deadline: float) -> float` returns `deadline - time.monotonic()` or raises
`UserError` at/below zero. Initialization uses `[codegraph, "init", str(root)]`,
then `[crg, "build", "--repo", str(root)]` for missing selected engines. Explicit
`--force` re-runs these build commands; it never recursively deletes the checkout.
Umbrellas select only CodeGraph. Avoid CRG global registration/configuration writes.
Use `git rev-parse --git-path info/exclude`, resolve relative output against the
checkout root, preserve existing content, and append each missing generated-directory
entry once. Write no excludes for the non-Git umbrella itself. The reference skill's
discovery/exclude behavior can inform the implementation; no global-install scanning
or configuration-rewrite functions are copied into the product.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run: `python3 code-intel/tests/test_code_intel.py DiscoveryTests IndexCommandTests -v`

Expected: PASS for all repository kinds, ordering, argv safety, exclude idempotence, and missing-index behavior.

- [ ] **Step 5: Commit**

```bash
git add code-intel/scripts/code_intel.py code-intel/tests/test_code_intel.py
git commit -m "feat: add repository setup and index updates"
```

### Task 4: Per-Worktree State, Locking, and Content Freshness

**Files:**
- Modify: `code-intel/scripts/code_intel.py`
- Modify: `code-intel/tests/test_code_intel.py`

**Interfaces:**
- Consumes: canonical `RepoScope.root`, safe child execution, and index commands from Tasks 2-3.
- Produces: `DataLocation(path: Path, source: Literal["PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"])`, `FreshnessMarker(root: str, head: str, versions: Mapping[str, str], checkout_fingerprint: str, index_fingerprints: Mapping[str, str], status: Literal["pending", "success", "failed"])`, `select_data_location(env: Mapping[str, str], *, read_only: bool) -> DataLocation`, `checkout_fingerprint(root: Path, deadline: float) -> str`, `index_fingerprint(root: Path, index_name: str, deadline: float) -> str`, `root_lock(root: Path, data: DataLocation, deadline: float)`, and atomic marker read/write helpers keyed by a digest of the canonical root.
- Also produces: `UnusableDataLocation(UserError)`, `CorruptState(UserError)`, `state_path(root: Path, data: DataLocation) -> Path`, `read_marker(root: Path, data: DataLocation) -> FreshnessMarker | None`, `write_marker(root: Path, data: DataLocation, marker: FreshnessMarker) -> None`, `capture(root: Path, tools: Mapping[str, Path], deadline: float) -> FreshnessMarker`, `observe_project(path: Path, *, deadline: float) -> dict[str, object]`, `mutate_project(path: Path, *, operation: Literal["setup", "update"], force: bool, deadline: float) -> None`, and CLI `doctor`, `project-status PATH`, `setup-project PATH [--force]`, `setup-batch BASE`, `update-project PATH`, `update-batch BASE`. `root_lock` returns `ContextManager[None]`. `capture` returns a success-shaped observation, never writes it, and requires all selected indexes to exist.

- [ ] **Step 1: Write failing state/fingerprint/concurrency tests**

Add `StateTests`, `FingerprintTests`, and `ConcurrencyTests`. Cover deterministic environment precedence; absent/unwritable/corrupt storage; root embedded in and validated against state; independent state/locks for linked worktrees; atomic replacement; pending/failed trust; same-root serialization; finite lock deadline; read-only observation without lock creation; tracked/untracked file edits and deletions; symlink targets without traversal; exclusions; persistent index journal inclusion; transient lock/PID exclusion; and mutation during either fingerprint pass.

```python
def test_first_nonempty_data_variable_never_falls_back(self):
    module = self.module
    unwritable = self.base / "not-a-directory"
    unwritable.write_text("occupied")
    writable = self.data
    env = {"PLUGIN_DATA": str(unwritable), "CLAUDE_PLUGIN_DATA": str(writable)}
    with self.assertRaises(module.UnusableDataLocation):
        module.select_data_location(env, read_only=False)
    self.assertFalse(any(writable.iterdir()))

def test_worktrees_have_independent_digest_keys(self):
    worktree_b = self.base / "linked"
    git(self.repo, "worktree", "add", "-qb", "other", str(worktree_b))
    data = self.module.select_data_location(os.environ, read_only=True)
    self.assertNotEqual(self.module.state_path(self.repo.resolve(), data),
                        self.module.state_path(worktree_b.resolve(), data))

def test_doctor_without_state_is_unhealthy_and_read_only(self):
    before = snapshot(self.base)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = self.module.main(["project-status", str(self.repo)])
    self.assertNotEqual(rc, 0)
    self.assertEqual(snapshot(self.base), before)
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 -B code-intel/tests/test_code_intel.py StateTests FingerprintTests ConcurrencyTests DoctorTests -v`

Expected: FAIL because state, fingerprint, and lock primitives are absent.

- [ ] **Step 3: Implement atomic state, OS locking, and bounded fingerprints**

Hash the canonical root into separate state/lock names while storing the full root for collision/mismatch validation. Use an OS lock released by process exit and a monotonic deadline. Write `pending` before mutation and publish `success` only by same-directory atomic replacement. Derive checkout inputs from Git's tracked and non-ignored untracked file sets, hash path/type/content in sorted order, hash symlink targets, and restart/fail if the input set changes mid-capture. Hash persistent index content/configuration including required journals while excluding transient locks and process identity files.

Do not restart a fingerprint after observing an input mutation: fail this attempt
open. Use `git ls-files -z --cached --others --exclude-standard`, hash the sorted
deduplicated raw paths and their types, and detect tracked deletions explicitly.
Compare input sets and per-file metadata before/after reads; an unreadable input or
any observed mutation invalidates capture. Index journals/WAL are persistent inputs;
never blanket-exclude `*.wal` or `*.journal`. Exclude transient engine lock/PID files
by their actual names and test both included journals and excluded transient files.

Use this marker-path implementation and public-operation control skeleton:

```python
def state_path(root, data):
    key = hashlib.sha256(os.fsencode(str(root.resolve()))).hexdigest()
    return data.path / (key + ".json")

def mutate_project(path, *, operation, force, deadline):
    scope = discover_scope(path, deadline=deadline)
    data = select_data_location(os.environ, read_only=False)
    for root, engines in setup_roots(scope):
        with root_lock(root, data, deadline):
            previous = read_marker(root, data)  # CorruptState propagates; do not repair.
            pending = FreshnessMarker(str(root), "", {}, "", {}, "pending")
            write_marker(root, data, pending)
            try:
                tools = {name: resolve_verified_tool(TOOLS[name], deadline=deadline) for name in engines}
                before = capture_checkout(root, deadline)
                if operation == "setup":
                    initialize_indexes_locked(root, tools, force=force, deadline=deadline)
                update_indexes_locked(root, tools, deadline=deadline)
                observed = capture(root, tools, deadline)
                if before != capture_checkout(root, deadline):
                    raise UserError("Checkout changed during indexing.")
                write_marker(root, data, observed)
            except Exception:
                write_marker(root, data, dataclasses.replace(pending, status="failed"))
                raise
```

Define `capture_checkout(root: Path, deadline: float) -> tuple[str, str]` as captured
HEAD plus checkout fingerprint, with HEAD read before/after and equality required.
For explicit non-Git umbrella setup, use empty HEAD plus a bounded recursive content
fingerprint excluding nested Git administration/indexes; child repositories retain
their independent normal markers. Hook paths never create umbrella state/indexes.
Failure to write failed state must retain/report the original error too; failure to
acquire a lock must never write another holder's marker. The skeleton's `previous`
read validates state before mutation; missing and valid pending/failed markers permit
recovery, corrupt state does not. Each batch root is locked independently in sorted
order and released before the next, avoiding nested-lock deadlocks. Add real process
tests where explicit setup/update and a hook compete for one root, and where different
worktrees complete concurrently without state loss.

Implement `observe_project` only now: discover scope and resolve tools; read state,
capture current inputs, then reread state. Trust requires identical successful state
reads and equality with the observed root/HEAD/versions/all fingerprints. A pending
marker or any changed/unreadable input is unhealthy. It never calls `root_lock`,
creates data storage, or repairs corrupt state. Report Python, mise, executable paths,
plugin root, selected data source/path and best-effort access metadata, scope kind,
index presence, current/stored HEAD, and trust reason. `doctor` uses `Path.cwd()`;
`project-status` uses its argument. Both print JSON and return 0 only when healthy.
Test filesystem snapshots for healthy, absent-state, corrupt-state, and concurrently
pending states; test same-HEAD offline edits and index changes as unhealthy.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run: `python3 -B code-intel/tests/test_code_intel.py StateTests FingerprintTests ConcurrencyTests DoctorTests -v`

Expected: PASS, including parallel different-root work and serialized same-root work through completion.

- [ ] **Step 5: Commit**

```bash
git add code-intel/scripts/code_intel.py code-intel/tests/test_code_intel.py
git commit -m "feat: track checkout scoped index freshness"
```

### Task 5: Shared Readiness Procedure and Fail-Open Lifecycle Hooks

**Files:**
- Modify: `code-intel/scripts/code_intel.py`
- Modify: `code-intel/tests/test_code_intel.py`
- Modify: `code-intel/hooks/hooks.json`

**Interfaces:**
- Consumes: verified tools, repository discovery, setup/update operations, data selection, root locks, fingerprints, and markers from Tasks 2-4.
- Produces: frozen `ReadinessResult(root: Path, tools: Mapping[str, Path], marker: FreshnessMarker)`, `ensure_ready(path: Path, *, force_sync: bool, deadline: float) -> ContextManager[ReadinessResult]`, `handle_hook(command: str, payload: object, *, cwd: Path | None = None) -> dict[str, object]`, `hook_response(event: str, text: str) -> dict[str, object]`, `extract_prompt_context(stdout: str) -> str`, and CLI commands `hook-status`, `hook-prompt`, and `hook-update` (all return exit 0 on failure with fallback JSON).

- [ ] **Step 1: Write failing readiness and hook-adapter tests**

Add `ReadinessTests` and `HookTests` covering the complete verification sequence. Include session initialization; prompt/write/Bash discovery of a new worktree; deleted-index recreation; clean branch switch; same-`HEAD` restore/reset; arbitrary Bash mutations; matching-marker reuse; offline edit and index mutation; failed/pending state; captured `HEAD` or checkout change during sync; lock, child, fingerprint, and overall deadlines; descendant termination/reaping; malformed Claude/Codex payloads; and common output JSON.

```python
def test_every_resolvable_bash_post_tool_event_forces_sync(self):
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
               "tool_input": {"command": "true"}, "cwd": str(self.repo)}
    with patch.object(self.module, "ensure_ready", side_effect=self.module.UserError("busy")) as ready:
        response = self.module.handle_hook("hook-update", payload)
    self.assertTrue(ready.call_args.kwargs["force_sync"])
    self.assertFalse(response["hookSpecificOutput"].get("block", False))

def test_unready_prompt_uses_fallback_without_prompt_hook(self):
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": "explain source", "cwd": str(self.repo)}
    with patch.object(self.module, "ensure_ready", side_effect=self.module.UserError("stale")), patch.object(self.module, "run_child") as child:
        response = self.module.handle_hook("hook-prompt", payload)
    child.assert_not_called()
    self.assertIn("normal file/search tools", json.dumps(response))
    self.assertFalse(any(json.loads(path.read_text())["status"] == "success"
                         for path in self.data.glob("*.json")))
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 code-intel/tests/test_code_intel.py ReadinessTests HookTests -v`

Expected: FAIL because readiness orchestration and hook adapters are absent.

- [ ] **Step 3: Implement one readiness state machine and three thin adapters**

Under the per-root lock: verify tools; inspect index presence; compare versions, captured `HEAD`, checkout fingerprint, and both index fingerprints; reuse only an exact successful marker unless the event forces sync; otherwise write pending, capture inputs, initialize missing indexes in dependency order, sync CodeGraph, incrementally update CRG, recapture index fingerprints, and verify that `HEAD` and checkout inputs remain unchanged before publishing success. Keep the lock through CodeGraph `prompt-hook`. On any error/timeout/mutation, terminate descendants, invalidate success if state is writable, and emit concise non-blocking fallback JSON. Do not call `prompt-hook` without established freshness and never treat its earlier stdout as current after a later failure.

Implement the lock lifetime with this context-manager skeleton. `capture` equality
includes root, HEAD, exact versions, checkout content, and both index fingerprints.
The final capture occurs after the caller's prompt work and before publication.

```python
@contextlib.contextmanager
def ensure_ready(path, *, force_sync, deadline):
    scope = discover_scope(path, deadline=deadline)
    if scope.kind == "umbrella":
        raise UserError("Umbrella scope is not initialized automatically; request authorization for setup-project.")
    if scope.kind not in ("repository", "worktree"):
        raise UserError("No Git checkout available.")
    root = scope.root
    data = select_data_location(os.environ, read_only=False)
    with root_lock(root, data, deadline):
        previous = read_marker(root, data)  # Corruption fails without overwriting it.
        pending = FreshnessMarker(str(root), "", {}, "", {}, "pending")
        try:
            tools = {name: resolve_verified_tool(spec, deadline=deadline) for name, spec in TOOLS.items()}
            indexes_exist = all((root / name).is_dir() for name in (".codegraph", ".code-review-graph"))
            observed = capture(root, tools, deadline) if indexes_exist else None
            reuse = (not force_sync and previous is not None and
                     previous.status == "success" and previous == observed)
            if not reuse:
                write_marker(root, data, pending)
                before = capture_checkout(root, deadline)
                initialize_indexes_locked(root, tools, force=False, deadline=deadline)
                update_indexes_locked(root, tools, deadline=deadline)
                observed = capture(root, tools, deadline)
                if before != capture_checkout(root, deadline):
                    raise UserError("Checkout changed during indexing.")
            yield ReadinessResult(root, tools, observed)
            if capture(root, tools, deadline) != observed:
                raise UserError("Checkout or indexes changed during hook completion.")
            write_marker(root, data, observed)
        except Exception:
            write_marker(root, data, dataclasses.replace(pending, status="failed"))
            raise

def hook_response(event, text):
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
```

As in Task 4, preserve the original diagnostic when invalidation itself fails. Do not
enter the invalidation handler until the root lock is held and state has been parsed;
corrupt state and lock timeout leave existing bytes unchanged. Successful matching
markers may be reused without sync, but still undergo the final capture. Missing
indexes, a valid pending/failed marker, offline edits, and changed index contents
take the synchronization branch. Capture failures themselves fail open.

Set the Python overall hook deadline to `time.monotonic() + 45`; every Git/version/
engine child gets `remaining(deadline)` (optionally capped below it), and lock waits
and fingerprint reads check that same deadline. The 55-second host timeout leaves
cleanup margin. Extend `run_child` with keyword `input_text: str | None = None`
and pass it to `communicate` for prompt-hook input. `handle_hook` maps commands to
event names, validates a JSON object and string CWD, defaults missing CWD to the
explicit `cwd` argument or process CWD, and recognizes `tool_name` or `toolName`.
Malformed JSON is caught by the CLI before calling the handler and produces fallback.
For PostToolUse, Bash and `Write`, `Edit`, `NotebookEdit`, `apply_patch` force sync;
other tools return empty non-blocking output without indexing. Umbrella discovery
must return the authorization guidance above without invoking any index command.

Within `with ensure_ready(...) as ready`, run CodeGraph prompt-hook only for prompt
submission, using `[str(ready.tools["codegraph"]), "prompt-hook"]`, current root CWD,
the original payload JSON as stdin, and the remaining deadline. Extract only the
string `hookSpecificOutput.additionalContext` from its valid JSON via
`extract_prompt_context`; malformed output fails open. Append the routing text below.
Return the assembled host response only AFTER the `with` exits successfully. Catch
exceptions outside it, discard assembled prompt output, and return fallback guidance.

Add real readiness tests in addition to the adapter snippets: warm indexes once,
then simulate prompt-time source edits, index edits, timeout, and failed child exit;
assert no success marker remains and no earlier prompt text escapes. Use two
processes plus a pipe/barrier so an explicit update cannot acquire the root lock
until prompt completion. On lock timeout, assert the other process's marker bytes
are unchanged. Test a non-Git umbrella with the repository fixture and assert no
`.codegraph` appears under the umbrella. For malformed-payload tests invoke the
CLI with raw stdin, check exit 0, and parse its stdout as the shared JSON contract.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run: `python3 code-intel/tests/test_code_intel.py ReadinessTests HookTests -v`

Expected: PASS with no stale marker, stale routing text, blocked host action, redundant matching-marker sync, or surviving writer.

- [ ] **Step 5: Commit**

```bash
git add code-intel/scripts/code_intel.py code-intel/tests/test_code_intel.py code-intel/hooks/hooks.json
git commit -m "feat: synchronize indexes from lifecycle hooks"
```

### Task 6: Shared Skill, Exact Distribution, and Installed-Layout Validation

**Files:**
- Create: `code-intel/skills/code-intel/SKILL.md`
- Create: `code-intel/skills/code-intel/agents/openai.yaml`
- Modify: `code-intel/tests/test_packaging.py`
- Modify: `.github/workflows/code-intel.yml`

**Interfaces:**
- Consumes: all CLI commands and hook readiness semantics completed in Tasks 2-5.
- Produces: implicitly invocable `code-intel` skill instructions for install/setup/restart/status/update and exact routing; repository, Claude, and Codex package validation including a temporary installed-layout smoke test.

- [ ] **Step 1: Write failing distribution and instruction-contract tests**

Add `re`, `shutil`, and `textwrap` to the `test_packaging.py` imports, then add this
exact distribution contract and repository-owned skill validator above
`PackagingTests`. The Git listing checks source files that would be packaged while
explicitly ignoring Python bytecode; the installed-layout fixture below copies only
this allowlist, so pre-existing state, indexes, and caches can never enter the smoke
test.

```python
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

def test_exact_distributable_file_set(self):
    self.assertEqual(repository_distributable_files(), EXPECTED_DISTRIBUTABLE_FILES)

def test_repository_owned_skill_validation(self):
    self.assertEqual(validate_skill(PACKAGE / "skills/code-intel"), [])
```

Add explicit instruction-contract assertions. These strings are the stable user
contract, rather than incidental headings: install exact pins only from
`install-tools`; restart after installation; obtain authorization before umbrella
`setup-project`; route symbol/call-path questions to CodeGraph; route review/impact
questions to code-review-graph; route architecture/semantic/refactoring questions to
code-review-graph; and fall back to normal file/search tools if the selected graph
cannot answer. Also reject instructions to migrate `code-intel-setup` or edit
user-level `CLAUDE.md`, `AGENTS.md`, MCP, or hook configuration.

```python
def test_skill_instruction_contract(self):
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
        "code-intel-setup",
        "edit CLAUDE.md",
        "edit AGENTS.md",
        "edit user MCP configuration",
        "edit user hook configuration",
    ):
        self.assertNotIn(prohibited, skill)
```

Add these complete staging and fake-server helpers above the test class. The fake
executables implement version checks, index creation, updates, prompt routing, and
server launch, so no helper or service is assumed outside this file.

```python
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
            print(json.dumps({{"routing": "fresh graph"}}))
        elif args[:1] == ["serve"]:
            Path(os.environ["FAKE_SERVER_LOG"]).write_text(os.getcwd())
        else:
            raise SystemExit("unexpected fake-tool argv: " + repr(args))
        """))
    path.chmod(0o755)
    return path
```

Extend `PackagingTests.setUp` after loading the manifests and add the subprocess
helper. Every child disables bytecode and receives isolated plugin data; no child
writes inside the installed package.

```python
temp = tempfile.TemporaryDirectory(prefix="code intel installed ; ")
self.addCleanup(temp.cleanup)
self.base = Path(temp.name)
self.installed = stage_installed_copy(self.base)
self.consumer_repo = self.base / "unrelated consumer"
self.consumer_repo.mkdir()
subprocess.run(["git", "init", "-q"], cwd=self.consumer_repo, check=True)
self.bin_dir = self.base / "fake bin"
self.bin_dir.mkdir()
write_fake_tool(self.bin_dir, "codegraph", "1.6.0")
write_fake_tool(self.bin_dir, "code-review-graph", "2.3.8")
self.data_dir = self.base / "plugin data"
self.data_dir.mkdir()

def run_installed_copy(self, *args, payload=None, cwd=None, extra_env=None):
    env = {
        **os.environ,
        "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "PLUGIN_DATA": str(self.data_dir),
        "CLAUDE_PLUGIN_DATA": "",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-B", str(self.installed / "scripts/code_intel.py"), *args],
        cwd=cwd or self.consumer_repo, input=None if payload is None else json.dumps(payload),
        capture_output=True, text=True, timeout=20, env=env,
    )
```

Add the complete smoke test. It runs read-only `doctor`, both MCP dispatch paths,
and all three hook commands from the unrelated checkout; `hook-update` is exercised
for both write and arbitrary Bash payloads.

```python
def test_installed_layout_smoke(self):
    doctor = self.run_installed_copy("doctor")
    self.assertNotEqual(doctor.returncode, 0)
    json.loads(doctor.stdout)

    for engine in ("codegraph", "crg"):
        log = self.base / (engine + ".cwd")
        completed = self.run_installed_copy(
            "serve", engine, extra_env={"FAKE_SERVER_LOG": str(log)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(log.read_text(), str(self.consumer_repo))

    payloads = (
        ("hook-status", {"hook_event_name": "SessionStart", "cwd": str(self.consumer_repo)}),
        ("hook-prompt", {"hook_event_name": "UserPromptSubmit", "prompt": "trace callers", "cwd": str(self.consumer_repo)}),
        ("hook-update", {"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {"file_path": "source.py"}, "cwd": str(self.consumer_repo)}),
        ("hook-update", {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "true"}, "cwd": str(self.consumer_repo)}),
    )
    for command, payload in payloads:
        completed = self.run_installed_copy(command, payload=payload)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        json.loads(completed.stdout)

    self.assertEqual(
        {p.relative_to(self.installed).as_posix() for p in self.installed.rglob("*") if p.is_file()},
        EXPECTED_DISTRIBUTABLE_FILES,
    )
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 code-intel/tests/test_packaging.py -v`

Expected: FAIL because the shared skill, agent metadata, exact file contract, and installed-layout checks are incomplete.

- [ ] **Step 3: Add the skill and finish authoritative validation**

Write concise host-neutral instructions that map each user intent to the exact CLI, require approval only for `install-tools` and explicit umbrella setup, tell users to restart after install, explain checkout/worktree ownership, and include the four exact routing/fallback sentences asserted by `test_skill_instruction_contract`. Configure `openai.yaml` for implicit invocation. Make CI run unit/packaging tests, the skill validator, Claude plugin validator, and temporary Codex marketplace-add/plugin-add smoke test; do not invoke or modify a generic validator that rejects supported Codex hooks metadata.

The repository-owned validator is `test_repository_owned_skill_validation` above;
CI must invoke it directly and must not depend on a validator installed in a global
skill directory. Replace `.github/workflows/code-intel.yml` with the following job
body after its path filters. This follows the repository's GitHub-hosted Ubuntu and
Python 3.11/3.12 matrix convention, installs Node explicitly, installs both host CLIs
before using them, and fails immediately if either executable is unavailable. Host
validation requires no account session: it validates/adds the local checkout only.

```yaml
permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
      - name: Establish host CLIs
        run: |
          npm install --global @anthropic-ai/claude-code @openai/codex
          command -v claude
          command -v codex
          claude --version
          codex --version
      - name: Repository tests and repository-owned skill validation
        env:
          PYTHONDONTWRITEBYTECODE: "1"
        run: |
          python3 -B code-intel/tests/test_code_intel.py -v
          python3 -B code-intel/tests/test_packaging.py -v
          python3 -B code-intel/tests/test_packaging.py PackagingTests.test_repository_owned_skill_validation -v
      - name: Claude package validation
        run: claude plugin validate code-intel
      - name: Isolated Codex marketplace and install smoke
        shell: bash
        run: |
          set -euo pipefail
          host_tmp="$(mktemp -d "$RUNNER_TEMP/code-intel-host.XXXXXX")"
          trap 'rm -rf "$host_tmp"' EXIT
          export CODEX_HOME="$host_tmp/codex-home"
          export PLUGIN_DATA="$host_tmp/plugin-data"
          export CLAUDE_PLUGIN_DATA=""
          export XDG_CONFIG_HOME="$host_tmp/xdg-config"
          export XDG_CACHE_HOME="$host_tmp/xdg-cache"
          export XDG_DATA_HOME="$host_tmp/xdg-data"
          export PYTHONDONTWRITEBYTECODE=1
          mkdir -p "$CODEX_HOME" "$PLUGIN_DATA" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME"
          codex plugin marketplace add "$GITHUB_WORKSPACE" --json
          codex plugin marketplace list --json > "$host_tmp/marketplaces.json"
          codex plugin add code-intel@claude-essentials --json
          codex plugin list --json > "$host_tmp/plugins.json"
          python3 - "$host_tmp/marketplaces.json" "$host_tmp/plugins.json" <<'PY'
          import json
          import sys
          marketplace = json.dumps(json.load(open(sys.argv[1], encoding="utf-8")))
          plugins = json.dumps(json.load(open(sys.argv[2], encoding="utf-8")))
          assert "claude-essentials" in marketplace, marketplace
          assert "code-intel" in plugins and "claude-essentials" in plugins, plugins
          PY
```

- [ ] **Step 4: Run the focused tests and validators and confirm GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B code-intel/tests/test_packaging.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -B code-intel/tests/test_code_intel.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -B code-intel/tests/test_packaging.py PackagingTests.test_repository_owned_skill_validation -v
claude plugin validate code-intel
```

Then run the exact `Isolated Codex marketplace and install smoke` shell block from
`.github/workflows/code-intel.yml`. Locally, first establish CLI availability with
the workflow's `npm install --global @anthropic-ai/claude-code @openai/codex` command,
or run these host-only gates on the same pre-provisioned runner image. Do not silently
skip either validator after the commands are available.

Expected: all tests and both host installation/validation paths pass; the installed copy works from an unrelated checkout; the distributed file set is exact.

- [ ] **Step 5: Commit**

```bash
git add code-intel/skills/code-intel/SKILL.md code-intel/skills/code-intel/agents/openai.yaml code-intel/tests/test_packaging.py .github/workflows/code-intel.yml
git commit -m "docs: add shared code intelligence operating guide"
```

## Final Full Verification

- [ ] From a clean test environment with fake pinned binaries available, run the complete standard-library suite:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B code-intel/tests/test_code_intel.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -B code-intel/tests/test_packaging.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -B code-intel/tests/test_packaging.py PackagingTests.test_repository_owned_skill_validation -v
```

- [ ] Establish `claude` and `codex` with the workflow's Node/npm step (or use the same pre-provisioned host-validation runner), run `claude plugin validate code-intel`, then run the exact isolated Codex block from `.github/workflows/code-intel.yml`; assert the marketplace list contains `claude-essentials` and the plugin list contains `code-intel@claude-essentials` as encoded there.
- [ ] Confirm `PackagingTests.test_installed_layout_smoke` stages only `EXPECTED_DISTRIBUTABLE_FILES` under a temporary path containing spaces and shell metacharacters, runs from a separate Git checkout, covers `doctor`, both fake-server `serve` dispatch paths, all three hooks, and both Bash/write `hook-update` payloads, with `PYTHONDONTWRITEBYTECODE=1` throughout.
- [ ] Verify `git diff --check`, verify the exact distribution assertion passes, and inspect `git status --short` to ensure no generated indexes, state, locks, caches, or unrelated marketplace changes are tracked.
- [ ] Confirm acceptance: both hosts install the same `0.1.0` package; only explicit setup installs exact pins; every lifecycle path either proves fresh checkout-scoped indexes or fails open with fallback guidance; worktrees remain independent; same-root writers serialize; doctor is read-only; and release-please covers both manifests and the changelog.
