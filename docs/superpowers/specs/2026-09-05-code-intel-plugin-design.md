# Code Intel Plugin Design

## Purpose

Add a distributable `code-intel` plugin to `claude-essentials`. The plugin
combines CodeGraph and code-review-graph (CRG), exposes the same operating
model to Claude Code and Codex, installs through each host's repository
marketplace, and keeps the current checkout's indexes ready for use.

The existing user-level `code-intel-setup` skill is the behavioral reference,
not the package template. Its repository discovery, index lifecycle, hook
adapter, atomic-write helpers, and tests are reused. Its global configuration
rewrites are replaced by plugin-native manifests, MCP declarations, and hooks.

## Scope

The plugin will:

- install from the existing Claude marketplace and Codex repo marketplace;
- distribute one shared skill, one shared Python control program, and shared
  lifecycle hooks;
- declare CodeGraph and CRG as bundled MCP servers for both hosts;
- explicitly install tested tool versions when the user invokes setup;
- automatically initialize missing indexes at session start and when later
  lifecycle hooks encounter a Git repository or linked worktree;
- initialize nested repositories plus a CodeGraph-only umbrella index when the
  user explicitly selects an umbrella workspace;
- keep indexes synchronized after file edits and Git branch changes;
- provide read-only doctor and project-status operations;
- preserve the routing rule that CodeGraph handles symbol/call-path questions
  and CRG handles review, impact, architecture, and semantic-search questions.

The plugin will not:

- detect, migrate, repair, or remove an older `code-intel-setup` installation;
- edit user-level `CLAUDE.md`, `AGENTS.md`, MCP configuration, or hook files;
- create compatibility aliases or symlinks for the older skill;
- silently download or upgrade executables from a lifecycle hook;
- index a non-Git umbrella automatically;
- provide simultaneous branch snapshots inside one physical checkout.

## Package Layout

```text
code-intel/
├── .claude-plugin/plugin.json
├── .codex-plugin/plugin.json
├── .mcp.json
├── hooks/hooks.json
├── skills/code-intel/
│   ├── SKILL.md
│   └── agents/openai.yaml
├── scripts/code_intel.py
├── tests/
│   ├── test_code_intel.py
│   └── test_packaging.py
└── CHANGELOG.md
```

The Claude manifest declares the skill directory implicitly, the shared hooks
file explicitly, and two MCP launch commands rooted at
`${CLAUDE_PLUGIN_ROOT}`. The Codex manifest points to `./skills/`,
`./hooks/hooks.json`, and `./.mcp.json`. The Codex marketplace entry points to
`./code-intel` relative to the repository marketplace root.

Both manifests use plugin name `code-intel`, initial version `0.1.0`, and the
same identity and descriptive metadata. The skill uses the same name and
allows implicit invocation.

## Tool Dependency Contract

The first release supports the existing tested installation path through
`mise` and pins exact versions:

- `npm:@colbymchenry/codegraph@1.6.0`
- `pipx:code-review-graph@2.3.8`

`code_intel.py install-tools` is an explicit, user-authorized operation. It
invokes `mise use -g` for the two pinned packages, verifies both versions, and
returns a non-zero status if `mise` is absent or either version differs.
Lifecycle hooks never invoke this operation.

Every non-install child and MCP launch sets `CODEGRAPH_NO_DOWNLOAD=1`,
`NODE_DISABLE_COMPILE_CACHE=1`, and `CODEGRAPH_INSTALL_DIR` to `os.devnull`.
CodeGraph 1.6.0 otherwise uses and prunes cached fallback bundles before checking
its download flag. An unavailable optional platform bundle must fail with an
installation diagnostic without creating cache files. Only `install-tools`
permits dependency installation. Directory-sensitive tool versions are observed
in each canonical target root and those observed versions populate freshness state.

MCP launch commands call `code_intel.py serve codegraph` or
`code_intel.py serve crg`. The dispatcher resolves the binaries from `PATH`
first and then from the standard mise shim directory. If a binary is absent,
has the wrong version, or returns an unparseable version, it emits one
actionable diagnostic to stderr and exits non-zero without
installing anything. After tool installation, the host must start a new
session so its MCP processes can connect.

All child processes receive argument arrays without a shell; paths are never
interpolated into shell commands. MCP stdout is reserved for protocol traffic.

## Index Ownership and Branch Semantics

CodeGraph and CRG indexes belong to a checkout directory, not to a Git branch.
Switching branches in one checkout reuses the same `.codegraph` and
`.code-review-graph` directories. A completed sync replaces their view with
the current filesystem, but before that sync they can be stale or transiently
contain facts from both the previous and current checkout states.

Every linked worktree has a different root directory and therefore receives
its own index directories. Concurrent branch work must use worktrees; the
plugin does not emulate branch-scoped indexes inside one checkout.

The plugin selects the non-empty `PLUGIN_DATA` value first, or otherwise the
non-empty `CLAUDE_PLUGIN_DATA` value. It does not silently select the second
location when the first is unusable. State and lock files live only under this
selected data directory, never in the plugin installation or checkout. An
absent or unwritable data directory, corrupt state, or failed state write
leaves readiness untrusted and makes the hook fail open with a diagnostic.
Hooks do not repair corrupt state or use it to skip work.

