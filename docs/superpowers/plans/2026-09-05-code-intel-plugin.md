# Code Intel Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a distributable `code-intel` plugin that gives Claude Code and Codex the same pinned CodeGraph/CRG setup, checkout-scoped index lifecycle, fail-open hooks, diagnostics, and routing guidance.

**Architecture:** A single standard-library Python program owns tool installation and dispatch, Git/worktree discovery, checkout/index fingerprinting, per-worktree state and locking, index setup/update, diagnostics, and the three hook adapters. Thin shared manifests, hooks, and skill instructions expose that program to both hosts; packaging tests enforce host parity, release metadata, validators, and installed-layout behavior.

**Tech Stack:** Python 3 standard library (`argparse`, `dataclasses`, `hashlib`, `json`, `pathlib`, `subprocess`, `tempfile`, platform file locking), `unittest`, JSON plugin/MCP/hook manifests, YAML skill metadata, GitHub Actions, release-please, `mise`, CodeGraph 1.6.0, code-review-graph 2.3.8.

**Spec:** `docs/superpowers/specs/2026-09-05-code-intel-plugin-design.md`

## Global Constraints

- The plugin name is `code-intel`; both host manifests and `code-intel/CHANGELOG.md` start at version `0.1.0` and share identity and descriptive metadata.
- The only supported install path is `mise use -g npm:@colbymchenry/codegraph@1.6.0` plus `mise use -g pipx:code-review-graph@2.3.8`; hooks and MCP dispatch never install or upgrade tools.
- `code-intel/scripts/code_intel.py` is Python-standard-library-only; every child is launched with an argv array, without a shell, and with a finite timeout inside a finite hook deadline.
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

Add `PackagingTests` methods named below. Parse JSON with `json.loads`, locate the repository with `Path(__file__).resolve().parents[2]`, and assert exact values rather than substrings:

```python
def test_manifest_identity_and_shared_entrypoints(self):
    self.assertEqual(claude["name"], codex["name"])
    self.assertEqual(claude["version"], codex["version"])
    self.assertEqual(codex["skills"], "./skills/")
    self.assertEqual(codex["hooks"], "./hooks/hooks.json")
    self.assertEqual(codex["mcpServers"], "./.mcp.json")

def test_marketplaces_register_only_the_package_source(self):
    self.assertEqual(claude_entry["source"], "./code-intel")
    self.assertEqual(codex_entry["source"], {"source": "local", "path": "./code-intel"})

def test_release_please_updates_both_manifest_versions(self):
    extra = release_config["packages"]["code-intel"]["extra-files"]
    self.assertEqual({item["path"] for item in extra}, {
        ".claude-plugin/plugin.json", ".codex-plugin/plugin.json"
    })
    self.assertEqual(release_state["code-intel"], "0.1.0")
```

Also assert two MCP server entries, exactly three hook event keys, `PostToolUse` matching `Bash|Write|Edit|NotebookEdit|apply_patch`, workflow path filters covering `code-intel/**`, both marketplaces, both release files, and its own workflow, and no unrelated marketplace entry changes.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 code-intel/tests/test_packaging.py -v`

Expected: FAIL because `code-intel` manifests and registrations do not exist.

- [ ] **Step 3: Add the minimum valid package and release metadata**

Use identical identity fields in both manifests. Make Claude declare its hooks and two MCP commands directly, make Codex point to the three shared paths, and make `.mcp.json` launch the Python dispatcher from `${CLAUDE_PLUGIN_ROOT}` while preserving the caller's working directory. Define fail-open command hooks with finite timeouts. Add only one new entry to each marketplace and one package entry to each release-please file. The dedicated workflow runs `python3 code-intel/tests/test_packaging.py -v` on the path filters asserted above.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run: `python3 code-intel/tests/test_packaging.py -v`

Expected: PASS for identity, entrypoints, hook schema, marketplace registration, release coverage, and CI path coverage.

- [ ] **Step 5: Commit**

```bash
git add code-intel/.claude-plugin/plugin.json code-intel/.codex-plugin/plugin.json code-intel/.mcp.json code-intel/hooks/hooks.json code-intel/CHANGELOG.md code-intel/tests/test_packaging.py .claude-plugin/marketplace.json .agents/plugins/marketplace.json release-please-config.json .release-please-manifest.json .github/workflows/code-intel.yml
git commit -m "feat: register code intelligence plugin package"
```

### Task 2: Tool Contract, Safe Process Runner, MCP Dispatch, and Doctor

**Files:**
- Create: `code-intel/scripts/code_intel.py`
- Create: `code-intel/tests/test_code_intel.py`

**Interfaces:**
- Consumes: `PLUGIN_ROOT = Path(__file__).resolve().parents[1]`, caller environment, and subprocess argv arrays.
- Produces: `ToolSpec(name: str, executable: str, version: str, mise_package: str)`, `run_child(argv: Sequence[str], *, cwd: Path | None, timeout: float, stdout_protocol: bool = False) -> subprocess.CompletedProcess[str]`, `resolve_verified_tool(spec: ToolSpec) -> Path`, and CLI commands `install-tools`, `serve {codegraph,crg}`, `doctor`, and `project-status PATH`.

- [ ] **Step 1: Write failing tool and diagnostic tests**

Create subprocess fakes that record `argv`, `cwd`, `timeout`, `env`, and whether `shell` was requested. Add exact tests for pinned `mise use -g` argv; PATH-before-mise resolution; exact, wrong, and unparseable versions; missing `mise`; installed-plugin paths containing spaces and shell metacharacters; caller-CWD preservation; server stdout passthrough; all dispatcher diagnostics on stderr; and doctor/project-status leaving a before/after recursive filesystem snapshot identical.

```python
def test_install_tools_uses_exact_pins(self):
    rc = module.main(["install-tools"])
    self.assertEqual(fake.calls[:2], [
        ["mise", "use", "-g", "npm:@colbymchenry/codegraph@1.6.0"],
        ["mise", "use", "-g", "pipx:code-review-graph@2.3.8"],
    ])
    self.assertEqual(rc, 0)

