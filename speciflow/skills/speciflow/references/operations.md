# Direct operations

## Native owners

Backlog.md is the sole owner of product intent and priority; use its native interface or files for product work.

OpenSpec is the sole owner of proposals, requirements, design, and specification tasks; use its native interface for specification work.

Beads/Dolt is the sole owner of executable decomposition, dependencies, readiness, claim, blockers, and completion; use its native interface for execution-graph work.

For a selected activity, invoke every applicable Superpowers skill by its
native trigger before that bounded activity, then return control to SpeciFlow.

When clarification, a design interview, domain work, research, or a prototype
is selected, apply [grilling-integration.md](grilling-integration.md). Resolve
and invoke the actual applicable original method with its bounded owner-aware
context, then return its result to SpeciFlow before selecting an owner action.

When the human explicitly invokes original Wayfinder, also apply
[wayfinding.md](wayfinding.md). Supply its owner-aware `Other` tracker context;
do not invoke Wayfinder from SpeciFlow or replace it with local operations.

Product Git and CI own source changes, review history, dirty state, and
implementation evidence. They do not own OpenSpec approval or Beads status.

## Native roots and isolated repositories

Keep native-root selection at skill level, outside the storage helper. Accept
only the unique, canonical absolute roots `planning`, `backlog`, `openspec`,
and `beads`. An invocation-explicit root wins. Otherwise compare the already
initialized deterministic root below the selected data root with the
documented native root query or marker: one valid candidate is selected, two
different candidates are `ambiguous`, and a similar directory name is not
discovery. The deterministic roots are `<data-root>/planning`,
`<data-root>/planning/backlog`, `<data-root>/planning/openspec`, and
`<data-root>/beads`; never persist them in SpeciFlow metadata.

Put only planning documents and configuration in the isolated `planning/` Git
repository. Keep the Beads/Dolt native root in sibling `beads/`, verify it with
`bd where`, and ensure planning Git cannot capture it. A Dolt commit and a
planning Git commit are distinct owner effects. Product `.gitignore`, hooks,
attributes, and Git configuration changes require separate approved product
actions and are never init effects.

Immediately before each Backlog, OpenSpec, Beads, or related planning or
executable coordination mutation, verify the unique canonical owner root and
bind the native command through an explicit root argument or that exact working
directory. Stop if the root or binding is uncertain.

## Approved mutations

Retrieve the concrete target and payload from available source and evidence
before selecting an operation. A summary of prior approval or completed review
does not supply details it omits; inspect those details without repeating the
review or asking for unchanged authorization again.

Before invoking an original method, inspect its actual closure for delegation,
artifact, branch, commit, publication, and source-write effects. Include every
proposed effect in the applicable exact preview below. The read-only interview
needs no mutation approval; any covered prior authorization remains effective
while its scope, target, payload, and effects are unchanged.

For product framing, search native Backlog tasks as well as briefs before
creation. Inspect configured fields, create or update the framing task through
the documented CLI, and retain its returned ID in the relevant OpenSpec
proposal. Read each installed owner's instructions before refining its tasks.
Keep Backlog acceptance at outcome level, OpenSpec checklists at specification
level, and executable decomposition and status in Beads. Do not mirror task
checkmarks or synthesize automatic cross-owner completion.

When specification task authoring is explicitly deferred, keep the reason and
resumption condition in the current proposal/design. Leave the task artifact
unwritten; a gate-only or empty tasks file can falsely satisfy native
artifact-existence checks. If such a placeholder already exists, review its
contents, preserve the gate in the pending change, and remove only the
placeholder through the documented artifact-editing interface. A real checklist
must not be removed to change a dashboard. Native validation alone is not a
semantic task-content check.