Each canonical worktree root has its own atomically replaced state file and
lock file, named by a digest of that root; the state also stores the canonical
root for validation. They never use `git-common-dir` as the key. Different
worktrees can update concurrently without overwriting one another's state.
An operating-system lock serializes all plugin initialization, update, and
lifecycle freshness checks for the same root, including explicit setup/update
commands. Read-only diagnostics observe state without creating/acquiring locks
and report pending or concurrently changing state as untrusted.
Lock acquisition has a finite deadline and process exit releases the lock.
The lock does not assume that Git, user edits, or engine watchers obey it.

A successful freshness marker contains the captured `HEAD`, exact engine
versions, a checkout content fingerprint, and fingerprints of both indexes.
Checkout fingerprinting hashes sorted paths, file types, and contents for
tracked and non-ignored untracked files, including indexing configuration;
it detects deletion and hashes symlink targets without following them. Git
administrative files and the generated index directories are excluded.
Index fingerprinting hashes persistent index contents and configuration,
including any database journal needed to interpret them; transient lock and
process identity files are excluded. Missing or unreadable inputs invalidate
the marker. This is a bounded local fingerprint pass, not a second indexer or
a background service. Neither `HEAD` alone, Git cleanliness, nor timestamps
alone establish freshness.

All three lifecycle hooks resolve the current Git worktree root from the
host's effective working directory and use the same readiness procedure:

1. select usable state storage, acquire the root lock, and verify both binaries;
2. inspect index presence and compare the current checkout/index fingerprints,
   `HEAD`, and versions with the successful marker;
3. if both indexes exist and all values match, reuse them unless this is a
   relevant post-tool update, which requires synchronization;
4. otherwise atomically mark readiness pending, capture `HEAD` and the checkout
   fingerprint, initialize either missing index in dependency order, and run
   `codegraph sync` and incremental `code-review-graph update`;
5. capture the resulting index fingerprints and recheck the checkout
   fingerprint and `HEAD`; a successful marker is eligible only if both engines
   succeeded and the captured checkout and `HEAD` are unchanged.

Keep the lock until the hook's remaining work completes, including CodeGraph's
`prompt-hook` when applicable, and recheck freshness before publishing success
and returning graph guidance. A failure while holding the lock invalidates any
existing successful marker when state remains writable. A failure to acquire
the lock returns fallback guidance without modifying another operation's state.

Any failure, timeout, pending operation, or change during synchronization
leaves readiness untrusted; never associate a completed sync with a newer
`HEAD` read afterward. A later successful synchronization can restore trust
after an ordinary failed/pending operation. If input changes while a
fingerprint is being captured, fail open without publishing success.

This procedure applies to worktrees created or first used after
`SessionStart`, and to indexes deleted mid-session. On session start and
prompt submission, a matching marker avoids a redundant full sync, while
content fingerprints detect offline edits even with unchanged `HEAD`.

Every `PostToolUse` Bash event with a resolvable Git worktree is relevant and
forces synchronization, even with unchanged `HEAD` and a clean working tree.
No command-name classifier or dirty-only filter may skip that update. This
covers checkout, switch, reset, restore, merge, rebase, commit, and arbitrary
Bash file mutations. Supported file-write events likewise ensure both indexes
are present and synchronize them before recording success. CodeGraph's
watcher continues to handle normal edits between hooks; a hook's explicit sync
confirms completion before declaring both indexes ready. CRG updates remain
incremental.

Index initialization adds `.codegraph/` and `.code-review-graph/` to the
checkout's local Git exclude file. It never edits the repository `.gitignore`.

## Hooks and Instruction Delivery

`hooks/hooks.json` defines three fail-open command hooks:

- `SessionStart` calls `hook-status` for verification and the shared readiness
  procedure. Its concise stdout becomes session context.
- `UserPromptSubmit` calls `hook-prompt`. It combines CodeGraph's
  raw `<codegraph_context ...>...</codegraph_context>` response (or successful
  empty stdout for a no-op) with the shared CodeGraph/CRG routing instructions
  only after the shared readiness procedure establishes freshness.
- `PostToolUse` calls `hook-update` after Bash and supported write tools.

The hook commands use `${CLAUDE_PLUGIN_ROOT}`, which both hosts provide for
plugin compatibility. The Python program accepts the minor input-shape
differences of both hosts and emits their shared `hookSpecificOutput` JSON
contract. Malformed input, unavailable tools, and indexing failures produce a
short diagnostic but never block the user's operation.

Every subprocess launched by a hook, including Git discovery, version checks,
initialization, updates, and `prompt-hook`, has a finite timeout within a finite
overall hook deadline. Fingerprinting and lock acquisition share that overall
deadline. Timeout terminates and reaps the child process and any descendants,
does not leave an index writer running after releasing the root lock, and
returns a fail-open response without a successful marker. Successful stdout
from an earlier child is insufficient to claim readiness after a later failure.