def test_serve_preserves_cwd_and_never_uses_shell(self):
    rc = module.main(["serve", "codegraph"])
    self.assertEqual(fake.calls[-1].cwd, invocation_cwd)
    self.assertFalse(fake.calls[-1].shell)
    self.assertEqual(rc, 0)
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 code-intel/tests/test_code_intel.py ToolContractTests DoctorTests -v`

Expected: FAIL because the controller and its commands do not exist.

- [ ] **Step 3: Implement only tool resolution, child supervision, dispatch, and read-only reporting**

Define the two immutable tool specs, exact version parsers, a single argv-only child runner that terminates and reaps the process group on timeout, and one-diagnostic error paths. `install-tools` alone invokes `mise use -g`, then verifies both tools. `serve` verifies before replacing/launching the requested MCP server, forwards protocol stdout unchanged, and emits diagnostics only to stderr. `doctor` and `project-status` use read-only existence/access/stat checks and return non-zero for any missing, wrong, unparseable, unusable, missing-index, or stale condition; neither command creates a directory, lock, probe, state, or index.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run: `python3 code-intel/tests/test_code_intel.py ToolContractTests DoctorTests -v`

Expected: PASS, including hostile path/CWD cases and unchanged diagnostic filesystem snapshots.

- [ ] **Step 5: Commit**

```bash
git add code-intel/scripts/code_intel.py code-intel/tests/test_code_intel.py
git commit -m "feat: add pinned tool dispatch and diagnostics"
```

### Task 3: Repository Discovery, Explicit Setup, and Updates

**Files:**
- Modify: `code-intel/scripts/code_intel.py`
- Modify: `code-intel/tests/test_code_intel.py`

**Interfaces:**
- Consumes: `run_child(...)` and verified `ToolSpec` instances from Task 2.
- Produces: `RepoScope(kind: Literal["repository", "worktree", "umbrella", "none"], root: Path, repositories: tuple[Path, ...])`, `discover_scope(path: Path, *, explicit: bool) -> RepoScope`, `ensure_local_excludes(root: Path) -> None`, `initialize_project(scope: RepoScope, *, force: bool) -> None`, `update_project(root: Path) -> None`, and CLI commands `setup-project PATH [--force]`, `setup-batch BASE`, `update-project PATH`, and `update-batch BASE`.

- [ ] **Step 1: Write failing discovery/setup/update tests**

Add `DiscoveryTests` and `IndexCommandTests` covering a normal repository, linked worktree, non-Git umbrella with nested repositories, unrelated non-Git directory, canonical paths with spaces/metacharacters, deterministic nested repository ordering, dependency order (CodeGraph before CRG), missing-index refusal for explicit updates, incremental CRG update, and idempotent writes to `git rev-parse --git-path info/exclude` without touching `.gitignore`.

```python
def test_umbrella_setup_initializes_children_then_codegraph_umbrella(self):
    scope = module.discover_scope(base, explicit=True)
    module.initialize_project(scope, force=False)
    self.assertEqual(fake.engine_operations, [
        ("codegraph-init", child_a), ("crg-init", child_a),
        ("codegraph-init", child_b), ("crg-init", child_b),
        ("codegraph-init", base),
    ])

