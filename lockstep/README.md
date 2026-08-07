# lockstep

Flow-enforcement engine for coding agents: declarative yamlgraph recipes, durable runs,
deterministic evidence gates.

## What / why

Coding agents skip steps under pressure — marking a task "done" without running the tests, or
writing a review that never actually happened. Every purely *advisory* mechanism (a checklist
in a skill, a routine that says "run the tests before reporting", a reminder to be honest) is
just more text the same agent evaluates under the same pressure that made it skip the step in
the first place. Advisory tools don't stop step-skipping, because the thing being checked and
the thing doing the checking are the same actor.

lockstep is a flow-enforcement **engine**, not another checklist. A recipe is a compiled graph
(interrupt → deterministic validator → conditional edge); each step's "done" report is checked
by code the agent does not control — a pinned command actually runs, a file hash actually gets
compared, a regex actually gets matched against a real file — before the run is allowed to
advance. The agent works outside the graph; the engine owns run state and validates evidence
deterministically. That is what makes this a different guarantee from an advisory skill: the
enforcement lives in a process the agent cannot talk its way around, only satisfy or escalate
past.

## The honest guarantee

A lockstep run cannot report progress without deterministically validated,
artifact-backed evidence — run status is machine-checked, never
self-attested — PROVIDED the consumer denies the agent writes to the state
dir and the recipe sources. With baseline checks in the recipe this
extends to: artifacts were produced during the run (`fresh`), the test
tree the pinned command ran against is byte-identical to baseline
(`unchanged`), and a step cannot PASS while its diff touches undeclared
paths (`diff_only`). In policy-marked projects the PreToolUse gate adds:
no writes at all outside an active run, while hooks fire. What is NOT
guaranteed: work *quality* (shape checks are tripwires), and anything
hook-borne if hooks die silently — which `doctor` + heartbeat make
observable, not impossible. Guarantee strength is "integrity of the
status signal", not "process compliance".

## Install

Requires `uv` on the machine that runs the MCP server (the plugin launches it via `uvx`).

```
/plugin marketplace add sergesha/claude-essentials
/plugin install lockstep@claude-essentials
```

Or auto-enable per project in `.claude/settings.json`:

```json
{
  "enabledPlugins": { "lockstep@claude-essentials": true },
  "extraKnownMarketplaces": {
    "claude-essentials": { "source": { "source": "github", "repo": "sergesha/claude-essentials" }, "autoUpdate": true }
  }
}
```

## Quickstart

v1 has no builtin recipe resolution — recipes are not packaged with the plugin. Copy an
example into your project first:

```
mkdir -p .lockstep/recipes
curl -fsSL https://raw.githubusercontent.com/sergesha/claude-essentials/main/lockstep/recipes/examples/feature-dev.yaml \
  -o .lockstep/recipes/feature-dev.yaml
```

Then, in a Claude Code session in that project, ask the agent to start it (it calls the MCP
tool `scenario_start` with `recipe: "feature-dev"`). The `lockstep` skill (installed with the
plugin) drives the rest of the loop: `scenario_status` → do the current step → `scenario_done`
with evidence → repeat. See `skills/lockstep/SKILL.md` for the full loop and
`skills/lockstep-author/SKILL.md` for writing your own recipe.

## Configuration

Two environment variables, read by the MCP server process:

| Var | Default | Purpose |
|---|---|---|
| `LOCKSTEP_STATE_DIR` | `~/.lockstep` | Durable run state: SQLite checkpoints, `runs.json`, recipe snapshots, baseline manifests, `policy.d/`, `heartbeat.jsonl`. Deliberately outside the project — not in git, easy to deny writes to. |
| `LOCKSTEP_RECIPES` | `<cwd>/.lockstep/recipes` | Where `list_recipes`/`scenario_start` resolve recipe names from. |

`LOCKSTEP_STALE_HOURS` (default `24`) is read in two places: `lockstep-mcp hook-session-start`
uses it for a cosmetic staleness hint on the SessionStart context line for a run that hasn't been
touched in that long (never affects engine behavior); `lockstep-mcp doctor` uses the same default
to actually gate `heartbeat.jsonl` recency — a heartbeat older than the threshold is a `[FAIL]`
line and flips `doctor`'s process exit code to `1`.

