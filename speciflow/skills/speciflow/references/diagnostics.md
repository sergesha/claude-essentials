# Derived diagnostics

`status` is a live, read-only, owner-separated view with exactly one slot for
each of Backlog.md, OpenSpec, Beads/Dolt, and Superpowers. Query selected
owners live; an unselected owner explicitly shows `N/A`. Each slot shows its
owner name, native root, native revision or version, tool interface, and read
time. Do not combine these results into a summary status; there is no aggregate phase.
Diagnostics may not repair, create, synchronize, archive, or close native
artifacts.

## Per-owner result

Use exactly one result for every owner slot. `N/A` applies only to an
unselected owner; every other result applies to a selected owner:

| Result | Meaning |
| --- | --- |
| `N/A` | An unselected owner; do not create or repair it. |
| `broken` | A selected owner has a missing required tool or interface. State what is missing; offer installation only on request. |
| `ambiguous` | Multiple roots or conflicting facts prevent one native answer. Preserve the alternatives and ask the user. |
| `unknown` | Output was lost or a required fact cannot be verified. Stop rather than infer or retry a mutation. |
| `valid` | The applicable documented checks below passed through the installed native interface. |

Report a dirty native root and its dirty paths as its owner's live fact. Dirty
is distinct from a dependency being unavailable and from `ambiguous`; do not
erase it by relabeling it as another owner result. Every read is current only
at its recorded read time.

For `valid`, use only the installed version's documented native interfaces;
do not create an adapter, schema, or state. The concise, reproducible checks
are:

- Backlog.md: one unique selected native root; documented read/status/list
  succeeds; run native validation when the installed version provides it.
- OpenSpec: one unique native root; documented status/list and validation for
  the selected changes or specs succeed.
- Beads/Dolt: inspect the installed `bd --help` and relevant subcommand help,
  then use only commands that interface documents. Confirm `bd where` matches
  the selected Beads root and query its documented read/status interface. When
  supported, `bd vc status` supplies the native branch, commit, and dirty state;
  `bd dolt status` reports the Dolt service state. `bd dolt` is not a raw Dolt
  CLI passthrough: never invent subcommands or forward raw Dolt flags such as
  `bd dolt log -n`. If the installed interface provides no required equivalent,
  report `broken` instead of guessing a command.
- Superpowers: the selected installed skill closure resolves and required
  applicable skills are readable; Superpowers owns no project status.

A missing interface is `broken`, conflicting root or facts are `ambiguous`,
and lost or unverifiable facts are `unknown`.

## Next

Derive `next` only from current selected-owner facts and current approved
scope. Every proposal is advisory, never authorization or stored workflow
state.

- With one justified candidate, present one action with its owner, reason,
  expected effect, and an approval question; do not mutate before approval.
- With two or more equally valid candidates, return `ambiguous`, present the
  choices, and ask the user to choose. Do not rank them or mutate.
- With zero candidates, return the literal `no proposed action`, give the
  reason, and do not invent work, create an owner, or record idle or complete
  state.

Never persist recommendations, phases, cursors, queues, or derived results.

## Views and export

On request, render an ephemeral table, tree, or Mermaid view only from the
native owner outputs or documented CLI capabilities read for that request. Do
not build a mirrored dashboard, cache the view, or make it a source of truth.

Export only after the user names one explicit destination and the exact files
to copy, sees any overwrite effects, and approves. Write a Markdown snapshot
with a non-authoritative, derived header and a warning that paths and metadata
are point-in-time observations. An export copies snapshots only: never import,
cache, synchronize, use it as SpeciFlow state, or let it change any owner.
