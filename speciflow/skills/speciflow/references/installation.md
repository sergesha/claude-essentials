# User-level installation

SpeciFlow is instructions and references, not an installer. It ships no
`speciflow` CLI, runtime, daemon, installer script, or universal host command.
Installation is performed only when the user requests it.

Before any host-side write, show one exact preview containing the source,
version or channel, command or UI action, target, and external effects. Wait for
explicit approval before running the requested host installation action. Start
a new session when the host requires a reload, then invoke SpeciFlow explicitly.

## Codex

Use Codex's built-in `$skill-installer` with the canonical GitHub skill URL.
This is a Codex prompt, not a shell command:

```text
$skill-installer install https://github.com/sergesha/claude-essentials/tree/main/speciflow/skills/speciflow
```

Preview source channel `sergesha/claude-essentials` branch `main`, target
`$CODEX_HOME/skills/speciflow` (normally `~/.codex/skills/speciflow`), and the
network read plus user-level skill files the installer will create. Obtain
explicit approval before installation. On the next turn, verify SpeciFlow in
`/skills`; if the current thread does not refresh it, use `/new` without exiting
Codex. Invoke `$speciflow` explicitly.

## Claude

Use Claude's plugin marketplace. Preview source `sergesha/claude-essentials`
and its `.claude-plugin/marketplace.json`, package
`speciflow@claude-essentials`, the version in the package manifest, and these
user-level Claude actions:

```text
/plugin marketplace add sergesha/claude-essentials
/plugin install speciflow@claude-essentials
```

The targets are the user's Claude marketplace registration and installed
SpeciFlow bundle. External effects are limited to those host-managed entries
and files. Obtain separate explicit approval before each requested action,
start a new Claude session, and invoke SpeciFlow explicitly.

## Owner dependencies

Do not bundle owner dependencies into either installation path. Diagnose a
missing dependency only when its owner is selected for the current requested
operation. Offer one owner-specific installation action with its exact source,
version or channel, command, target, external effects, and approval gate. A
missing unselected owner tool is `N/A`.

For an npm-based installation preview, Backlog.md is package `backlog.md` with
the `backlog` CLI, and OpenSpec is package `@fission-ai/openspec` with the
`openspec` CLI. The unscoped npm package `openspec` is not the OpenSpec owner
CLI. Inspect current package metadata and the installed interface before
proposing the exact channel or version.

## Original grilling-family dependency

Install this dependency when the user directly requests its installation or
refresh, or when a selected grilling, domain-modeling, research, prototype, or
human-invoked Wayfinder activity requires it. It remains original external
source: do not copy its algorithms into SpeciFlow, add it to a plugin manifest,
or install the same bundle through two host mechanisms.

### Codex project closure

Use the official `skills` CLI from the selected project root with an explicit
Codex target. Inspect the current `skills` CLI version and interface before use.
Show the resolved CLI version in the preview; that qualification applies to this
one operation and is not a permanent SpeciFlow pin.

```sh
DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 NODE_DISABLE_COMPILE_CACHE=1 npx --yes skills add mattpocock/skills --agent codex --skill grill-me grilling grill-with-docs domain-modeling wayfinder research prototype setup-matt-pocock-skills --copy --yes
```

Preview all eight targets as
`<project>/.agents/skills/{grill-me,grilling,grill-with-docs,domain-modeling,wayfinder,research,prototype,setup-matt-pocock-skills}`.
Before install or refresh, resolve the then-current requested original closure
and adapt the selected names and targets if upstream added dependencies.
Include these effects in the preview:

- npm package/dependency resolution and cache writes, plus GitHub clone/API
  checks in a temporary directory;
- recursive replacement of any existing same-named target directories, after
  inspecting conflicts such as a pre-existing unrelated `research` skill;
- copy of the selected closure into `<project>/.agents/skills/` and creation or
  update of the installer's native `<project>/skills-lock.json`.

The telemetry opt-outs shown above suppress the CLI's ancillary telemetry and
risk-audit requests. The native lock is installer bookkeeping, not SpeciFlow
state. Do not promise that the operation performs no Git activity: source
acquisition may use Git, although it does not commit or change branches in the
target project. Installation does not run any installed skill and does not
authorize changes to `AGENTS.md`, `CLAUDE.md`, `docs/agents/`, context or ADR
files, trackers, labels, branches, commits, or product source.
Project `.agents/skills/` is a shared host-discovery convention, so Codex
targeting does not guarantee that another host reading that directory cannot
discover the copied files.

Do not use native `skills update --project` as a Codex-constrained refresh:
the inspected updater has no `--agent` option and may autodetect and write
other agent directories. After qualifying a changed original closure, repeat
the same explicit `add ... --agent codex --copy --yes` operation and preview
its replacement effects.

Use the route above for this preview. If the user explicitly selects another
documented installer, inspect that installer and preview only its own effects;
for example, an auto-download implementation may download a complete
repository archive to a temporary directory, fall back to Git, and abort on an
existing destination instead of replacing it. Never merge effects from two
installers.

### Claude managed bundle

Use the original `mattpocock-skills` package from Claude's official
`claude-plugins-official` marketplace through one host-managed action:

```text
/plugin install mattpocock-skills
```

Preview the full promoted bundle rather than only the eight applicable skills:
network access, Claude's managed plugin registration and files, broader skill
discovery, and the marketplace update channel. Marketplace pins can lag the
upstream `main` branch, so inspect and report the actual installed revision
after installation or update. Do not also place skills.sh copies of this
bundle into Claude.

### Setup and update boundary

Read the existing project context before offering setup. Installation alone
never invokes `setup-matt-pocock-skills` and never permits its project effects.
If the selected activity needs configuration, preview only the exact required
tracker/domain effects and apply the normal authorization boundary; grilling
does not require configuring the full upstream workflow.

Do not replace the loaded closure during a bounded interview or supporting
activity. Between activities, update through the selected host's documented
mechanism, then read the installed entrypoints, linked files, invocation
policy, and effects and record the source and actual revision in review
evidence. If the new closure is incompatible, name the affected capability,
stop only that dependent activity, and continue independent owner work. A
rollback is a separate documented host action under its applicable
authorization. Do not create a SpeciFlow lock or vendored fallback.
