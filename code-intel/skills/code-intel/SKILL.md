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

Claude Code and Codex use the same plugin `SessionStart` command. It first checks
whether `codegraph` and `code-review-graph` are available on PATH, even outside a
Git repository or when indexes already exist. If a tool is missing, it reports
the missing commands and asks the agent to offer installation; it does not run
installers or initialize indexes.

On that notification, tell the user which tools are missing and offer to install
them. After consent, run `install-tools` yourself; do not make the user discover
or type the installation command. An explicit request to install is consent.
If they decline, continue with normal search/file tools without installing.
If prerequisites for installation are missing, explain the reported prerequisite
and agree on provisioning it or using the user's preferred installer.
After installation, verify tool availability with `status --base PATH`, resume
the requested project setup, and ask the user to restart the host for MCP pickup.
Do not claim MCP is connected until the host confirms it.

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
and an available uv or pipx for code-review-graph. `install-tools` installs only
missing tools and leaves existing installations unchanged; `upgrade` explicitly
updates both. They check only the installers needed for the requested work and
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
