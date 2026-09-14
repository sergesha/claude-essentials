# Direct operations

## Native owners

Use each owner through its native interface:

| Owner | Use for |
| --- | --- |
| Backlog.md | Product intent, priority, acceptance criteria |
| OpenSpec | Requirements, design, specification tasks |
| Beads/Dolt | Executable decomposition, dependencies, claim, completion |
| Superpowers | TDD, debugging, verification, implementation review |
| Grill | Interviews, domain modeling, research, prototyping |
| Product Git/CI | Source changes, review history, implementation evidence |

**Supporting disciplines** (Superpowers, Grill): invoke by native trigger
before the bounded selected activity, then return control to SpeciFlow.
Both install and apply through the host's native skill interface.

**CLI knowledge caching:** Check each owner CLI's help (`--help`) once
per session, not per operation. After the first check, you know the
interface — reuse that knowledge for subsequent commands.

When the human explicitly invokes Wayfinder, also apply
[wayfinding.md](wayfinding.md) for the native owner binding.

## Native roots and isolated repositories

Keep native-root selection at skill level, outside the storage helper. Accept
only the unique, canonical root names `planning`, `backlog`, `openspec`,
and `beads` (relative to the selected data root). An invocation-explicit root wins. Otherwise compare the already
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

### Cross-owner mutation recipe

For any action that crosses owner boundaries, follow these steps in order:

1. **Preserve intent**: Complete the ephemeral check in
   [transitions.md](transitions.md) — verify every approved upstream item
   appears in the proposed downstream result. For epic-level closures
   (archive OpenSpec spec, mark Backlog task Done), run
   [doctor](doctor.md) first. Individual Beads issue close with passing
   code review and evidence does not need full doctor.
2. **Inspect**: Read the target owner's documented lifecycle, current native
   state, root, revision, and dirty state.
3. **Classify**: Determine whether the effect is a lifecycle transition
   (create, apply, archive, close, claim, commit) or an in-state edit.
4. **Preview**: Propose exactly one action with:
   - the documented owner operation (or editing interface for in-state edits)
   - for lifecycle transitions, the exact native lifecycle operation
   - the exact target, payload, and expected effects
