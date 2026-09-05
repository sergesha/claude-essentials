# Direct operations

## Native owners

Backlog.md is the sole owner of product intent and priority; use its native interface or files for product work.

OpenSpec is the sole owner of proposals, requirements, design, and specification tasks; use its native interface for specification work.

Beads/Dolt is the sole owner of executable decomposition, dependencies, readiness, claim, blockers, and completion; use its native interface for execution-graph work.

For a selected activity, invoke every applicable Superpowers skill by its
native trigger before that bounded activity, then return control to SpeciFlow.

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
transition, name the exact native lifecycle operation. Then state `Review:
required|skipped — reason` before any Backlog, OpenSpec, Beads, or related
planning or executable coordination mutation. Review is required for a
semantic mutation; skip it only for demonstrably read-only or mechanical work
and name the reason. Approval of semantic intent is not approval of an
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

For a required review, give a fresh isolated read-only reviewer the approved
scope, exact proposed artifact or diff, evidence, and applicable native
methodology, but no author rationale or preferred verdict. It returns blocking
defects relative to approved scope separately from scope proposals. Materially
revised drafts require another review. End review when no approved-scope
blocker remains; optional scope proposals do not keep it open.

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
`incomplete`; lost or ambiguous output is `unknown`. Use a documented atomic claim
when available; otherwise prohibit automatic claim and require manual native
assignment. A guessed ID, local lock, note, Git commit, OpenSpec validation,
or review verdict is not claim, create, or other owner-transition evidence.
Never hide an owner result with planning Git or duplicate the same owner's
native commit.

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

Read `status` live from the four native tools and persist nothing.

Derive `next` live as one non-authoritative action, present it for approval, and persist no recommendation, phase, cursor, or queue.