The injected routing guidance is deliberately compact:

- use CodeGraph first for verbatim symbol source, callers, callees, call paths,
  and dynamic dispatch;
- use CRG first for review, blast radius, affected flows, architecture,
  communities, semantic search, and refactoring;
- fall back to normal file/search tools when the selected graph lacks the
  answer or readiness cannot be established;
- do not use an index when readiness is missing, pending, failed, timed out,
  or stale; use normal file/search tools until readiness is established.

When readiness cannot be established, the prompt hook emits concise fallback
guidance rather than recommending an unavailable or stale graph, and does not
invoke CodeGraph's `prompt-hook`. A failed or timed-out `prompt-hook` also
fails open without presenting its output as current routing context.

## Control Program

`scripts/code_intel.py` remains Python-standard-library-only and exposes:

```text
install-tools
doctor
project-status PATH
setup-project PATH [--force]
setup-batch BASE
update-project PATH
update-batch BASE
serve {codegraph,crg}
hook-status
hook-prompt
hook-update
```

`doctor` is read-only. It reports Python, mise, exact tool versions, executable
resolution, plugin-root resolution, writable plugin data, current repository
kind, index presence, current `HEAD`, stored indexed `HEAD`, and marker
trust/freshness. It does not create directories, lock files, probe files,
indexes, or state, or repair corrupt state. Writability reporting uses
read-only metadata/access checks and is explicitly best effort. Missing tools,
wrong or unparseable versions, unusable state, and missing/stale indexes are
reported as unhealthy with a non-zero exit status. It does not inspect any
legacy installation.

Explicit update commands never initialize missing indexes. Automatic hook
initialization applies only to a normal Git repository or linked worktree. A
recognized non-Git umbrella reports the missing scope and directs the agent to
request authorization for `setup-project`.

Explicit `setup-project --force` rebuilds an existing CodeGraph index with
`codegraph index`; `codegraph init` is reserved for a missing index because
1.6.0 treats init on an existing database as a no-op. CRG rebuilds with `build`.

## Installation and Release

Claude Code installation remains:

```text
/plugin marketplace add sergesha/claude-essentials
/plugin install code-intel@claude-essentials
```

Codex installation uses the repository marketplace:

```text
codex plugin marketplace add sergesha/claude-essentials --json
codex plugin add code-intel@claude-essentials --json
```

After plugin installation, the user invokes the skill's setup operation,
approves the two pinned tool installations, restarts the host, and reviews the
plugin hooks where the host requires trust approval.

`code-intel` is an independent release-please package. Both host manifests and
the package changelog share its version. A dedicated CI workflow runs on
changes to the plugin, both marketplace files, release metadata, and its own
workflow.
Validation derives the expected version from the current package manifests and
requires parity with release-please state and both installed host manifests.
The initial version remains `0.1.0`; later release bumps must pass the same gates.

## Verification

Tests must prove behavior, not headings or incidental prose:

- repository and worktree discovery;
- umbrella detection and initialization order;
- idempotent local Git excludes;
- exact pinned install commands and version checks;
- MCP dispatch from an installed plugin path while preserving caller CWD;
- argv-based execution with paths containing spaces and shell metacharacters;
- missing/wrong/unparseable version diagnostics without implicit installation,
  unhealthy doctor status, and MCP diagnostics confined to stderr;
- session-start initialization of missing indexes, plus prompt/write/Bash hooks
  discovering a new worktree or recreating indexes removed mid-session;
- forced synchronization when `HEAD` changes in a clean checkout;
- an indexed edit followed by same-`HEAD` restore/reset to a clean checkout,
  and arbitrary Bash file mutations, all forcing synchronization;
- independent concurrent worktrees retaining their own markers and same-root
  operations serializing through completion;
- `HEAD` or checkout changes during sync preventing a successful marker;
- no redundant full sync with matching checkout/index fingerprints, plus
  offline same-`HEAD` edits, index changes, and failed/pending state forcing
  revalidation and synchronization;
- deterministic data-variable precedence and fail-open behavior for absent,
  unwritable, or corrupt storage, without writes to plugin or checkout state;
- doctor leaving the filesystem unchanged, including when state is absent;
- fail-open behavior for malformed hook payloads, failed tools, lock deadline
  expiry, and subprocess/fingerprint deadlines, with no successful marker or
  surviving index writer after timeout;
- prompt fallback when readiness cannot be established, without stale graph
  recommendations;
- common prompt-hook JSON for Claude and Codex;
- manifest identity and marketplace registration parity;
- release-please version coverage;
- the exact distributable file set.

Validation includes the skill validator, repository packaging/schema tests for
both host manifests (including shared hooks), the Claude plugin validator,
Python unit tests, and an installed-layout smoke test staged in a temporary
directory. Codex validation uses an isolated marketplace-add/plugin-add smoke
test with temporary host data; it does not assume a `codex plugin validate`
subcommand. A bundled generic validator that rejects supported Codex `hooks`
metadata is not authoritative: retain the hooks declaration, verify it through
the repository schema tests and installed-layout smoke test, and do not modify
the global validator.