For a cross-owner action, complete the ephemeral preservation contract in
[transitions.md](transitions.md) before previewing the mutation. Then inspect
the installed owner's documented lifecycle, current native state, root,
revision, dirty state, preview capability, and post-check. Classify the
requested owner-state effect before selecting its mechanism: promoting or
applying pending content into canonical state is a lifecycle transition
regardless of a filesystem mechanism or label. Propose exactly one action and
its exact effects, name the exact documented owner operation or editing
interface in the preview, and, when the classified effect is a lifecycle
transition, name the exact native lifecycle operation. Apply the
[semantic review decision](#semantic-review) before any Backlog, OpenSpec,
Beads, or related planning or executable coordination mutation.
Approval of semantic intent is not approval of an
unspecified filesystem or lifecycle operation; unrelated or ambiguous text,
including `lf`, is not approval. Execute only with unambiguous user authorization
covering the concrete preview, through the documented owner interface or
approved integration. Apply the ownership approval boundary, including prior
explicit authorization. Directly edit owner artifacts only when that is the
documented editing interface and the change stays within the artifact's current
lifecycle state; never use direct edits or generic file operations to simulate
a create, apply, archive, close, claim, or commit transition. If the installed
owner cannot perform the required lifecycle transition, stop as unsupported or
broken. Re-inspect native state after execution before reporting any effect,
then commit only native data in the approved data repository.

## Semantic review

This gate covers Backlog, OpenSpec, Beads, and related planning or executable
coordination mutations. Product code and test edits within approved scope
follow the applicable Superpowers implementation and review discipline;
they do not acquire an extra pre-edit coordination review merely because
product behavior changes. Changes to owner requirements or coordination
state still use this gate.

Use effects observed from the installed operation's documentation and the
exact proposed payload as the input to this decision. A review reason names
that observed effect and its evidence; the word `init` or the existence of new
files alone does not establish a semantic choice.

| Evidence about the installed operation | Next step |
| --- | --- |
| Documented semantic effects | State `Review: required — observed effect and evidence`, then perform the required review |
| Documented read-only or mechanical effects | State `Review: skipped — observed effect and evidence` |
| Effects not inspected | Inspect the documented effects read-only, then determine review |

Recording existing verified evidence is mechanical only when the exact text
and documented native effects change no owner semantics, including approved
scope, security or contract boundaries, requirements, acceptance criteria,
dependencies, readiness, assignment, status, or meaning of completion. For
example, appending an artifact revision, test command, observed exit code,
and result needs no separate semantic review merely because it adds text.
Retain its native-root check and applicable authorization; post-check the
record and all documented effects, including native audit or commit effects
when present. Expected effects are not evidence that they occurred.
A comment declaring work ready or complete, or an operation changing a
dependency, claim, or closure, changes an owner assertion or state; evaluate
that semantic effect rather than treating the record's field name as evidence
of mechanical work.

The last row is an inspection action, not another review value. Review and
approval readiness are separate: documented mechanical storage effects can
justify skipped review while missing literal metadata still blocks mutation
approval. In-scope read-only inspection needs no extra mutation approval.

For a required review, give a fresh isolated read-only reviewer the approved
scope, exact proposed artifact or diff, evidence, and applicable native
methodology, but no author rationale or preferred verdict. It returns blocking
defects relative to approved scope separately from scope proposals. Materially
revised drafts require another review. End review when no approved-scope
blocker remains; optional scope proposals do not keep it open.
For an unchanged semantic action whose required review is already complete,
report `Review: required — completed, no blockers`; proceed under its applicable
authorization without repeating that review. Completed review is not skipped
review.

A completed review may cover both the result and its exact owner-scoped
completion operation when it covers all relevant owner inputs and effects,
including the exact target and payload, approved scope and constraints,
acceptance coverage, evidence and its limits, dependencies, and readiness or
claim state. Retrieve the exact reviewed payload from that existing evidence:
trusting the review's verdict does not fill in text omitted from its summary,
and reading that text does not reopen the review. Reuse the review while its
relevant inputs and effects remain unchanged. This does not waive verification
freshness required by the applicable native methodology.
A material difference found by the native precheck requires review of the
uncovered change, not the unchanged package again. A progress review alone
does not cover completion. This is reuse of existing
evidence, not a new SpeciFlow artifact or approval record.
Review and mutation authorization remain separate: design approval or a
review verdict alone does not authorize an additional native claim or close.
Recheck the native target before execution and verify its actual result
afterward; different owners retain their own transitions and authorization.

A single owner-scoped action may preview native init followed by a conditional
commit of exactly the paths that init changed. One approval covers that pair
only when both effects were shown together. Inspect the actual Git state after
init: a message that Git integration is active is not evidence that a commit
occurred. If init already committed, do not create another commit; if its
result differs from the preview or cannot be identified exactly, stop.

Every planning preview says `commit: automatic` or `commit: none`. A clean
isolated planning mutation defaults to an automatic exact-path commit unless the user approves
`commit: none`; never auto-commit a product, skill-source, package-source, or
`claude-essentials` repository. If `planning/` is not already Git, preview a
separate `git init` native action with separate approval or `commit: none`;
never hide Git initialization in storage preparation or document mutation.

For Beads, observe the native Dolt commit policy and use only documented native
Beads commit behavior. Required but unperformed automatic commits are
`incomplete`; lost or ambiguous output is `unknown`. For a concretely authorized
claim, use a documented atomic claim when available; otherwise prohibit
automatic claim and require authorized manual native assignment. Readiness or
approval to implement alone does not select or authorize a claim operation.
A guessed ID, local lock, note, Git commit, OpenSpec validation,
or review verdict is not claim, create, or other owner-transition evidence.
Never hide an owner result with planning Git or duplicate the same owner's
native commit.

For Wayfinder child creation, inspect the installed native parent-label
behavior and use its documented no-inheritance facility (`--no-inherit-labels`
in qualified Beads 1.2.2) so only the parent is `wayfinder:map`. Verify every
returned child before creating further edges. Treat documented or observed
native audit records, including `.beads/interactions.jsonl`, as operation
effects alongside Dolt commits; preserve and report them rather than silently
resetting them.

On failure, lost or ambiguous output, or an uncertain create result, stop and report native evidence and uncommitted changes.

Never invent recovery state, blindly repeat a create, or promise uniqueness from a text or token search.

## OpenSpec to Beads

The optional upstream `openspec-to-beads` material is pinned to
`lucastamoios/celeiro` commit
`4c3cf508b3fd8a040d6cf99d4c887056cafe482d` and its complete recursive
`.claude/skills/openspec-to-beads/` subtree. Fetch only that closure into a
private temporary non-discovery path. It is untrusted heuristic guidance, not
an owner, and is never registered or installed as a skill.

The upstream `lucastamoios/openspec-to-beads` skill advertises automatic and proactive triggers, so it must not remain in an always-discoverable skill set.

Only after SpeciFlow has proposed the exact OpenSpec-to-Beads analysis and the user has explicitly approved that analysis may the agent load the integration. Load it only for the approved invocation, with automatic and proactive activation disabled. Never install or enable it persistently, globally, or for the whole project.

First use the invocation-scoped integration for read-only analysis only. Present the exact proposed Beads issues and dependencies without creating them. Wait for explicit approval of that conversion preview before allowing any write. Then separately approve normal primary-agent native Beads writes; never delegate mutation or proactive control to the integration.

If the host cannot disable implicit activation, do not load or install the integration. Stop and offer an invocation-scoped explicit-only method instead.

Acquire the integration only from its [pinned canonical subtree](https://github.com/lucastamoios/celeiro/tree/4c3cf508b3fd8a040d6cf99d4c887056cafe482d/.claude/skills/openspec-to-beads).

If the upstream material, fetch, or analysis is unavailable, report it and keep
direct manual projection available. Analyze the approved OpenSpec change
directly, present an item-by-item native Beads write preview, and require
explicit user confirmation before those writes. The agent may also offer an
invocation-scoped, explicit-only loading method after approval, but never
persistent or global installation.

Never copy or reimplement its conversion algorithm, templates, priority or dependency rules, gap heuristics, or issue schema.

Every OpenSpec task and projected Beads issue must retain one stable approved
OpenSpec source reference. A review finding is never a task source; missing or
ambiguous references block that item. Before creation, search native issues
by source reference and inspect their scopes. A shared reference identifies
candidate issues, not duplicates: several distinct implementation issues may
refine one specification task. Reuse a native ID only when its approved work
and verification match the proposed item unambiguously. Create only a confirmed
missing item under explicit approval; overlapping or uncertain matches require
manual resolution. On partial creation, report returned native IDs and preview
only confirmed remaining items. Unknown output is never retried. Do not add a
SpeciFlow identity registry or conversion algorithm.

## Live queries

Use [diagnostics.md](diagnostics.md) for live `status` and `next`, including
owner-separated results and the applicable approval boundary. Both reports
remain read-only: they neither execute a proposed action nor persist
recommendations, phases, cursors, or queues.