def test_update_project_refuses_missing_indexes(self):
    with self.assertRaisesRegex(module.UserError, "setup-project"):
        module.update_project(repo)
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 code-intel/tests/test_code_intel.py DiscoveryTests IndexCommandTests -v`

Expected: FAIL because discovery and index commands are absent.

- [ ] **Step 3: Implement discovery and explicit index commands**

Resolve canonical worktree roots with Git, distinguish linked worktrees without using the common directory as identity, and recognize umbrellas only for explicit setup/batch operations. Initialize missing child indexes in stable path order and engine dependency order, then create only the umbrella CodeGraph index. Make `--force` rebuild explicitly, make update commands reject absent indexes, keep CRG updates incremental, and update only each checkout's local exclude file with the two exact generated-directory entries.

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

- [ ] **Step 1: Write failing state/fingerprint/concurrency tests**

Add `StateTests`, `FingerprintTests`, and `ConcurrencyTests`. Cover deterministic environment precedence; absent/unwritable/corrupt storage; root embedded in and validated against state; independent state/locks for linked worktrees; atomic replacement; pending/failed trust; same-root serialization; finite lock deadline; read-only observation without lock creation; tracked/untracked file edits and deletions; symlink targets without traversal; exclusions; persistent index journal inclusion; transient lock/PID exclusion; and mutation during either fingerprint pass.

```python
def test_first_nonempty_data_variable_never_falls_back(self):
    env = {"PLUGIN_DATA": str(unwritable), "CLAUDE_PLUGIN_DATA": str(writable)}
    with self.assertRaises(module.UnusableDataLocation):
        module.select_data_location(env, read_only=False)
    self.assertFalse(any(writable.iterdir()))

def test_worktrees_have_independent_digest_keys(self):
    self.assertNotEqual(module.state_path(worktree_a, data),
                        module.state_path(worktree_b, data))
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 code-intel/tests/test_code_intel.py StateTests FingerprintTests ConcurrencyTests -v`

Expected: FAIL because state, fingerprint, and lock primitives are absent.

- [ ] **Step 3: Implement atomic state, OS locking, and bounded fingerprints**

Hash the canonical root into separate state/lock names while storing the full root for collision/mismatch validation. Use an OS lock released by process exit and a monotonic deadline. Write `pending` before mutation and publish `success` only by same-directory atomic replacement. Derive checkout inputs from Git's tracked and non-ignored untracked file sets, hash path/type/content in sorted order, hash symlink targets, and restart/fail if the input set changes mid-capture. Hash persistent index content/configuration including required journals while excluding transient locks and process identity files.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run: `python3 code-intel/tests/test_code_intel.py StateTests FingerprintTests ConcurrencyTests -v`

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
- Produces: `ensure_ready(path: Path, *, force_sync: bool, deadline: float) -> ReadinessResult`, host-compatible `hookSpecificOutput` JSON, and CLI commands `hook-status`, `hook-prompt`, and `hook-update`.

- [ ] **Step 1: Write failing readiness and hook-adapter tests**

Add `ReadinessTests` and `HookTests` covering the complete verification sequence. Include session initialization; prompt/write/Bash discovery of a new worktree; deleted-index recreation; clean branch switch; same-`HEAD` restore/reset; arbitrary Bash mutations; matching-marker reuse; offline edit and index mutation; failed/pending state; captured `HEAD` or checkout change during sync; lock, child, fingerprint, and overall deadlines; descendant termination/reaping; malformed Claude/Codex payloads; and common output JSON.

```python
def test_every_resolvable_bash_post_tool_event_forces_sync(self):
    response = invoke_hook("hook-update", bash_payload, cwd=repo)
    self.assertEqual(fake.operations[-2:], ["codegraph-sync", "crg-update"])
    self.assertFalse(response["hookSpecificOutput"].get("block", False))

