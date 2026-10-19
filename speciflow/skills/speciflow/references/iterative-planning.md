# Iterative planning

Planning is exploration — you don't know what you don't know. Instead of
trying to produce a complete plan in one pass, use iterative refinement
driven by Grill interviews at each stage.

## Two-phase lifecycle

```
Phase 1: Backlog refinement (broad strokes → sufficient scope)
Phase 2: OpenSpec refinement (scope → complete specification)
```

Each phase is a loop of: interview → refine → commit → check sufficiency.

## Phase 1: Backlog refinement

Start with the user's rough idea. Each iteration sharpens it into a
Backlog task with clear scope.

Define what and why: high-level requirements, DoD, NFRs, and constraints.
Do not prescribe implementation. OpenSpec owns the technical solution and
plan; absence of those details here does not make Backlog insufficient.

### Iteration protocol

1. **Capture**: Write the current understanding as a Backlog task draft
   (or update from previous iteration).
2. **Interview**: Challenge the current draft (apply the original-method availability rule in
   [grilling-integration.md](grilling-integration.md#when-the-formal-method-is-unavailable)):
   - What assumptions are untested?
   - What's missing from scope boundaries?
   - What does "done" look like — can you test it?
   - What domain concepts are ambiguous?
3. **Refine**: After the original interview process and required confirmation
   are complete, incorporate only its settled outcomes into the draft. Unresolved
   choices remain in that process; do not introduce a coordinator-written interview.
4. **Commit**: Save the refined Backlog task as a versioned checkpoint.
   Commit message: `plan(backlog): iteration N — <what changed>`.
5. **Sufficiency check**: Evaluate against the criteria below.
6. **Continue or transition**: If not sufficient → next iteration (step 2).
   If sufficient → transition to Phase 2.

### Sufficiency criteria (Backlog → OpenSpec)

Before checking sufficiency, run [doctor](doctor.md) to verify coverage
and detect orphans or drift introduced during this iteration.

All four must be met to transition:

- [ ] **Outcome**: What should exist when this is done? Stated as a
  user-visible result, not an implementation task.
- [ ] **Scope boundaries**: What is explicitly NOT included? At least
  one exclusion identified.
- [ ] **Acceptance sketch**: How will we verify it works? At least one
  concrete acceptance criterion.
- [ ] **Unknowns resolved or marked**: Major unknowns either answered
  or explicitly deferred to OpenSpec investigation.

### Convergence check

After each iteration, assess whether the draft is converging:

- **Converging**: Each iteration resolves more unknowns than it surfaces.
  Continue.
- **Plateauing**: Iteration N surfaced as many unknowns as it resolved.
  Flag to user: "This may need decomposition — the scope might be too
  broad for a single task."
- **Diverging**: Each iteration surfaces MORE unknowns. Stop and
  decompose into smaller tasks.

Don't wait for 5 failed iterations — flag the pattern early.
Non-convergence is a signal about scope, not a failure of the process.

## Phase 2: OpenSpec refinement

Start from the sufficient Backlog task. Each iteration sharpens it into
a complete specification.

### Iteration protocol

1. **Draft**: Write the current understanding as an OpenSpec change
   (or update from previous iteration).
2. **Interview**: Stress-test the specification (apply the original-method availability rule in
   [grilling-integration.md](grilling-integration.md#when-the-formal-method-is-unavailable)):
   - Do requirements cover all acceptance criteria from Backlog?
   - What edge cases are unaddressed?
   - Are requirements specific enough to implement and test?
   - Do any requirements contradict each other?
3. **Refine**: Formulate the technical solution and design in OpenSpec,
   within Backlog's approved outcomes and constraints.
   Derived requirements and scenarios should be testable and unambiguous;
   implementation details belong in OpenSpec design/plan artifacts. A new
   product requirement or change to an approved parent uses
   [parent contract amendments](ownership.md#parent-contract-amendments).
4. **Commit**: Save the refined spec as a versioned checkpoint.
   Commit message: `plan(openspec): iteration N — <what changed>`.
5. **Completeness check**: Evaluate against the criteria below.
6. **Continue or approve**: If not complete → next iteration (step 2).
   If complete → spec is ready for approval and Beads projection.

### Completeness criteria (OpenSpec → Beads)

Before checking completeness, run [doctor](doctor.md) to verify that
all Backlog criteria are covered and no orphan requirements appeared.

All four must be met to approve:

- [ ] **Coverage**: Every Backlog acceptance criterion has at least one
  requirement addressing it.
- [ ] **Specificity**: Each requirement is concrete enough to write a
  test for. No "should handle edge cases appropriately."
- [ ] **Consistency**: No requirements contradict each other.
- [ ] **Unknowns sufficient**: Every unknown whose resolution could
  change the contract, alter this phase's deliverables, or cause
  significant rework is answered — regardless of where the unknown
  originated (Backlog, interview, or implementation). Remaining
  unknowns that cannot affect the current phase are noted with their
  impact boundary and decision deadline — they do not block this stage.

## Ready parts within the complete scope

Retain the complete approved outcome, including unresolved parts and their
prerequisites. Refine the selected part until its contract and verification are
sufficient; an unknown blocks only work whose result it can change. Independently
ready, authorized work may proceed. Record pending scope and its resumption
condition with the existing native owner, not as a new SpeciFlow state.
A ready part does not establish product-wide completeness. Apply the stage-aware
coverage check in [doctor.md](doctor.md#1-coverage-top-down).

## Cross-phase assumption invalidation

During OpenSpec refinement, a Grill interview may reveal that a core
assumption from the Backlog task is wrong (e.g., "we have purchase
history" turns out to be "we only have browse behavior").

When this happens:

1. **Check the effect on approved meaning.** A factual discovery does not
   authorize changing a requirement or substituting the promised outcome.
2. **Seek a solution within the current contract.** If none works without
   changing a parent, use [parent contract amendments](ownership.md#parent-contract-amendments):
   user grilling and an actual answer before dependent owner mutation. Reuse
   an exact existing decision; a reviewer agreeing the premise is wrong is
   not that decision.
3. **Amend only covered effects** at the highest affected owner, then
   re-evaluate sufficiency and refine its dependants. Calling the update a
   correction rather than scope expansion supplies no authority.
4. **Resume affected work** after the prerequisite is resolved. Independent
   authorized work continues throughout.

This is not a failure — it's the process working. Discovering wrong
assumptions during spec refinement is better than discovering them
during implementation.

## Grill invocation for planning

Planning exploration without an approved-parent amendment uses a simplified
Grill context. For that interview, pass:

```text
Question: <the specific gap or assumption being challenged>
Current draft: <link to or summary of the current Backlog/OpenSpec artifact>
Return condition: complete the original grilling process for this bounded question, including its required confirmation, then return to coordination
```

Use the full 6-slot context from [grilling-integration.md](grilling-integration.md)
for approved-parent amendments in any phase and for bounded implementation
interviews. A planning label does not remove the applicable authority checks.

## Returning from the interview

SpeciFlow supplies the bounded planning question; the original grilling skill
controls the interview process. Do not replace it with an adversary/facilitator
script or switch to refinement before its completion condition is met.
After completion, return to coordination and record only covered results with
the correct owner through the existing mutation recipe. The user remains the
decision-maker except for an explicitly authorized bounded respondent substitution.

## Scope drift protection

After each iteration, compare the refined version with the previous:

- **Added detail within existing scope** → normal refinement, proceed.
- **Derived testable technical requirement within Backlog's boundaries** → OpenSpec
  refinement under the authoring grant, not automatically scope expansion.
- **New product requirement or changed approved parent contract** → use
  [parent contract amendments](ownership.md#parent-contract-amendments).
  This includes narrowing, exclusions, and substitutions. A draft proposal
  or interview finding is not an adopted requirement.

## What to commit

Commit the **planning artifact**, not the interview transcript:

- Backlog phase: the Backlog task in its native format
- OpenSpec phase: the OpenSpec change in its native format
- Each commit is a readable, self-contained snapshot
- The commit history IS the iteration history

## When NOT to iterate

Not every task needs iterative planning:

- **Simple, well-understood task**: Skip to Backlog task creation.
  One Grill interview if there's doubt, then straight to OpenSpec.
- **Bug fix within approved scope**: No planning needed — it's a defect,
  not a new feature.
- **Mechanical change**: No planning needed.

The iteration protocol is for tasks where the user starts with an idea
that needs exploration and refinement to become actionable.
