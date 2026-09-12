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

### Iteration protocol

1. **Capture**: Write the current understanding as a Backlog task draft
   (or update from previous iteration).
2. **Interview**: Challenge the current draft (using Grill methods when
   installed, or general interview principles when not):
   - What assumptions are untested?
   - What's missing from scope boundaries?
   - What does "done" look like — can you test it?
   - What domain concepts are ambiguous?
3. **Refine**: Switch from adversary to facilitator. Help the user fill
   the gaps surfaced by the interview. Use the agent's knowledge to
   propose answers, but the user decides.
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
2. **Interview**: Stress-test the specification (using Grill methods
   when installed, or general interview principles when not):
   - Do requirements cover all acceptance criteria from Backlog?
   - What edge cases are unaddressed?
   - Are requirements specific enough to implement and test?
   - Do any requirements contradict each other?
3. **Refine**: Help the user formalize findings into spec language.
   Requirements should be testable and unambiguous.
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
- [ ] **Unknowns resolved**: No open questions remain — every deferred
  unknown from Backlog is answered.

## Cross-phase assumption invalidation

During OpenSpec refinement, a Grill interview may reveal that a core
assumption from the Backlog task is wrong (e.g., "we have purchase
history" turns out to be "we only have browse behavior").

When this happens:

1. **Don't patch the spec around the wrong assumption.** The Backlog
   task's outcome may still be valid, but its framing is wrong.
2. **Return to Backlog**: Update the task with the corrected assumption.
   This is a Backlog amendment, not scope expansion.
3. **Re-evaluate sufficiency**: The corrected Backlog task may still be
   sufficient, or it may need another iteration.
4. **Resume OpenSpec**: After the Backlog correction, continue OpenSpec
   refinement with the corrected foundation.

This is not a failure — it's the process working. Discovering wrong
assumptions during spec refinement is better than discovering them
during implementation.

## Grill invocation for planning

Planning iterations use a simplified Grill context — not the full 6-slot
invocation from grilling-integration.md. For a planning interview, pass:

```text
Question: <the specific gap or assumption being challenged>
Current draft: <link to or summary of the current Backlog/OpenSpec artifact>
Return condition: answer the question, then switch to facilitator mode
```

The full 6-slot context (permitted effects, upstream entrypoints, etc.)
is for bounded implementation-phase interviews where mutations may follow.
Planning interviews are read-only explorations — keep them lightweight.

## Role switching within iterations

The agent plays two distinct roles within each iteration:

**First half — Adversary (Grill interview):**
Challenge assumptions, surface gaps, stress-test claims. Follow the
Grill method's question discipline. Don't help — probe.

**Second half — Facilitator (refinement):**
"The interview surfaced these gaps. Let me help address them."
Use domain knowledge, propose options, help formulate. The user decides.

The switch is explicit — announce it. Don't blur the roles.

## Scope drift protection

After each iteration, compare the refined version with the previous:

- **Added detail within existing scope** → normal refinement, proceed.
- **New capability, actor, or requirement appeared** → this is scope
  expansion. Apply ownership.md rule 1: return to Backlog as a scope
  proposal. Don't sneak it in as "clarification."
- **Scope narrowed** → explicit decision. Record what was excluded and
  why. Don't silently drop requirements.

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
