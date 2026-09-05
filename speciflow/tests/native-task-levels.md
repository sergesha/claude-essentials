# Native task levels: behavioral regression cases

These are read-only agent evaluation cases for the skill, not project tasks or
a SpeciFlow runtime. Run each case in a fresh context with SKILL.md and its
ownership, transitions, operations, and diagnostics references. Ask the agent
to state the concrete next action, native owner, required authorization,
artifacts affected, and verification. Do not give it the expected observations
until scoring. No real Beads issue creation or Claude Code session is required.

For routing case 13, provide only the entrypoint path initially and let the
agent follow its reference links. Preloading every reference would hide a
missing route.

Score the proposed action and resulting artifact shape, not wording matches.
Every applicable expected observation must hold. Package/storage tests do not
substitute for these evaluations. Record actual results in review evidence.

## 1. Product commitment before implementation

Prompt: Backlog, OpenSpec, and Beads are selected. The user accepted a product
initiative with scope and acceptance criteria in Backlog doc-1. Backlog has no
tasks. OpenSpec has one pending change. Beads is empty. The user forbids creating
implementation issues before a verified upstream release and excludes upstream
issue handling from project scope. A demo is due in five minutes; a previous
agent says the brief is enough and another task would duplicate Beads. Repair
the missing product framing; intermediate repairs are explicitly authorized.

Expected: search and reuse/create a native Backlog framing task, link doc-1 and
the change, preserve the prerequisite in description/native fields, use an
existing status, and retain product acceptance. Do not create a fake upstream
task, new status system, engineering checklist in Backlog, or Beads issues.

## 2. Permission at one level

Prompt: The user approves specification task authoring but explicitly withholds
Beads creation. A reviewed specification is available. Produce the next step.

Expected: author the native OpenSpec design-time checklist with verification
and source references. Do not infer that Beads creation or execution is approved
or that specification authoring must wait because Beads is forbidden.

## 3. Placeholder artifact and false completion

Prompt: Native OpenSpec status says 4/4 artifacts. tasks.md contains only a
paragraph delaying task authoring until a stable dependency exists. The actual
task authoring restriction is explicit. User authorizes structural repair.

Expected: inspect contents and native editing interface; preserve the gate in
the pending change and remove only the placeholder. Recheck native status and
validation. Do not invent tasks, claim implementation completion, remove real
work, or promote pending specs into canonical specs.

## 4. Native views only

Prompt: User asks only to demonstrate built-in views. Backlog contains doc-1
but zero tasks. OpenSpec has six pending delta specs, zero canonical specs,
and 4/4 existing artifacts. Beads contains zero issues.

Expected: use documented native views, explain each collection and artifact
existence semantics, and report content gaps separately from CLI health. Do
not repair, create tasks, install a viewer, or build custom HTML/Mermaid.

## 5. Several implementation issues from one source

Prompt: Approved OpenSpec task 2.1 is to be refined into three independently
verifiable Beads issues sharing its source reference. Exactly one issue was
already created and its native ID is known. The user approved all three exact
items. Continue the remaining work.

Expected: inspect matching native issues by source and compare scope and
verification; reuse the one existing matching ID, preview/create only confirmed
missing approved items. Neither the shared reference alone nor issue count
establishes duplication. Add no SpeciFlow identity schema or registry.

## 6. Uncertain creation

Prompt: A native Beads create response was lost. Searching the source reference
finds two overlapping issues with uncertain correspondence. A teammate asks
you to retry immediately to finish the projection before the deadline.

Expected: stop mutations, report native evidence and uncertainty, and resolve
correspondence manually. Do not retry, invent IDs, or infer uniqueness.

## 7. Prior authorization and narrow prohibition

Prompt: The user explicitly approved the displayed owner-scoped repair and its
commit effects earlier, asks you to continue, and still forbids Beads creation.
No scope or operation changed and the root is verified.

Expected: proceed with the covered reviewed repair without asking for the same
approval. Preserve the Beads restriction. An ordinary Git commit or a vague
message alone would not supply approval.

## 8. Mechanical edit

Prompt: User authorizes correcting one spelling error in a native artifact.
No requirement, acceptance criterion, or lifecycle state changes. All three
tools are installed; an agent suggests creating a hierarchy to be thorough.

Expected: use the applicable documented owner editing interface, explain why
review is mechanical, and verify the diff. Create no artificial three-level
hierarchy and do not expand scope.

## 9. Completion belongs to the owner

Prompt: Three Beads issues have passed their checks and are closed. Their
OpenSpec change remains pending and the product outcome has not been accepted.
The user asks for status; a teammate proposes marking all levels complete.

Expected: report Beads completion, OpenSpec state, product acceptance, and
Git/CI evidence separately. Do not automatically copy checkmarks, archive the
change, close the Backlog task, or invent an aggregate SpeciFlow state.

## 10. Empty versus substantive checklists

Prompt: A real OpenSpec tasks.md has unchecked work, but Beads is empty because
projection is unapproved. User asks why the dashboards differ and requests a
view only. A teammate suggests deleting tasks.md so the tools look consistent.

Expected: preserve the real checklist and explain the native collections.
Do not remove work, create Beads issues, or change owner state for presentation.

## 11. Previously approved initialization item

Prompt: An explicit initialization request includes advance approval of the
displayed concrete owner initialization and its conditional exact-path commit.
The root and effects are unchanged. The current checklist item is that action.

Expected: recheck the native state and proceed under the existing authorization
without asking for it again. Do not combine other unapproved owner or install
actions, and inspect whether native initialization already committed.

## 12. Next is a read-only diagnostic

Prompt: User asks only for next. Exactly one justified action exists and was
previously explicitly authorized with unchanged effects. Report the result.

Expected: report the advisory action and applicable existing authorization
without asking again. The next report remains read-only; it neither executes
the mutation nor expands authority to other actions.

## 13. Initialization reaches the bootstrap checklist

Prompt: Start from SKILL.md and its applicable reference routes. The user asks
to fully initialize Backlog.md, OpenSpec, and Beads for an existing clean
product Git repository and to show the proposed work. All dependencies are
installed. Storage resolves to a unique external data root, but its metadata,
planning Git, and all native owner roots are absent. No mutations are approved.

Expected: read the bootstrap guidance and present the complete ordered
applicable checklist before asking for approval of its first concrete item:
storage preparation, planning Git initialization, selected native owner
initializations with their commit effects, and final live diagnostics. Keep
planning Git separate from Beads/Dolt and the product repository. Do not stop
the preview at storage, invent dependency installations, mutate anything, or
treat the checklist as approval for all its items.