The plugin's `mcpServers.lockstep.env` block passes both of the first two through as
`${LOCKSTEP_STATE_DIR}` / `${LOCKSTEP_RECIPES}` — set them in your own shell/settings before
starting Claude Code if you want a non-default location. **Caveat**: Claude Code's `${VAR}`
expansion, when the variable is unset, leaves the literal placeholder text in place (with a
warning surfaced in `claude mcp list`) rather than omitting the key — so an unset
`LOCKSTEP_STATE_DIR` does NOT fall through to the engine's own `~/.lockstep` default via this
path, it passes the literal string `${LOCKSTEP_STATE_DIR}` as a directory name. Export both
vars (even just to their defaults, e.g. `LOCKSTEP_STATE_DIR=~/.lockstep`) before launching
Claude Code, or drop the `env` block from a local copy of the plugin config if you want the
engine's own built-in defaults to apply untouched.

## Protecting the state dir and recipes

The honest guarantee above is conditional on this. Deny the agent write access to:

- `$LOCKSTEP_STATE_DIR` (all of it — the run index, checkpoints, baselines, and policy files
  live there; a write-capable agent could edit `runs.json` directly, forge a baseline, or drop
  a policy file to unlock its own gate)
- the recipe source directory (`$LOCKSTEP_RECIPES`, typically `<project>/.lockstep/recipes/`) —
  `scenario_start` snapshots the recipe at start and validates only that snapshot, so a live
  edit mid-run is inert, but an agent that can edit the *live* file before ever calling
  `scenario_start` can smuggle a weakened recipe into the very run it then starts
- the hooks/plugin config itself and `.claude/settings.json`, for the same reason

