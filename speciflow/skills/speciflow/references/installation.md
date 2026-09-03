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
