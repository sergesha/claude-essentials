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

When the human explicitly invokes Wayfinder, also apply
[wayfinding.md](wayfinding.md) for the native owner binding.

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

### Cross-owner mutation recipe

For any action that crosses owner boundaries, follow these steps in order:

1. **Preserve intent**: Complete the ephemeral check in
   [transitions.md](transitions.md) — verify every approved upstream item
   appears in the proposed downstream result. For lifecycle closures
   (close, archive, complete), run [doctor](doctor.md) first to verify
   cross-level consistency before the transition.
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
| Changes owner semantics (scope, requirements, dependencies, status, acceptance, claim) | `Review: required` — state the observed effect and evidence |
| Read-only or mechanical (append evidence, record metadata) | `Review: skipped` — state the observed effect and evidence |
| Not yet inspected | Inspect the documented effects first, then classify |

**Mechanical means**: the exact text and native effects change no owner
semantics. Example: appending a test command and exit code is mechanical.
A comment declaring work complete, or an operation changing a dependency
or claim, is semantic — classify by the effect, not the field name.

### Required review protocol

1. Give a fresh reviewer: approved scope, exact proposed artifact/diff,
   evidence, and applicable native methodology.
2. The reviewer returns blocking defects (vs approved scope) separately
   from scope proposals.
3. End review when no approved-scope blockers remain.
4. Reuse an unchanged completed review — state
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

## Live queries

Use [diagnostics.md](diagnostics.md) for live `status` and `next`, including
owner-separated results and the applicable approval boundary. Both reports
remain read-only: they neither execute a proposed action nor persist
recommendations, phases, cursors, or queues.
