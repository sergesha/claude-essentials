---
name: code-intel
description: Use when installing, setting up, checking, updating, or repairing CodeGraph and code-review-graph, or when graph-backed source, call-path, review, impact, architecture, semantic search, or refactoring questions arise in a checkout or umbrella directory.
---

# Code intelligence

Use the shared plugin dispatcher on either host. Resolve `../../scripts/code_intel.py`
relative to this skill's directory to an absolute path; keep the working directory
in the user's target checkout. Quote the dispatcher and target paths in commands.
The examples below use `DISPATCHER` for that resolved path and `TARGET` for the
selected project directory.

| Intent | Command |
| --- | --- |
| Check the current checkout without changing it | `python3 -B "$DISPATCHER" doctor` |
| Check a particular project without changing it | `python3 -B "$DISPATCHER" project-status "$TARGET"` |
| Install the tested engines, after authorization | `python3 -B "$DISPATCHER" install-tools` |
| Initialize or repair the selected project | `python3 -B "$DISPATCHER" setup-project "$TARGET"` |
| Rebuild indexes when setup reports invalid existing indexes | `python3 -B "$DISPATCHER" setup-project "$TARGET" --force` |
| Set up an umbrella and its discovered repositories | `python3 -B "$DISPATCHER" setup-batch "$TARGET"` |
| Refresh the selected project | `python3 -B "$DISPATCHER" update-project "$TARGET"` |
| Refresh an umbrella and its discovered repositories | `python3 -B "$DISPATCHER" update-batch "$TARGET"` |

## Installation and ownership

Only `install-tools` installs engines. Obtain authorization for that installation;
it uses mise to install exactly `npm:@colbymchenry/codegraph@1.6.0` and
`pipx:code-review-graph@2.3.8`. If mise is unavailable, report that prerequisite.
Restart the host after installation. Then run `doctor` from the target checkout.
Hooks and MCP launchers never install or upgrade tools; version mismatches require
the explicit installation path.

Request authorization before setup-project on a non-Git umbrella.
The same authorization covers umbrella initialization through `setup-batch`;
confirm the intended directory and repositories before that broader operation.
An umbrella gets CodeGraph only, while each discovered Git checkout gets both
engines. Ordinary Git checkout setup and updates can proceed within the user's
existing task authorization.

Each checkout owns its indexes, including linked worktrees: select the worktree
root, never its shared Git metadata directory. Session, prompt, and post-tool hooks
prepare and refresh ordinary Git checkouts automatically and serialize writers for
the same root. Non-Git umbrella initialization remains explicit. A directory's
existence alone does not prove freshness; `project-status` must trust the success
marker against the current checkout, HEAD, exact tool versions, and index content.

On missing tools, stale indexes, locks, timeouts, or failed refreshes, hooks fail
open with fallback guidance. Use read-only diagnostics to choose setup or update;
do not claim graph evidence is fresh after a failed check. The package owns its
MCP and hook declarations. Keep user-level instruction and host configuration
files unchanged; no migration of previous installations is part of this workflow.

## Routing

Use CodeGraph first for symbol source, callers/callees, call paths, and dynamic dispatch.
Use code-review-graph first for review, blast radius, impact, and affected flows.
Use code-review-graph first for architecture, communities, semantic search, and refactoring.
If the selected graph cannot answer, fall back to normal file/search tools.
