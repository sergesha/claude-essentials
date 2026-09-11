# Derived diagnostics

`status` is a live, read-only, owner-separated view. Report one entry per
owner (Backlog.md, OpenSpec, Beads/Dolt, Superpowers, Grill) with these
required fields:

```json
{
  “owner”: “Backlog.md | OpenSpec | Beads/Dolt | Superpowers | Grill”,
  “selection”: “why this owner is or is not queried”,
  “result”: “valid | broken | ambiguous | unknown | N/A”,
  “root”: “observed native root path, or not observed”,
  “evidence”: “interface status, revision, and read timestamp”
}
```

**Selection rules:** Select owners by the concern in the request, not by
default. A scope phrase like “only Beads” queries Beads and leaves others
as `N/A`. Evaluate Superpowers and Grill triggers independently from the
current activity context.

**Evidence rules:**
- Query selected owners live through their native interface.
- Unselected owners: `N/A` with evidence `not applicable`.
- Unavailable metadata: `not observed` — never substitute ambient host data.
- A missing required CLI is `broken` regardless of other metadata.
- There is no aggregate status.

**Rendering:** Use a table, structured list, or JSON — adapt to the context.
An explicit user request for a specific format takes precedence.

Diagnostics is read-only: it never repairs, creates, synchronizes, archives,
or closes native artifacts.

## Supporting disciplines (Superpowers, Grill)

Superpowers and Grill are supporting disciplines, not product owners. Their
`result` reflects whether the installed skill closure is available and
readable, not project-task state. When the current activity has no applicable
trigger for either, report them as `N/A`.

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
