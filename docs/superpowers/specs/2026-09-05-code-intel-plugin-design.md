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
- automatically initialize missing indexes when a session starts in a Git
  repository;
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

MCP launch commands call `code_intel.py serve codegraph` or
`code_intel.py serve crg`. The dispatcher resolves the binaries from `PATH`
first and then from the standard mise shim directory. If a binary is absent or
has the wrong version, it emits one actionable diagnostic and exits without
installing anything. After tool installation, the host must start a new
session so its MCP processes can connect.

## Index Ownership and Branch Semantics

CodeGraph and CRG indexes belong to a checkout directory, not to a Git branch.
Switching branches in one checkout reuses the same `.codegraph` and
`.code-review-graph` directories. A completed sync replaces their view with
the current filesystem, but before that sync they can be stale or transiently
contain facts from both the previous and current checkout states.

Every linked worktree has a different root directory and therefore receives
its own index directories. Concurrent branch work must use worktrees; the
plugin does not emulate branch-scoped indexes inside one checkout.

The plugin stores a small hook state map in `PLUGIN_DATA`, falling back to
`CLAUDE_PLUGIN_DATA`. Entries are keyed by the canonical worktree root and
record the last observed Git `HEAD`. They never use `git-common-dir` as the
key, because linked worktrees that share a Git object database must remain
independent.

On `SessionStart`, the hook:

1. resolves the current Git worktree root;
2. verifies both binaries;
3. initializes either missing index in dependency order;
4. synchronizes existing indexes;
5. records the current `HEAD` only after both operations succeed.

After a Bash tool call, the hook compares the current `HEAD` with the stored
value. A change forces `codegraph sync` and `code-review-graph update`, even
when the working tree is clean. This covers checkout, switch, reset, merge,
rebase, and commit. Ordinary file-write hooks update CRG incrementally;
CodeGraph's watcher remains responsible for normal file edits.

Index initialization adds `.codegraph/` and `.code-review-graph/` to the
checkout's local Git exclude file. It never edits the repository `.gitignore`.

## Hooks and Instruction Delivery

`hooks/hooks.json` defines three fail-open command hooks:

- `SessionStart` calls `hook-status` for verification, initialization, and
  synchronization. Its concise stdout becomes session context.
- `UserPromptSubmit` calls `hook-prompt`. It combines CodeGraph's
  `prompt-hook` response with the shared CodeGraph/CRG routing instructions.
- `PostToolUse` calls `hook-update` after Bash and supported write tools.

The hook commands use `${CLAUDE_PLUGIN_ROOT}`, which both hosts provide for
plugin compatibility. The Python program accepts the minor input-shape
differences of both hosts and emits their shared `hookSpecificOutput` JSON
contract. Malformed input, unavailable tools, and indexing failures produce a
short diagnostic but never block the user's operation.

The injected routing guidance is deliberately compact:

- use CodeGraph first for verbatim symbol source, callers, callees, call paths,
  and dynamic dispatch;
- use CRG first for review, blast radius, affected flows, architecture,
  communities, semantic search, and refactoring;
- fall back to normal file/search tools only when the selected graph lacks the
  answer;
- do not use an index while its synchronization operation has failed.

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
kind, index presence, current `HEAD`, and stored indexed `HEAD`. It does not
inspect any legacy installation.

Explicit update commands never initialize missing indexes. Session-start
initialization applies only to a normal Git repository or linked worktree. A
recognized non-Git umbrella reports the missing scope and directs the agent to
request authorization for `setup-project`.

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

## Verification

Tests must prove behavior, not headings or incidental prose:

- repository and worktree discovery;
- umbrella detection and initialization order;
- idempotent local Git excludes;
- exact pinned install commands and version checks;
- MCP dispatch from an installed plugin path while preserving caller CWD;
- missing-tool diagnostics without implicit installation;
- session-start initialization of missing indexes;
- forced synchronization when `HEAD` changes in a clean checkout;
- no redundant full sync when `HEAD` and index state are current;
- fail-open behavior for malformed hook payloads and failed tools;
- common prompt-hook JSON for Claude and Codex;
- manifest identity and marketplace registration parity;
- release-please version coverage;
- the exact distributable file set.

Validation includes the skill validator, Codex plugin validator, Claude plugin
validator, Python unit tests, and an installed-layout smoke test staged in a
temporary directory.
