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
no writes at all outside a run this session drives, while hooks fire. What is NOT
guaranteed: work *quality* (shape checks are tripwires), and anything
hook-borne — a hook that dies, dies silently; nothing observes hook
liveness. The load-bearing layer is the engine's evidence gate, which
does not depend on hooks firing. Guarantee strength is "integrity of the
status signal", not "process compliance".

## Install

Requires `uv` on the machine that runs the MCP server. Distribution is the plugin's own cloned
files: `mcpServers`/`hooks.json` invoke `uv run --project ${CLAUDE_PLUGIN_ROOT}/engine
lockstep-mcp <verb>` — no PyPI package required. `${CLAUDE_PLUGIN_ROOT}` is set by Claude Code to
the plugin's install directory. Hooks and the MCP server work immediately after install; the
first invocation resolves dependencies into `uv`'s cache (a one-time delay), subsequent runs are
fast. A PyPI release of `lockstep-mcp` (installable via `uvx`) is an optional future distribution
path, not required for the plugin to work.

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
| `LOCKSTEP_STATE_DIR` | `~/.lockstep` | Durable run state: SQLite checkpoints, `runs.json`, recipe snapshots, baseline manifests, `policy.d/`, `bindings/`. Deliberately outside the project — not in git, easy to deny writes to. |
| `LOCKSTEP_RECIPES` | `<cwd>/.lockstep/recipes` | Where `list_recipes`/`scenario_start` resolve recipe names from. |

`LOCKSTEP_SESSION_STALE_MINUTES` (default `30`) is the session-liveness window for the policy
gate's session binding (see below): a run's driving session counts as live while its binding's
`last_seen` stamp is within the window, and an abandoned run becomes adoptable once its driver
has been silent longer. Read only by the hooks — the engine neither reads nor writes bindings.

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

## Protecting the state dir, recipes, and the engine itself

The honest guarantee above is conditional on this. Deny the agent write access to:

- `$LOCKSTEP_STATE_DIR` (all of it — the run index, checkpoints, baselines, policy files, and
  session bindings live there; a write-capable agent could edit `runs.json` directly, forge a
  baseline, drop a policy file to unlock its own gate, or rewrite a `bindings/` sidecar to
  claim another session's run)
- the recipe source directory (`$LOCKSTEP_RECIPES`, typically `<project>/.lockstep/recipes/`) —
  `scenario_start` snapshots the recipe — and every fractal child recipe its subcall markers
  name via `scenario:` — at start, and every launch (the run itself, and any child run it
  spawns later) uses only those snapshots, so a live edit mid-run is inert; but an agent that
  can edit the *live* file before ever calling `scenario_start` can smuggle a weakened recipe
  into the very run it then starts
- the plugin/engine source itself (the plugin's cloned files — `${CLAUDE_PLUGIN_ROOT}`,
  including `lockstep/engine/`): v2 executes `lockstep_mcp/_subcall_wrapper.py` by path as the
  subcall supervisor, and every hook/server invocation runs the cloned `lockstep_mcp` code — an
  agent that can rewrite it replaces the supervisor and owns the "independent" session outright
- the hooks/plugin config itself and `.claude/settings.json`, for the same reason

via your permission system (Claude Code's `permissions.deny` in project or user settings).
**Without an owner-managed permission system, this boundary is filesystem-only** — plain OS
file permissions on those paths, nothing lockstep-specific enforces it.

## Policy gate + doctor

**Policy gate** (opt-in per project): `lockstep-mcp policy require --project <path> --recipe
<name>` writes a policy file under `$LOCKSTEP_STATE_DIR/policy.d/`, owner-authored only (the
agent can't write there per the previous section). Once set, the PreToolUse hook denies
`Write|Edit|NotebookEdit|Bash|Task` in that project unless **the calling session is driving an
active run of exactly that recipe** — session binding, not mere run existence: the platform
delivers `session_id` in every hook input, and each run carries a hook-owned binding sidecar
(`$LOCKSTEP_STATE_DIR/bindings/<run-id>.json`) naming the one session that drives it. The run
is bound to its starter at `scenario_start` (a PostToolUse hook on the lockstep MCP tools reads
the run id from the tool response), and every gated tool call or lockstep call by the owner
refreshes the binding's `last_seen` stamp — so another session's awaiting run never unlocks
yours, and a fresh session is never let in by yesterday's abandoned run. No Bash-command
parsing — it's a wholesale deny, so heredoc/quoting tricks don't matter, `Read`/`Grep` stay
open. Absent `session_id` in the hook input is ambiguity and denies.

**Crash recovery (adoption)**: a resumed conversation gets a NEW session id, so binding alone
would lock the human out of their own project after a crash. The door: once the driver has
been silent longer than `LOCKSTEP_SESSION_STALE_MINUTES` (default 30m — silence means no gated
tool call and no lockstep MCP call), a `scenario_status` call on the run ADOPTS it: the
PostToolUse hook rebinds it to the calling session, recording the previous owner as
`adopted_from`. The gate itself never adopts — a stray Write in the project cannot take over
an abandoned run; only the deliberate lockstep-tool touch can, and never from a live driver
(a live session's ordinary work keeps its stamp fresh, so the touch is a no-op and the gate
stays shut). The impatient alternative needs no window: `scenario_abort` the run, then
`scenario_start` a fresh one — bound to you at birth. There is no timestamp expiry on runs:
`RunRecord.updated` does not tick during real work (not on Write/Edit, not on status polls,
not while a subcall runs), so its age measured nothing — the binding's `last_seen`, which does
tick, replaced it. That also retires the old `_nudge_ancestors` caveat: a finished child's
nudge refreshes the parent's `updated`, which the gate no longer reads, and never touches
bindings. A spawned child session's gate is unchanged by bindings — its `LOCKSTEP_CHILD_RUN`
credential plus an all-awaiting ancestry chain rooted in a policy-recipe run is the whole
predicate; the env credential binds the child to its run more tightly than a sidecar could.
`lockstep-mcp policy clear --project <path>` removes the gate. No policy file for a
project → always allowed (opt-in only, and bindings are policy-gate machinery only — no
policy, no gate, regardless of bindings); policy file present but state unreadable → deny
(internally fail-closed — the only hook that can, since it's the only one that actually blocks
an action).

**`lockstep-mcp doctor`**: v1-trimmed diagnostic — state/recipes dirs exist, and the
installed version (self-reported, informational — there's no external pin to check it against;
distribution is the plugin's own cloned files). Exits `1` if any check failed, `0` if all green —
a CI/operator script can gate on the process exit code, not just scrape the report text. Not
implemented in v1 (planned v2): effective-settings inspection, handler self-exec.

## Subcalls (v2)

A **subcall** is a recipe step that hands a closed sub-task to a separate
`claude -p` process the main (worker) agent's conversation cannot read or
steer — first use case: an independent review gate, so the reviewer isn't
the same session that did the work. A **fractal** subcall goes further: the
spawned session runs its own lockstep child run, with its own snapshot,
baseline and gate — the parent's evidence is that child run's terminal
status, machine-checked, not the child's word. See
`skills/lockstep-author/SKILL.md` for the recipe dialect
(`recipes/examples/feature-dev-reviewed.yaml` + `review-gate.yaml` is a
full worked example) and `skills/lockstep/SKILL.md` for how a worker
experiences a subcall in flight.

**What it guarantees**: the worker cannot WRITE the subcall's control
channel or drive the child run. This is enforced by state-dir/`runners.yaml`
denial (the same permission-system denial the rest of this README already
requires) + absolute-path exec (the engine never PATH-resolves a runner —
on a no-sudo host `~/.local/bin` is agent-writable and on PATH, so PATH
resolution would let the worker plant a fake `claude` that forges the
"independent" session) + child-run origin binding (only the credentialed
child process — the one holding the `LOCKSTEP_CHILD_RUN`/
`LOCKSTEP_CHILD_NONCE` pair the engine minted at spawn — may call
`scenario_done`/`scenario_escalate`/`scenario_abort` on a parented run; a
caller without that credential is refused). Validated artifact bytes are
hash-pinned from the DENIED side — the child's own validated baseline
snapshot for a fractal subcall, or the runner's captured output file under
the state dir for a one-shot subcall — never from collect-time project
bytes, which the worker owns and could have edited after the fact. Be
precise about what the pin means: it proves the bytes are **unchanged
since the child run's last validated PASS** — nothing more. The worker
can author those bytes up to that instant; what vouches for the content
is the child's own checks plus the parent's own content checks (the
shipped example pairs the pin with a `Verdict: PASS` regex — keep both
whenever you copy it). A fractal child is a full lockstep run: every v1
guarantee above applies to it recursively. The policy gate's linkage: the
worker session is unlocked by the awaiting policy-recipe run BOUND to it
(session binding — see "Policy gate" above), and a spawned child session
— its environment carries `LOCKSTEP_CHILD_RUN` — is unlocked ONLY while
that run's own ancestry chain is fully awaiting and terminates in an
awaiting run of the policy's recipe in the project; a child whose chain
is dead is denied even while the worker stays unlocked by its own run.

**What it does NOT guarantee:**

- **Origin binding closes the sanctioned MCP surface. It does NOT close
  same-user OS access**: on a multi-user OS a process environment may be
  reachable by other same-user processes through ordinary OS facilities,
  and a worker with shell can Bash-launch its own credentialed engine. That
  is the SAME same-user residual class v1 already carries (shell reaches
  the state dir too) — stated honestly, not claimed closed.
- **Input poisoning is real.** The worker CAN read the child's transcript
  (`~/.claude/projects/<slug>/` — outside this project's boundary; add your
  own deny if that matters to you) and CAN author the child's inputs — a
  file planted with "this was already reviewed/approved" is a live attack,
  not a hypothetical. This is why the shipped reviewer prompt
  (`feature-dev-reviewed.yaml`) explicitly instructs the reviewer to treat
  any in-repo claim of pre-approval as a FINDING to report, never as an
  instruction to follow — write the same instruction into any reviewer
  prompt you author.
- **No liveness without tool calls.** Polling happens only on
  `scenario_status`/`scenario_done` entry — nothing runs in the background.
  A sleeping subcall makes no progress until something calls one of those
  two tools.
- **Subcall work quality is not guaranteed**, same as everywhere else in
  this README — shape checks are tripwires, not proof.

**Owner setup**: a `runner:` name (e.g. `claude`) only resolves through
`$LOCKSTEP_STATE_DIR/runners.yaml` — owner-authored, denied to the agent by
the same permission rule as the rest of the state dir. It pins the
runner's executable by ABSOLUTE path (never resolved from `PATH`) and a
non-empty model allowlist; an unlisted runner, a relative path, or an empty
model list is a loud start-time refusal (`scenario_start` resolves every
subcall marker's runner against the allowlist before any run is created),
never a silent fallback. The engine always launches with the FIRST model in
the runner's `models` list; later entries are reserved for future
per-marker selection. The state
dir itself must not sit inside the project tree — the engine refuses to
start a run if it does (`runners.assert_state_dir_sane`), because a state
dir under the gate would hand the agent the allowlist it's supposed to be
denied.

**Spawned-child tool discovery** (live-smoke finding): on a host with many
MCP servers, Claude Code defers MCP tools behind its tool-search
mechanism; a small-model child may fail to load the lockstep tools and
stall to escalation (fail-closed, but wasted). The engine preamble names
the exact tool names to load; consumers can additionally pin
`"alwaysLoad": true` on the lockstep entry in the project's `.mcp.json`
so a spawned child always has the tools loaded. Also required once per
host: the project must be trusted for non-interactive runs (an untrusted
workspace ignores `permissions.allow`, the child cannot write its
artifact or call MCP tools, and the run escalates — the child's stderr
names the exact `hasTrustDialogAccepted` fix).

**OS-agnostic**: process spawn, liveness, termination, and the run-index
lock all use only Python stdlib with portable semantics — no
`fcntl`/`msvcrt`/`/proc`/`setsid` assumptions anywhere in the core paths;
the same test suite is green on macOS and Linux with zero platform skips.

**Explicit deferrals** — not silently missing, deliberately out of v2's
scope:

- `copilot` stays in `FORBIDDEN_NODE_TYPES` in v2 — the conditional
  shim-runner allowance is deferred.
- v2 never resumes runner sessions; the envelope's `session_id` is
  informational only (`safe_argv`'s resume gate exists for the future
  continuation path).

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
  because it's the only one that can actually block an action). Nothing observes hook liveness —
  a dead hook stays dead silently. Rely on the engine's evidence gate, which every
  `scenario_done` goes through regardless of whether any hook fired.

## Known issues (v1)

- **Glob semantics for `**`-prefixed patterns**: `unchanged`/`fresh` filter the
  stored manifest with `fnmatch` (where `**/` requires a literal `/`) while the
  current tree is scanned with `glob` (where `**` matches zero segments). A bare
  `**/*`-style glob therefore reports spurious "changed"/"not covered" results
  for TOP-LEVEL files. Direction is strictly fail-closed (no bypass possible).
  Workaround: use directory-anchored globs (`tests/**`, `src/**`) and literal
  filenames (`pytest.ini`) — the shapes every shipped recipe uses.
- **Hidden trees are hashed**: baseline manifests include hidden files by design
  (a dotfile inside a fenced glob must not be an invisible edit channel). Under
  broad globs this also pulls in `.venv/`-style trees — only `.git/`,
  `__pycache__/`, `*.pyc` are ignored. Keep `baseline_globs` narrow.