via your permission system (Claude Code's `permissions.deny` in project or user settings).
**Without an owner-managed permission system, this boundary is filesystem-only** — plain OS
file permissions on those paths, nothing lockstep-specific enforces it.

## Policy gate + doctor + CI liveness assert

**Policy gate** (opt-in per project): `lockstep-mcp policy require --project <path> --recipe
<name>` writes a policy file under `$LOCKSTEP_STATE_DIR/policy.d/`, owner-authored only (the
agent can't write there per the previous section). Once set, the PreToolUse hook denies
`Write|Edit|NotebookEdit|Bash|Task` in that project unless an **active run of exactly that
recipe** exists for that project — any other recipe, or no run, is denied. No Bash-command
parsing — it's a wholesale deny, so heredoc/quoting tricks don't matter, `Read`/`Grep` stay
open. `lockstep-mcp policy clear --project <path>` removes the gate. No policy file for a
project → always allowed (opt-in only); policy file present but state unreadable → deny
(internally fail-closed — the only hook that can, since it's the only one that actually blocks
an action).

**`lockstep-mcp doctor [--hooks-json <path>]`**: v1-trimmed diagnostic — state/recipes dirs
exist, `hooks.json`'s version pin matches the installed `lockstep-mcp` version, `heartbeat.jsonl`
recency against `LOCKSTEP_STALE_HOURS` (default 24h — an older heartbeat is `[FAIL]`), and a
`uvx lockstep-mcp==<version> --version` pre-warm one-liner to print. Exits `1` if any check
failed, `0` if all green — a CI/operator script can gate on the process exit code, not just
scrape the report text. Not implemented in v1 (planned v2): effective-settings inspection,
handler self-exec.

**CI liveness assert**: "the rule is in the file" is not "the rule works" — every hook handler
appends one best-effort JSONL line to `heartbeat.jsonl` unconditionally, before any early-exit
for "nothing configured". A consumer's CI can prove the hook wiring itself actually fires
(independent of whether any lockstep run is active) by running `claude -p "ok"` in a repo with
lockstep's hooks configured and asserting `heartbeat.jsonl` grew by at least one line.

## Known assumptions

**`run.project` = the MCP server process's cwd at `scenario_start`** (captured once via
`Path.cwd().resolve()`, never a tool argument) — hooks match a run to the current project by
resolved-equality-or-parent-prefix against this value. **VERIFIED live** (2026-08-07): a
scratch project directory with a `.mcp.json` pointing `uv run --project
<clone>/lockstep/engine lockstep-mcp serve` at a throwaway `LOCKSTEP_STATE_DIR`, driven by
`claude -p --mcp-config .mcp.json --strict-mcp-config --allowedTools
"mcp__lockstep__scenario_start" --model haiku` with a prompt making exactly one
`scenario_start(recipe="two-steps")` call. The resulting `runs.json` record's `project` field
was read back directly and asserted byte-equal to the scratch directory path — confirmed: the
MCP server does launch with the project directory as its cwd when Claude Code spawns it per
its `.mcp.json` entry, and `Path.cwd()` at server-start time is exactly that project directory.

## Honesty notes

- **The Stop hook is a nudge, not a wall.** It blocks once per stop chain — our handler checks
  `stop_hook_active` and allows the very next stop attempt through unconditionally, it never
  re-blocks within the same chain (the platform's own ~8-consecutive-block cap on Stop hooks in
  general is not something this handler ever exercises). It cannot prevent a sufficiently
  determined stop at all — it can only delay and annotate once, never truly enforce. The
  engine's evidence gate is what's actually load-bearing: a `scenario_done` report without
  valid, machine-checked evidence is simply rejected regardless of whether the Stop hook fired
  at all. Don't rely on the hook to make an agent honest; rely on it only to reduce the chance
  an active run goes unreported by accident.
- **Hooks are inert on interim git-URL installs.** Until `lockstep-mcp` is published to PyPI,
  `hooks.json`'s pinned `uvx lockstep-mcp==0.1.0 hook-*` commands resolve to nothing installable
  — `uvx` can't fetch a PyPI package that doesn't exist yet. Use the interim invocation for a
  manual/local MCP server entry in the meantime: `uvx --from
  git+https://github.com/sergesha/claude-essentials#subdirectory=lockstep/engine lockstep-mcp`
  (or, for local development, `uv run --project <clone>/lockstep/engine lockstep-mcp <verb>`, as
  used in the live verification above).
- **Write-capable MCP tools from other servers are outside the PreToolUse matcher.** The
  policy gate matches on tool names `Write|Edit|NotebookEdit|Bash|Task` only; a different MCP
  server's own write-capable tool (a filesystem-write tool from an unrelated plugin, say) isn't
  named in that matcher and isn't gated by lockstep's policy at all.
- **`scenario_dryrun` can probe tripwire regexes for free.** Shape checks (`file_matches`,
  `md_has_sections`, ...) are tripwires by design — they prove an artifact *says* the right
  thing, not that the right thing is true. `scenario_dryrun` runs those same checks against
  arbitrary evidence with no run, no state, and no attempt-budget cost, which makes iteratively
  reverse-engineering exactly what a regex/heading needs to say cheaper than it would be inside
  a real run. This isn't a new hole dryrun opens — shape checks are gameable either way — it
  just makes probing free.
- **v1 concurrency limits — single-user scope.** Route-log writes for concurrently active runs
  can interleave (no locking). `runs.json` writes are atomic per-write (tmp file + `os.replace`)
  but last-write-wins across concurrent updates — two writers racing can lose one update. Fine
  for one agent driving one run at a time; not safe for multiple agents sharing one state dir
  concurrently.
- **Subagent hook bypass is irrelevant.** A subagent that never triggers the parent's
  PreToolUse/Stop hooks still cannot fake progress — enforcement lives in the engine's evidence
  validation (`scenario_done`), which every caller goes through regardless of hook inheritance.
- **Hooks are kept deliberately trivial and tested, because silent hook death is real.** A
  crashing hook that exits non-0/non-2 is fail-OPEN on Stop/SessionStart per the platform's own
  hook contract (PreToolUse is the one hook internally fail-closed against its own exceptions,
  because it's the only one that can actually block an action). `doctor` + `heartbeat.jsonl`
  exist specifically to make hook death observable rather than silent — they don't make it
  impossible.
