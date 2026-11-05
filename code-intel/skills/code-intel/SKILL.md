---
name: code-intel
description: Use when installing, upgrading, repairing, configuring, checking, initializing, or updating CodeGraph or code-review-graph, or using their graphs for code exploration, review, impact analysis, architecture, or refactoring. Includes umbrella directories, missing current-project indexes, and Claude Code/Codex integration.
---

# Code Intel Setup

Use the bundled script for every operation. Do not recreate its discovery, initialization, or update loops.

Resolve `../../scripts/code_intel.py` relative to this skill directory to an
absolute path. The examples use `CODE_INTEL` for that path; quote it and keep the
working directory in the target project.

## Operations

- Install missing tools: `python3 "$CODE_INTEL" install-tools`
- Complete plugin installation/update: `python3 "$CODE_INTEL" ensure-tools`
- Upgrade tools and check the installation: `python3 "$CODE_INTEL" upgrade --base BASE`
- Initialize one project or umbrella: `python3 "$CODE_INTEL" setup-project PATH`
- Initialize all eligible projects: `python3 "$CODE_INTEL" setup-batch BASE`
- Update one initialized project or umbrella: `python3 "$CODE_INTEL" update-project PATH`
- Update every initialized index: `python3 "$CODE_INTEL" update-batch BASE`
- Check the installation: `python3 "$CODE_INTEL" status --base BASE`
- Check one project without changing it: `python3 "$CODE_INTEL" project-status PATH`

Use `setup-batch` or `update-batch` whenever several projects are in scope. They are the only supported batch methods.

## Initialization contract

`setup-batch` recursively discovers Git repositories under `BASE`, excluding `node_modules`, `.venv`, `vendor`, `.cache`, and `__pycache__`. It skips repositories with fewer than five recognized code files within depth five.

For each eligible repository it initializes CodeGraph, builds CRG, registers CRG under the directory name, and locally excludes both generated directories through Git's `info/exclude`. A failure stops later steps for that repository; batch processing continues.

It then initializes CodeGraph-only umbrella indexes. An umbrella has no `.git`, contains an AI marker, contains at least two Git repositories within depth three, and lacks `.codegraph`. Never register an umbrella with CRG.

Explicit `setup-project` on an umbrella initializes every non-excluded nested Git repository, then the umbrella CodeGraph index. `--force` is required for a markerless umbrella.

## Session-start discovery

Claude Code and Codex use the same `SessionStart` command, `hook-start`. Before
checking indexes, it calls `ensure-tools`: on first activation of each plugin
release it installs or upgrades both tools, including CRG's `communities` extra.
This is part of the code-intel installation/update operation. When updating this
plugin yourself, run `ensure-tools` from the updated installed copy immediately;
do not report the update complete based only on the plugin version.

Both clients share a locked success receipt. It is saved only after both PATH
commands work and igraph imports and runs Leiden in CRG's own Python environment.
Later sessions recheck that environment and skip installers when the receipt
still matches. Changed or broken tools trigger provisioning again.
Missing package managers, failed installations, or failed capability checks are
reported; the hook remains fail-open and does not record success or initialize
indexes on failure. Resolve the reported problem and retry `ensure-tools`.
Do not install package managers implicitly. Restart the host for MCP pickup;
do not claim an already running MCP server reloaded until the host confirms it.

When both tools are available, in a Git repository or linked worktree it
automatically initializes both indexes when
either is missing. The prompt and post-tool hooks provide the same repair path,
so a worktree opened after session start is covered. Hooks remain fail-open.

For a non-Git umbrella, the hook only reports missing indexes. Initialize an
umbrella with `setup-project` when setup or repair is in scope; otherwise ask
first because eligible nested repositories are included.

## Update contract

Explicit update operations never initialize. CodeGraph uses `sync` only when
`.codegraph` exists. CRG uses `update` only when `.code-review-graph` exists.
Plugin hooks first initialize missing indexes for the current Git repository
or worktree, then use incremental updates on later changes.

## Plugin integration

Install `code-intel` from the `claude-essentials` marketplace in Claude Code or
Codex. The plugin supplies both MCP servers, hooks, and this skill. It does not
rewrite global instructions, hooks, MCP settings, or skill symlinks.

Python 3.11+ is required for the setup script. Both MCP servers and setup commands
find `codegraph` and `code-review-graph` through `PATH`; there are no
package-manager-specific paths. Existing installations from any manager work
when their executables are available in the host's environment.

For automated provisioning, `install-tools` and `upgrade` use npm for CodeGraph
and an available uv or pipx for `code-review-graph[communities]`. Existing
mise-managed tools are updated through their declaring mise config, preserving
version requests and existing extras; `communities` is added to CRG's extras.
The `communities`
extra installs igraph in the tool's own Python environment for graph-based
community detection instead of the file-based fallback.
`install-tools` installs only missing tools and leaves existing installations
unchanged; `upgrade` explicitly updates both and includes the `communities` extra.
For an existing CRG installation without igraph, use `ensure-tools`;
repeating `install-tools` will skip it. With the user's preferred installer,
include `communities` in that same tool environment.
They check only the installers needed for the requested work and
do not install package managers. Alternatively install the tools with the user's
preferred manager.
Ensure both commands are on the PATH inherited by Claude Code/Codex, including
when launched as desktop applications, then restart the host.
`status` checks tool versions, the CRG registry, and discovered CodeGraph indexes.
`project-status` reports index presence.

Both clients call `hook-prompt` for `UserPromptSubmit`. It converts CodeGraph
context to the shared `hookSpecificOutput.additionalContext` JSON contract.

## Code intelligence routing

| Need | Use first |
| --- | --- |
| Symbol source, callers/callees, call paths, dynamic dispatch | CodeGraph |
| Review, blast radius, impact, affected flows | code-review-graph |
| Architecture, communities, semantic search, refactoring | code-review-graph |

If the selected graph cannot answer, fall back to normal file/search tools.

If `codegraph_explore` reports no `.codegraph/` index and the current directory is not a Git repository but contains Git sub-repositories, use the `code-intel` skill to run `setup-project` for that directory, then retry. The index persists.
