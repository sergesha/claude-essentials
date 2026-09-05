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
| `broken` | A selected owner has a missing required tool or interface. State what is missing and apply the root skill's missing-tool rule: offer one safe owner-specific loading or installation action, but do not run it without approval. |
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
- Superpowers: the selected installed skill closure resolves and required
  applicable skills are readable; Superpowers owns no project status.

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

## Bootstrap preview

For an explicit setup or full-stack initialization request, inspect all
selected owners and show the complete ordered checklist before asking for the
first approval. Include only applicable steps: owner dependency installation,
storage initialization (not only read-only resolution), planning Git init when
required, each applicable native-owner initialization with its commit effect,
and a final live status
check. Show the exact source, target, effects, and approval boundary for every
mutating item.

The checklist is an ephemeral preview, not SpeciFlow state, a queue, or
authorization for all steps. Reinspect native state after each approved item
and update the remaining preview. Apply the ownership approval boundary to
each concrete item, including applicable prior authorization. When approval
is missing, ask only for the current item; when
multiple owner installations are equally valid, present them as an
`ambiguous` choice.

Distinguish the complete ordered setup outline from its current
approval-ready mutation. Distinguish observed facts from uninspected details
in the outline; uninspected details of later items do not gate a fully known
current item. Before requesting current approval, show the observed literal
source and target, documented operation, exact payload or configuration,
filesystem and native-state effects, and the applicable commit behavior. For
storage preparation, copy the literal `metadata_path` and the complete
three-field `expected_metadata` JSON returned by `storage.py resolve`.

If any required current-item fact is unavailable, the current response must
request no mutation approval, including conditional invitations or approval
for future execution. Do not label the current item approval-ready or claim
exact values were shown. State the missing fact and next in-scope read-only
inspection; perform it without extra approval when possible. If inspection is
unavailable or prohibited, report that limitation without asking mutation
approval.

Determine an initialization review from the installed version's documented effects,
not the name `init` or an absent root; unknown effects require read-only
inspection and may not be labeled mechanical or `Review: skipped`.

A same-owner native init and conditional commit of its exact resulting paths
may be one previewed action and one approval. Inspect the init result before
committing; if the native tool already committed, do not add another commit.
Never combine different owners, dependency installation, storage preparation,
or planning Git init under that native-owner approval.

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
