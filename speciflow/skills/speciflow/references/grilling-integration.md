# Grill integration

Grill (the `mattpocock/skills` family) is a supporting discipline — it
supplies interview, domain, research, and prototype methods. SpeciFlow
selects when to invoke Grill; Grill shapes the bounded activity; results
return to the appropriate owner.

## Method selection

| Activity needs | Use this method |
| --- | --- |
| Stress-test a plan, decision, or idea | `grilling` |
| Challenge domain terms and scenarios against project language | `domain-modeling` (+ `grilling` for interview) |
| Resolve a blocking factual question from primary sources | `research` |
| Give the user a concrete artifact to judge | `prototype` |

Start by narrowing the question. Deepen when the user asks, when an
answer exposes dependent decisions, or when a missing fact prevents a
responsible decision. End when the bounded question is resolved.

## How to invoke

### Step 1: Resolve the method

1. Find the installed `mattpocock/skills` source through the host's
   native skill interface.
2. Read the resolved entrypoint and every linked file in its closure.
3. Hold the loaded closure stable for the bounded activity.
4. Record source and revision when available.

### Step 2: Set up context

For implementation-phase interviews (bounded question during execution):

```text
Selected activity and question:
Approved scope and authoritative source revisions:
Established decisions and unresolved prerequisites:
Permitted effects and existing authorization:
Applicable original upstream entrypoint and dependencies:
Return condition and destination owner:
```

For planning-phase interviews (iterative refinement): use the simplified
3-slot context from [iterative-planning.md](iterative-planning.md).

### Step 3: Run the interview

Invoke through the host's native skill interface. The method leads the
interview using its own question discipline. SpeciFlow provides the
bounded context; the method provides the practice.

### Step 4: Return results

When shared understanding is reached, route results by type:

| Result type | Goes to | How |
| --- | --- | --- |
| Design decisions and rationale | OpenSpec | Preview spec update → review → approve → write |
| Product-scope proposals | Backlog | Return as scope proposal for prioritization |
| Research findings and evidence | Authorized native artifacts | As evidence or inputs |
| Prototype artifacts | Product Git | Only with explicit approval |

For OpenSpec updates: present the exact spec change, apply the
cross-owner mutation recipe, then continue to the next SpeciFlow action.

## Composition with Superpowers

For design work: `superpowers:brainstorming` owns the design discipline,
Grill leads the interview. Each stays in its lane:

- Brainstorming structures the design process (approaches, trade-offs)
- Grill challenges assumptions and surfaces gaps (interview questions)
- Brainstorming presents options; Grill stress-tests them
- The user decides; results go to the appropriate owner

## User-only entrypoints

These are human-invoked only — SpeciFlow never calls them:

- `grill-me` — the user starts a grilling session
- `grill-with-docs` — grilling with document context
- `wayfinder` — multi-session decision navigation
- `setup-matt-pocock-skills` — installation

A prior direct invocation by the user continues within the same activity.
Reading an entrypoint's instructions is not the same as invoking it.

## When the method is unavailable

If the Grill skills are not installed, or the loaded closure is
incomplete or incompatible:

1. Report what's missing or incompatible.
2. Continue any unaffected owner-native work.
3. Offer installation through [installation.md](installation.md)
   as a separate action.