def test_unready_prompt_uses_fallback_without_prompt_hook(self):
    response = invoke_hook("hook-prompt", prompt_payload, fail_update=True)
    self.assertNotIn("codegraph-prompt-hook", fake.operations)
    self.assertIn("normal file/search tools", json.dumps(response))
    self.assertNotIn("ready", successful_marker_statuses())
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 code-intel/tests/test_code_intel.py ReadinessTests HookTests -v`

Expected: FAIL because readiness orchestration and hook adapters are absent.

- [ ] **Step 3: Implement one readiness state machine and three thin adapters**

Under the per-root lock: verify tools; inspect index presence; compare versions, captured `HEAD`, checkout fingerprint, and both index fingerprints; reuse only an exact successful marker unless the event forces sync; otherwise write pending, capture inputs, initialize missing indexes in dependency order, sync CodeGraph, incrementally update CRG, recapture index fingerprints, and verify that `HEAD` and checkout inputs remain unchanged before publishing success. Keep the lock through CodeGraph `prompt-hook`. On any error/timeout/mutation, terminate descendants, invalidate success if state is writable, and emit concise non-blocking fallback JSON. Do not call `prompt-hook` without established freshness and never treat its earlier stdout as current after a later failure.

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

Extend `PackagingTests` to assert the exact relative file set under `code-intel/`, skill frontmatter name `code-intel`, implicit invocation, exact pinned dependencies, restart-after-install guidance, explicit umbrella authorization, the four routing/fallback rules, and absence of legacy migration/global-config instructions. Add an installed-layout fixture that copies only the asserted distribution into a temporary directory, runs `doctor`, exercises MCP dispatch with a fake verified binary, and invokes all three hooks from a different CWD. Add an isolated Codex smoke command to CI that sets temporary host data, adds the repository marketplace, and adds `code-intel@claude-essentials`.

```python
def test_exact_distributable_file_set(self):
    actual = {p.relative_to(package).as_posix() for p in package.rglob("*") if p.is_file()}
    self.assertEqual(actual, EXPECTED_DISTRIBUTABLE_FILES)

def test_installed_layout_keeps_invocation_cwd(self):
    completed = run_installed_copy("serve", "codegraph", cwd=consumer_repo)
    self.assertEqual(read_fake_server_cwd(), str(consumer_repo))
    self.assertEqual(completed.returncode, 0)
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 code-intel/tests/test_packaging.py -v`

Expected: FAIL because the shared skill, agent metadata, exact file contract, and installed-layout checks are incomplete.

- [ ] **Step 3: Add the skill and finish authoritative validation**

Write concise host-neutral instructions that map each user intent to the exact CLI, require approval only for `install-tools` and explicit umbrella setup, tell users to restart after install, explain checkout/worktree ownership, and state the four routing/fallback rules verbatim in meaning. Configure `openai.yaml` for implicit invocation. Make CI run unit/packaging tests, the skill validator, Claude plugin validator, and temporary Codex marketplace-add/plugin-add smoke test; do not invoke or modify a generic validator that rejects supported Codex hooks metadata.

- [ ] **Step 4: Run the focused tests and validators and confirm GREEN**

Run:

```bash
python3 code-intel/tests/test_packaging.py -v
python3 code-intel/tests/test_code_intel.py -v
claude plugin validate code-intel
```

Run the repository's available skill validator against `code-intel/skills/code-intel`, then execute the exact temporary-host Codex add/install commands encoded in `.github/workflows/code-intel.yml`.

Expected: all tests and both host installation/validation paths pass; the installed copy works from an unrelated checkout; the distributed file set is exact.

- [ ] **Step 5: Commit**

```bash
git add code-intel/skills/code-intel/SKILL.md code-intel/skills/code-intel/agents/openai.yaml code-intel/tests/test_packaging.py .github/workflows/code-intel.yml
git commit -m "docs: add shared code intelligence operating guide"
```

## Final Full Verification

- [ ] From a clean test environment with fake pinned binaries available, run the complete standard-library suite:

```bash
python3 code-intel/tests/test_code_intel.py -v
python3 code-intel/tests/test_packaging.py -v
```

- [ ] Run the repository skill validator, `claude plugin validate code-intel`, and the isolated Codex marketplace-add/plugin-add smoke test specified by `.github/workflows/code-intel.yml`.
- [ ] Stage an installed copy under a temporary path containing spaces and shell metacharacters; from a separate Git worktree run `doctor`, both `serve` dispatch paths with fake servers, `hook-status`, `hook-prompt`, and Bash/write `hook-update` payloads.
- [ ] Verify `git diff --check`, verify the exact distribution assertion passes, and inspect `git status --short` to ensure no generated indexes, state, locks, caches, or unrelated marketplace changes are tracked.
- [ ] Confirm acceptance: both hosts install the same `0.1.0` package; only explicit setup installs exact pins; every lifecycle path either proves fresh checkout-scoped indexes or fails open with fallback guidance; worktrees remain independent; same-root writers serialize; doctor is read-only; and release-please covers both manifests and the changelog.

