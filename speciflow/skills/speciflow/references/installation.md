# User-level installation

SpeciFlow is instructions and references, not an installer. It ships no
`speciflow` CLI, runtime, daemon, installer script, or universal host command.
Installation is performed only when the user requests it.

Before any host-side write, show one exact preview containing the source,
version or channel, command or UI action, target, and external effects. Wait for
explicit approval before running either marketplace-registration or
plugin-installation action. Start a new session when the host requires a
reload, then invoke SpeciFlow explicitly.

## Codex

Use the repository's Codex marketplace. Preview source
`<absolute-repository-path>/.agents/plugins/marketplace.json`, package
`speciflow@claude-essentials` version `0.1.0`, and these user-level actions:

```text
codex plugin marketplace add <absolute-repository-path> --json
codex plugin add speciflow@claude-essentials --json
```

The targets are the user's Codex marketplace registration and installed
SpeciFlow bundle. External effects are limited to those host-managed entries
and files. Obtain separate explicit approval before each requested action,
start a new Codex session, and invoke `$speciflow` explicitly.

## Claude

Use the repository's Claude marketplace. Preview source
`sergesha/claude-essentials` and its `.claude-plugin/marketplace.json`, package
`speciflow@claude-essentials` version `0.1.0`, and these user-level Claude
marketplace actions:

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