5. **Review**: Apply [semantic review](#semantic-review) for owner mutations.
6. **Authorize**: Get unambiguous user approval covering this specific preview.
   Prior explicit authorization remains valid for unchanged operations.
7. **Execute**: Use only the documented owner interface.
8. **Verify**: Re-inspect native state after execution before reporting effects.
   For lifecycle transitions (closing, completing, archiving): Superpowers
   verification-before-completion must pass BEFORE the lifecycle transition.
   A passing verification is a prerequisite, not a separate approval.
9. **Commit**: Commit only native data in the approved data repository.

**Approval composition**: Supporting discipline approval (e.g., Superpowers
code review passes) and owner mutation approval (e.g., close Beads issue)
are two separate gates. One does not grant the other.

If the installed owner has no operation for the required lifecycle transition,
report it as unsupported and stop.

## Semantic review

This gate applies to Backlog, OpenSpec, Beads, and planning mutations.
Product code and test edits within approved scope use the applicable
Superpowers or Grill discipline instead.

### Review decision

Inspect the installed operation's documented effects and classify:

| Documented effects | Action |
| --- | --- |
| Changes scope, requirements, dependencies, acceptance criteria, or exclusions | `Review: required` — separate semantic review |
| Claim of an approved ready task, or close with passing code review + evidence | `Review: skipped` — verify evidence exists, no separate reviewer needed |
| Read-only or mechanical (append evidence, record metadata) | `Review: skipped` — state the observed effect |
| Not yet inspected | Inspect the documented effects first, then classify |

**Key distinction:** Claiming an already-approved task and closing it
with evidence from a completed code review are operational, not
semantic — they don't change the meaning of the work. A separate
semantic review is for changes that alter what the work IS (scope,
requirements, dependencies, acceptance criteria).

### Required review protocol

**Critical: use a separate clean-context subagent as reviewer.** The
agent that did the work cannot review it — self-review is fundamentally
unreliable.

**Two review modes:**

**Direct review** — "does this match the spec?"
- Reviewer receives: approved scope + diff/artifact + evidence
- Use for: code review, spec completeness, intent preservation,
  user-initiated changes

**Adversarial review** — "is the agent's own judgment trustworthy?"
- Reviewer receives: **original user intent** + current state (no
  intermediate rationalizations or agent's preferred verdict)
- Compares with the *original* user formulation, not the latest
  approved version (which the agent may have shaped)

Adversarial triggers only when ALL THREE conditions are met:
1. The **agent** is the origin of the proposal (not the user)
2. The proposal alters **semantic content** (not mechanical/typo)
3. The context is a **lifecycle gate** (not informational)

| Situation | Mode |
| --- | --- |
| Code review, user-initiated spec change | Direct |
| Agent proposes spec change at a lifecycle gate | **Adversarial** |
| Agent removes or weakens a requirement ("cleanup", "simplification") | **Adversarial** (even between gates) |
| Doctor at epic closure | **Adversarial** |
| Typo fix, formatting, mechanical correction | No review |
| Status report, informational query | No review |

**Protocol:**

1. Select mode from the table above.
2. Spawn a clean-context subagent with the appropriate inputs.
3. The reviewer returns blocking defects (vs approved scope) separately
   from scope proposals.
4. End review when no approved-scope blockers remain.
5. Reuse an unchanged completed review — state
   `Review: required — completed, no blockers`.

### Commit policy

| Repository | Policy |
| --- | --- |
| `planning/` (isolated Git) | `commit: automatic` for clean mutations; `commit: none` if user approves |
| Product, skill-source, `claude-essentials` | Manual commit only — never auto-commit |
| Beads/Dolt | Use native Dolt commit behavior only |

Include `commit: automatic` or `commit: none` in every planning preview.
If `planning/` is not yet a Git repo, preview `git init` as a separate action.

### Failure handling

On failure, lost output, or uncertain result: stop, report native evidence
and uncommitted changes. On a partial create: report returned IDs and preview
only remaining items.

## OpenSpec to Beads projection

OpenSpec owns specifications; Beads owns executable work. The projection
turns approved specs into implementation tasks while keeping each tool
authoritative in its domain.

### Projection protocol

1. **Read**: Analyze the approved OpenSpec change — requirements, tasks,
   acceptance criteria.
2. **Preview**: Present item-by-item proposed Beads issues with:
   - bounded work description and verification criteria
   - dependency edges between issues
   - stable source reference back to the approved OpenSpec item
3. **Deduplicate**: Search existing Beads issues by source reference. A
   shared reference identifies candidates, not duplicates — several
   implementation issues may refine one spec task. Reuse an ID only when
   its scope matches unambiguously.
4. **Approve**: Wait for explicit user approval of each proposed item.
5. **Create**: Create only approved items through native Beads writes.
   Report returned native IDs.
6. **Partial**: On partial creation, report completed IDs and preview
   only remaining items.

### Optional upstream integration

The `openspec-to-beads` integration from `lucastamoios/celeiro` can assist
with step 2 (analysis and preview). When available:

- Load it invocation-scoped with automatic activation disabled.
- Use it for read-only analysis only — present its output as a preview.
- All Beads writes go through the normal primary-agent native interface.

If the integration is unavailable, perform the analysis directly from the
approved OpenSpec content — the protocol is the same either way.

### Boundary

Each tool retains authority over its domain. The projection reads OpenSpec
and writes Beads; it changes neither tool's ownership model. Every projected
issue retains one stable approved OpenSpec source reference.

## Importing from non-native sources

When the user has existing work in non-native tools (Notion, Google Docs,
Jira, spreadsheets, etc.), treat those as **source material to migrate from**,
not as authoritative owners.

### Migration protocol

1. **Identify**: Ask the user for links/access to each external source.
2. **Read**: Extract the content from external sources (read-only).
3. **Classify**: Map each item to its native owner by concern:
   - Product intent, priorities, acceptance → Backlog.md
   - Requirements, design, specifications → OpenSpec
   - Implementation tasks, dependencies, status → Beads/Dolt
4. **Preview**: Present a migration plan showing what goes where, item by
   item. Flag conflicts and ambiguous items.
5. **Approve**: Get explicit approval before creating any native artifacts.
6. **Create**: Write through each native owner's interface, one owner at
   a time.
7. **Verify**: Run diagnostics to confirm the migrated state.

External sources remain non-authoritative after migration. The native
owner's copy becomes the source of truth. When source data conflicts
(e.g., two documents disagree on a requirement), flag each conflict to
the user before classifying — do not resolve conflicts autonomously.

## Exploratory (pre-Backlog) work

When the user is exploring an idea before committing scope (research,
feasibility, prototyping), no Backlog task or OpenSpec change exists yet.

1. Use Grill methods (research, prototype, domain-modeling) for the
   exploration.
2. Prototype artifacts are throwaway — label them as such.
3. If the exploration justifies proceeding, present findings and propose
   creating a Backlog task + OpenSpec change. Wait for approval.
4. If not, document why and stop. No owner artifacts are created.

Do not create Backlog tasks, OpenSpec changes, or Beads issues for
unapproved exploratory work.

## Reopening closed work

When completed work needs to reopen (production incident, discovered
defect, requirement change):

1. **Classify**: Is this a defect in approved scope (bug fix) or a new
   requirement (scope expansion)?
2. **Bug fix**: Reopen the affected Beads issue. The approved scope still
   covers it — no new Backlog approval needed.
3. **New requirement**: Route through Backlog as a new scope item, then
   OpenSpec, then Beads. The emergency does not bypass the process.
4. **Lifecycle reversal**: Reopening a closed Beads issue, un-archiving
   an OpenSpec change, or changing a Done Backlog task back to In Progress
   are lifecycle transitions — each requires separate preview and approval.

For a **bulk scope change** (project pivot, wholesale archival): before
closing or archiving, assess which completed work transfers to the new
scope. Present the reuse assessment to the user so they can decide what
to keep, archive, or discard before any lifecycle transitions begin.

## Multi-spec coordination

When a decision affects multiple approved specs or parallel workstreams:

1. Identify the shared dependency and all affected specs.
2. Use Grill for the shared decision — one interview, not one per spec.
3. Update all affected specs consistently with the same decision.
4. Preview all updates together so the user sees the full impact.
5. Each spec update is still a separate owner mutation with separate
   authorization.

## Incoming items during execution

When ideas, questions, or corrections arrive while work is in progress,
triage before acting. The default is **capture and continue** — not
interrupt.

### Triage (4 questions, instant)

1. Does this **block** the current work? (dependency, wrong assumption)
2. Is this a **mandatory stop** item? (security, irreversibility)
3. Does this **invalidate** a fundamental assumption of the current task
   **or the approved project architecture**?
4. Does this **compound in cost** with each task completed? (e.g., wrong
   database choice, wrong runtime — every finished task increases rework)

**Any YES → interrupt.** Apply the appropriate protocol (scope amendment,
mandatory stop, cross-phase invalidation). Current work pauses.

**All NO → capture to inbox, continue current work.**

### Inbox

The inbox is a lightweight capture — not a Backlog task. Use one of:

- `backlog draft create "<one-line summary>"` — Backlog draft (no full
  task creation, no preview/approval cycle)
- A `## Inbox` section in the current planning document
- A timestamped note in `<data-root>/planning/inbox.md`

Each item gets: one-line summary, source (who said it, when), and
category tag (`idea`, `question`, `correction`, `future-scope`).

Capture takes seconds, not minutes. No approval needed. Continue
current work immediately after capture.

### Processing inbox

After the current work unit completes (Beads issue closed, iteration
finished, or natural pause):

1. Review inbox items.
2. Each item goes through normal speciflow routing — iterative planning
   for new ideas, scope amendment for corrections, doctor for alignment
   checks.
3. Empty items that were addressed during work or became irrelevant.

The inbox is ephemeral — not a second backlog or queue. If an item
survives two reviews without action, promote it to a real Backlog task
or discard it.

## Live queries

Use [diagnostics.md](diagnostics.md) for live `status` and `next`, including
owner-separated results and the applicable approval boundary. Both reports
remain read-only: they neither execute a proposed action nor persist
recommendations, phases, cursors, or queues.
