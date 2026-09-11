# Native Wayfinder binding

Apply this reference only after the human explicitly invokes the original
`mattpocock/skills` `wayfinder` entrypoint. Wayfinder is user-only: SpeciFlow
must not invoke it, simulate it, or copy its charting, interview, selection, or
resolution method. Resolve the original entrypoint to the installed
`mattpocock/skills` source, completely read it and every linked file in its
closure, and hold that closure stable for the activity. Then
pass the native context below through Wayfinder's documented `Other` tracker
extension. The original method remains in control of wayfinding; this reference
only binds owners and operations.

## Invocation context

Fill and pass these values in memory, not in a SpeciFlow file or schema:

```text
Wayfinding tracker: verified native Beads/Dolt root and qualified installed
map, child, dependency, readiness, claim, comment, close, and history operations.
Planning owners: verified Backlog and OpenSpec roots, authoritative source
revisions or user-approved dirty snapshots, and their linked artifacts.
Product authority: approved Backlog outcome, scope, priority, exclusions, and
unresolved prerequisites.
Semantic authority: current OpenSpec change and stable question references.
Permitted effects: exact existing authorization for owner writes, native
commits/audits, research or prototype artifacts, delegation, branches, and
product-source changes.
Return condition: finish the selected bounded activity and return cross-owner
selection to SpeciFlow.
```

Project domain documents remain sources in their documented role. The original
`domain-modeling` method may actively challenge them, but definitions, design
answers, and rationale return to the current OpenSpec context through its
authorized editing interface.

## Native owner mapping

Use one Beads graph. The map is a native parent/index carrying only the
`wayfinder:map` label and the original low-resolution map sections. Destination,
Notes, and Out of scope summarize and link approved Backlog intent; they are not
scope authority. Fog points to unresolved OpenSpec questions or areas.
Decisions-so-far links named child issues and their OpenSpec results without
copying answers or live status.

Each sharp question or prerequisite is a native child whose returned Beads ID
is its identity. Bound its research, interview, prototype, or prerequisite work
and link the stable OpenSpec question and source revision. Use the original
`wayfinder:<type>` label. A standard native task is sufficient; an optional
native decision type does not transfer semantic ownership from OpenSpec.

Use native parent relationships, blockers, readiness, assignment/claim,
comments, close, and history. Do not mirror that state in OpenSpec checklists,
Markdown fields, map prose, or SpeciFlow metadata. Beads owns execution state;
its assignee is the claimant, not the semantic owner.

Preserve original chart breadth, fog, ticket types, create-then-wire order,
named links, linked assets, one-selected-ticket rule, research exception, and
fresh-session behavior. Human answers stay HITL. Product facts and requirements
stay in Backlog and OpenSpec rather than becoming canonical issue descriptions.

## Qualified native operations

Before every write: verify the canonical root, installed help, and current
state; preview the exact payload and effects; apply semantic review and
existing authorization; then inspect the native result. Include documented or observed Dolt commits and CLI audit writes such
as `.beads/interactions.jsonl` in the effect preview and post-check. Preserve
native audit effects even when Git reports them dirty.

Use returned native IDs only. Create the map before its children, create all
children before wiring blockers, and verify every response. Suppress parent
label inheritance through the installed native facility: for qualified Beads
1.2.2 child creation this is `--no-inherit-labels`. Verify each child's parent,
intended type label, source reference, and absence of `wayfinder:map` before the
next create or edge. Never identify a map by label alone. A mismatch, partial
result, or unknown output stops the operation without silent correction, blind
retry, or guessed IDs.

SpeciFlow selects the next cross-owner action from current native state. Honor
a child the human names; when only a map is named, query the current child
records, blockers, readiness, and claims, including each ready child's current
native title and OpenSpec question reference. Complete this available read-only
lookup within the current `next` report; do not return it as future work or ask
the human to supply facts available from the owner. When candidates are equally
supported, present every candidate before asking the human to choose in the
shape `[<native title>](<documented native link or path>) — <OpenSpec question
reference>`, substituting only values obtained from the current owners.
Wayfinder may propose a solution inside the selected activity but must not
assign itself the next Bead.

The primary agent performs every owner write. It atomically claims selected
work as the first Beads write; a losing or unknown claim stops that work. A
research agent applies the original `research` method and returns cited evidence
only: it does not claim, comment, close, update the map, write OpenSpec, or
select a neighbor. Preserve parallel research when all affected tickets are
ready and the exact delegation, artifact, branch, and commit effects are
authorized. Otherwise perform only the authorized chart or resolution effects.

Research capture, prototype capture, product promotion, owner recording,
comment, close, and map update are distinct effects. Map Notes cannot grant
implementation permission. New tickets remain a separately previewed native
create-then-wire action. A scope exclusion, narrowing, issue deletion, or
scope-based close first returns to Backlog for approval.

## Owner result and completion

Resolve according to the child's bounded acceptance, not its label:

- Work scoped only to research or a prerequisite may close when its accepted
  evidence or performed-work result is linked. Any proposed semantic answer
  remains pending; that close is not OpenSpec acceptance or a map decision.
- Work that promises a semantic decision, including research-labelled work,
  resolves only after the human supplies the answer and rationale and they are
  reviewed, authorized, recorded, and verified in the current OpenSpec context.
  The Beads resolution comment links that result and may add a clearly
  non-authoritative gist before native close.

Agreement in conversation is neither an OpenSpec write nor Beads completion.
The map update links the child and owner result without copying either. After
the original single selected non-research activity, or its permitted research
set, return control to SpeciFlow.

## Fresh resume and restrictions

A later session requires a fresh explicit human Wayfinder invocation.
Re-resolve the original source and closure, record its current revision when
available, and verify the Backlog, OpenSpec, Beads, and relevant Product Git
roots and revisions. Query the native map, children, dependencies, readiness,
claims, comments, close state, history, commits, and audits; then follow owner
references and verify their current revisions. A handover, transcript, cached
source, map gist, or untracked report is only a pointer to inspect. Never restore
a local cursor or copied status.

If the user forbids Beads writes, do not chart, claim, comment, close, or update
a map, and do not fall back to `.scratch`, local Markdown tickets, OpenSpec task
status, or another registry. Continue only allowed reads, original-method
discussion, or independently authorized OpenSpec edits, and name the unavailable
Wayfinder capability precisely. This restricted path is not a completed native
multi-session route.
