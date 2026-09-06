# Derived diagnostics

`status` is a live, read-only, owner-separated view. The actual user reply
contains this filled table: four owner rows and all seven named columns.
This is the minimum report even for a brief answer; brevity shortens cell
text while retaining the rows and columns.

| Owner | Selection reason | Result | Native root | Revision/version | Interface | Read time |
| --- | --- | --- | --- | --- | --- | --- |
| Backlog.md | Not selected | N/A | not applicable | not applicable | not applicable | not applicable |
| OpenSpec | Not selected | N/A | not applicable | not applicable | not applicable | not applicable |
| Beads/Dolt | Explicit Beads status request | broken | not observed | not observed | Required CLI absent | not observed |
| Superpowers | Applicable verification trigger | valid | not observed | not observed | Required skill closure readable | not observed |

This example has only a Beads request, an absent Beads CLI, and a readable
applicable Superpowers closure; roots, versions, and read timestamps were not
observed. Adapt the cells to the actual evidence. For selected Superpowers,
the root is its observed installed closure path. Read time is an observed
timestamp; when unavailable, its cell is `not observed`.

Fill every cell from current observations and the selection rules in
[ownership.md](ownership.md). The Superpowers selection reason evaluates the
native trigger for the current activity, including read-only verification,
independently of assigned project tasks. Query selected owners live; report
unselected owners as `N/A` with unqueried evidence marked `not applicable`.
Mark unavailable evidence `not observed`. Missing report metadata does not
change an evidenced owner result: an absent required CLI is still `broken`.
There is no aggregate status. Diagnostics may not repair, create, synchronize,
archive, or close native artifacts.

## Per-owner result

Use exactly one result for every owner slot. `N/A` applies only to an
unselected owner; every other result applies to a selected owner:

| Result | Meaning |
| --- | --- |
| `N/A` | An unselected owner; do not create or repair it. |
| `broken` | A selected owner has a missing required tool or interface. State what is missing and apply the root skill's conditional missing-tool rule. |
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
  `bd dolt status` reports the Dolt service state. Never synthesize subcommands
  or options from a related CLI, a different version, or memory. If the
  installed interface provides no required equivalent, report `broken` instead
  of guessing a command.
- Superpowers: `valid` means the selected installed skill closure resolves and
  required applicable skills are readable. This result reports guidance
  availability, not project-task state; Superpowers owns no project status.

A missing interface is `broken`, conflicting root or facts are `ambiguous`,
and lost or unverifiable facts are `unknown`.

## Next

Derive `next` only from current selected-owner facts and current approved
scope. Every proposal is advisory, never authorization or stored workflow
state.

- With one justified candidate, present one action with its owner, reason,
  and expected effect. Apply the ownership approval boundary: ask only when
  existing explicit authorization does not cover that concrete action. A
  diagnostic `next` report itself remains read-only and grants no authority.
- With two or more equally valid candidates, return `ambiguous`, present the
  choices, and ask the user to choose. Do not rank them or mutate.
- With zero candidates, return the literal `no proposed action`, give the
  reason, and do not invent work, create an owner, or record idle or complete
  state.

Never persist recommendations, phases, cursors, queues, or derived results.


## Views and export

Use the installed owner's built-in views first. Inspect current help for
Backlog board/browser/overview, OpenSpec view/status/show, and Beads graph/status;
their availability and flags depend on version. A request for built-in views
only permits native output, not a custom visualization or third-party viewer.

Explain counts in their native scope: Backlog boards count tasks, not briefs;
OpenSpec canonical specs and pending change specs are different collections;
artifact completion can mean file existence rather than approved content;
Beads graphs contain only issues actually created there. Inspect those native
contents before interpreting a zero or 100 percent. Report a missing framing
task or gate-only checklist as a content gap even if CLI health is valid.
Do not fill an empty view with invented tasks during diagnostics.

On request, render an ephemeral table, tree, or Mermaid view only from the
native owner outputs or documented CLI capabilities read for that request. Do
not build a mirrored dashboard, cache the view, or make it a source of truth.

Export only after the user names one explicit destination and the exact files
to copy, sees any overwrite effects, and approves. Write a Markdown snapshot
with a non-authoritative, derived header and a warning that paths and metadata
are point-in-time observations. An export copies snapshots only: never import,
cache, synchronize, use it as SpeciFlow state, or let it change any owner.
