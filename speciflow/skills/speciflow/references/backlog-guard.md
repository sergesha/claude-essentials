# Planning direct-write guard

The full SpeciFlow plugin includes one synchronous `PreToolUse` command hook
for Claude Code and Codex. It redirects recognized direct mutations of
initialized Backlog.md product data and active OpenSpec specifications to
SpeciFlow and the native owner's interface.

## Decision

A denial requires a recognized mutation and its associated protected path.
Mentioning a protected path, reading it or using an unfamiliar program is not
a reason to deny. The hook maintains no permitted-read-command, Python-import
or text-method list.

- Structured editor paths and patch add/update/delete/move targets are checked.
- Shell checks associate deletion, movement, destination copies, in-place edits
  and output redirections with their own targets.
- Python checks recognize direct filesystem mutations with literal paths and
  ordinary local path assignments. Text processing and unknown readers need
  no exemption. String replacement is distinct from path replacement.
- Native `backlog` and `openspec` calls pass without a subcommand allowlist.
  Separate shell mutations and external output redirections remain checked.

Allowed calls return exit 0 with no hook output. Recognized direct mutations
return exit 2 and an owner-specific reminder on stderr, blocking the whole tool
call before it runs. The hook itself never changes owner files or invokes the
skill. Unknown code receives no decision; silence is not proof of safety.

## Protected owners and editable changes

Backlog protection requires native initialization markers, rather than an
arbitrary file named `backlog.md`. Owner data includes tasks, drafts, documents,
decisions, milestones and configuration. Native configured locations, ordinary
relative/absolute paths, cwd/workdir and symlinks are recognized. Beads is
outside this hook.

OpenSpec protection covers the active `specs/` tree of an identified native
root, including canonical `openspec/specs` and externally stored roots with
native markers. An unrelated folder named `specs` is not sufficient.

Proposal, design, tasks and delta specs under `openspec/changes/**` remain
editable. Reading an active specification and writing the result into a change
in the same program is allowed. Copying an active spec into a change is allowed;
copying over an active spec is a recognized mutation. Native OpenSpec archive
applies valid changes silently.

## After a denial

For Backlog, use SpeciFlow and the native Backlog CLI to amend product intent.
For active OpenSpec contracts, use SpeciFlow to prepare or update the appropriate
change artifacts, then apply them through the native OpenSpec CLI. Reuse the
user's applicable authorization and follow the skill's owner and amendment
rules. The reminder grants no additional authority.

## Evidence and boundary

A blocked recognized mutation preserves the original requirements for recovery
when an agent or person returns to the process after drift. Actual reminder
delivery and subsequent change work are tested separately from byte preservation.

The hook does not establish all effects of arbitrary executable code, scripts,
imports, dynamic paths, background processes or interactive stdin. Codex
`write_stdin` does not run a fresh `PreToolUse`; specialized tools may omit the
common hook path. A permitted native CLI call does not prove the complete
SpeciFlow procedure ran. No runtime interception, rollback service, writer
helper, workflow ledger or host filesystem policy is provided.

Disabled, untrusted, missing or failing hooks are not successful protection.
Inspection failures produce a visible error; host failure handling can allow
the original call. Source tests and the verification report record the exact
tested mutation forms and observed host results.

## Activation

Install the full plugin through the host's native plugin mechanism; see
[installation.md](installation.md). Installing only the skill does not register
the hook. Python 3.11 or later must be available as `python3`.

In Codex, review and trust the current hook definition through `/hooks`.
Installation and enabling alone do not trust it. After an upgrade, verify
loading in each host and exercise disposable denials plus silent read, native
CLI and change-authoring controls.

Host interfaces: [Claude hooks](https://code.claude.com/docs/en/hooks) and
[Codex hooks](https://learn.chatgpt.com/docs/hooks).
