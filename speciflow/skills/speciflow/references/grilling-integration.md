# Grill integration

Grill (the `mattpocock/skills` family) is a supporting discipline — it
supplies interview, domain, research, and prototype methods. SpeciFlow
selects when to invoke Grill; Grill shapes the bounded activity; results
return to the appropriate owner.

For an unresolved change to approved parent meaning, grilling is mandatory
under [ownership.md](ownership.md#parent-contract-amendments). Its actual user
answer is a prerequisite to dependent mutation, not a recommendation that
review can waive. An exact settled user decision is reused. Ordinary technical
refinement and covered repairs do not acquire a new interview gate.

## Method selection

| Activity needs | Skill to invoke |
| --- | --- |
| Stress-test a plan, decision, or idea | `mattpocock-skills:grilling` |
| Challenge domain terms and scenarios against project language | `mattpocock-skills:domain-modeling` (+ `grilling` for interview) |
| Resolve a blocking factual question from primary sources | `mattpocock-skills:research` |
| Give the user a concrete artifact to judge | `mattpocock-skills:prototype` |
| Design module interfaces, seams, deep modules | `mattpocock-skills:codebase-design` |
| Guide human through manual steps (infra, credentials, UI) | `mattpocock-skills:wizard` |
| Edit skill files or agent-facing documents | `mattpocock-skills:writing-for-agents` |

On Codex, these are under `.agents/skills/` and invoked by local name
(`grilling`, `domain-modeling`, `codebase-design`, etc.).

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
If the question would change an approved parent, use the full context above
even during planning. If the user explicitly substitutes a fresh subagent as
respondent, apply the [bounded delegation and clean-context rule](ownership.md#delegation-for-autonomous-flows);
do not inherit the orchestrator's conclusions or infer authority from settings.

### Step 3: Execute the original skill

Invoke the resolved original `mattpocock-skills:grilling` through the host's
native skill interface and execute its complete bounded process. SpeciFlow
supplies scope and owner routing, not an interview algorithm. The loaded skill
governs question rounds, dependencies, recommendations, fact-finding delegation,
answer handling, and its completion/confirmation condition. Do not override
the method beyond the presentation adaptation below, paraphrase it into a
substitute interview, or stop after an answer while its completion condition
remains unmet.

**Presentation adaptation: one question at a time.** Ask one currently eligible
question, with the recommendation required by the original skill, and wait for
the respondent's answer. Then continue under the original method's decision
and dependency rules. This changes question delivery, not the interview's
coverage or completion condition: unasked questions remain unresolved, answers
may change which question comes next, and a fixed orchestrator-written script
cannot replace that process. Complete every branch of the bounded interview
before requesting its final shared-understanding confirmation. An answer to
one question does not authorize early return to owner mutation.

A method label, a source read, a generic approval question, and an orchestrator's
claim that it "grilled" someone are not execution evidence. Verify the actual
prescribed actions, responses, and completion against the loaded original
closure before any dependent owner amendment. Keep independently authorized
work moving while the interview's prerequisite or answer is pending.

An explicitly authorized subagent substitution changes the respondent, not the
method. Run the delegated interaction in fresh contexts under the original
skill. Recommendations required by that skill must arise in the clean interview
context, not be imported from the orchestrator's preferred answer. Do not invent
the respondent's answers or confirmation.

### Step 4: Return results

After the original skill's completion condition, including its required
confirmation of shared understanding from the authorized respondent, is met,
route results by type:

| Result type | Goes to | How |
| --- | --- | --- |
| Design decisions and rationale | OpenSpec | Preview spec update → review → approve → write |
| Product-scope proposals | Backlog | Keep proposed until the user decision covers the concrete amendment; then apply the existing mutation recipe |
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

## When the formal method is unavailable

Resolve the original source through the receiving host before choosing a fallback
(see [installation.md](installation.md)). A readable file and a host-discovered
method are separate checks.

| Requested activity | Action when the required original method cannot be loaded |
| --- | --- |
| User selected the original method, the full method set, prohibited substitution, or the parent amendment route requires grilling | Report the unavailable original skill and pause that dependent activity. Recover the required method; do not replace it with an orchestrator-made interview or a different method. A later explicit user change to the requested method is a new instruction, never an inferred fallback. |
| Ordinary clarification with no required original method | Use general interview principles and identify them as such. |

Continue already-authorized independent work. Completing that work does not
complete the unavailable-method activity. Reading a method establishes access;
its prescribed actions and results establish execution.
