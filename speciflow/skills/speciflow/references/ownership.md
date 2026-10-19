# Ownership

Each concern has one authoritative owner:

| Owner | Sole authority |
| --- | --- |
| Backlog.md | What and why: product outcomes, high-level requirements, DoD, NFRs, constraints, and priority |
| OpenSpec | How: technical solution, architecture, derived testable requirements, design, and specification plan |
| Beads/Dolt | Atomic implementation tasks derived from OpenSpec; executable dependencies, readiness, claim, blockers, and completion |
| Superpowers | TDD, debugging, verification, review, and implementation discipline |
| Grill | Interviews, domain modeling, research, and prototyping discipline |
| Product Git and CI | Source changes, review history, dirty state, and implementation evidence |

Product Git and CI own implementation evidence; code evidence is never Beads
status or OpenSpec design approval.

Superpowers and Grill are supporting disciplines. They shape how a selected
activity is explored, implemented, or verified but own none of the product,
design, execution, or source state above. Both install and apply through the
host's native skill interface — same pattern, same boundaries.

## Native artifacts and granularity

Select owners before checking their availability:

- Backlog, OpenSpec, and Beads are selected by an explicit request for that
  owner or by the concern it owns within the applicable approved project work.
- Superpowers is selected when explicitly requested or when a native skill
  trigger applies to the bounded activity, including read-only verification.
  Check that applicable skill closure even when no project task belongs to it.
- Grill is selected when the activity involves clarification, a design
  interview, domain modeling, research, or prototyping.

Installed CLIs, absent native roots, and the four-tool list do not by themselves
select owners. Diagnostics report unselected owners as `N/A`; a missing required
interface makes a selected owner `broken`. If involvement is unclear from the
requested work, clarify that scope rather than selecting all four by default.

Use selected tools to the extent the work needs:

| Concern | Native representation |
| --- | --- |
| Committed product initiative | A Backlog task expressing outcomes, scope, high-level requirements, DoD, NFRs, constraints, and priority. No implementation recipe. |
| Specification and technical refinement | An OpenSpec change describing the technical solution, architecture, interfaces, derived requirements, scenarios, design, and authorized design-time plan/checklist. |
| Concrete implementation | Beads issues decomposing that solution into independently verifiable results, with executable dependencies and references to the approved OpenSpec source. |
| Multi-session Wayfinder work | A Beads parent/index and bounded child work linked to Backlog scope and current OpenSpec questions; semantic questions, answers, and rationale remain in OpenSpec. |

When Backlog is selected for a committed initiative, search for and reuse its
framing task; if missing, create it through the native CLI under the applicable
approval. A brief supports that task and does not replace it. Product subtasks
are useful for separately accepted outcomes, not copies of engineering steps.
Use the installed task types and statuses; no custom epic type is required.
For a question or mechanical edit, do not manufacture a three-tool hierarchy.

Link the OpenSpec change to its Backlog task using native references or an
artifact path and revision. Decompose by need, not one-to-one: a framing task
may have several changes, and a specification task several implementation
issues. Links carry provenance; the source owner keeps authority.

Backlog never dictates the implementation: classes, algorithms, call sequences,
batch sizes, and engineering steps belong in OpenSpec. Allowed change locations
or architectural patterns may constrain the solution space without prescribing
the solution. Missing implementation detail in Backlog is intentional, not a gap
requiring a user decision. OpenSpec chooses and explains the solution within
those boundaries, keeping technical contracts and scenarios in specifications
and implementation details in its design/plan artifacts. Beads decomposes that
approved solution; an atomic task has one independently verifiable result, not
a fixed file count or a copied specification checklist.

A native tool's optional plan field or default workflow does not change this
allocation. Use a reference to OpenSpec where needed, not a second plan in
Backlog. Do not silently remove or relocate an existing approved constraint
because it was recorded at the wrong level; apply the amendment route below
when its meaning would change.

An external prerequisite does not erase the product commitment. Retain its
reference and entry condition in the framing task using native fields or
description. Use an existing appropriate Backlog status; when none expresses
the condition, keep the task open and explain it in the description. This is
product context, not a second executable blocker graph. If the user excludes
upstream work from project scope, do not create an upstream task or Bead.

Inspect what the user's restriction covers: creating implementation issues,
authoring specification tasks, and starting work are distinct actions. A gate
on one does not imply a gate on all. Retain unmet product intent while
respecting each explicit restriction; never infer projection approval.

Native tool edits remain authoritative. Never copy owner content into a SpeciFlow schema.
Use native IDs or typed references when available; otherwise use an artifact
path plus commit SHA, or the current user's approval of a dirty snapshot.
Missing stable links are manual confirmed operations, not SpeciFlow IDs.

### Organizing Backlog work

For mixed-topic work, choose relevant native fields using the installed
Backlog instructions and existing project conventions:

| Need | Native Backlog field |
| --- | --- |
| Recognizable topic and intended outcome | Descriptive task title |
| Durable product or workstream grouping | Configured project |
| Topics and other cross-cutting facets | Labels |
| A finite delivery outcome or stage shared by tasks | Milestone |
| A separately accepted part of a larger product outcome | Parent task, following the granularity above |

Use the fields that serve the request and are supported and configured;
a simple backlog may need only descriptive titles and labels. Product grouping
stays in Backlog; executable dependencies stay with their selected owner.
For topic queries, use the corresponding native filters and views. Include the
title or topic with an ID when a bare ID would obscure which work is meant,
for example `TASK-3 — SpeciFlow: improve procedure reliability`.

## Approved refinement

Every cross-owner result must remain a refinement of the applicable approved
upstream intent. Provenance identifies the source but does not prove complete
coverage. The downstream owner may add detail within its authority, but may not
narrow an outcome, discard required work at a blocker or intermediate
milestone, or conceal an unresolved dependency.

An exclusion, deferral, substitution, or new obligation requires the decision
defined by [parent contract amendments](#parent-contract-amendments), recorded
with the upstream owner. Validation, review, downstream acceptance, and
implementation evidence do not supply that decision. Use the ephemeral comparison in
[transitions.md](transitions.md); never persist a coverage table or SpeciFlow
traceability record.

Definitions, design answers, and rationale produced during an interview belong
to the current OpenSpec context. Existing glossaries, context documents, ADRs,
and code are project sources whose documented role must be read and respected;
copying or updating one does not automatically make it canonical. Agreement on
meaning, semantic review, and authorization for an exact mutation remain
separate decisions.

In Wayfinder, Beads owns the map relationship, dependency, readiness, claim,
and completion of bounded decision work. Its native assignee identifies the
claimant, not the semantic owner. A map and its resolution comments are indexes
to owner results; neither may become a second product scope or design-answer
store. Apply [wayfinding.md](wayfinding.md) after explicit human invocation.

## Review authority and scope discipline

Backlog.md is the sole scope authority. These rules apply at every level
and stage:

**1. Review findings have three categories:**
- **Implementation defect** (code does not match approved behavior) → repair
  within the current assignment under rule 4.
- **Spec gap** (a demonstrated conflict prevents an approved outcome) →
  resolve the conflict: correct the implementation, reconsider the mechanism,
  clarify the requirement, or use the parent contract amendment route below.
- **Enhancement** (capability beyond approved goals) → non-blocking scope
  proposal to Backlog.

For a claimed gap, identify the approved outcome, failing scenario, and why
existing behavior or a narrower mechanism cannot satisfy it. Investigate that
necessity before adopting the proposed solution. A dependency of the agent's
chosen mechanism is not automatically a dependency of the approved outcome.
Prior implementation effort and review agreement establish neither necessity
nor approval. A gap requires resolution, not necessarily a spec amendment.
A specific, demonstrated safety or security risk may stop affected work;
a reviewer cannot assign new scope or override an agreed exclusion.

**2. Every action traces to applicable approved intent.** Inspect the
user's goal, later decisions, approved linked sources, and bounded delegation
using the approval boundary below. When authority is missing, withhold only
the dependent action and obtain the missing decision; continue independent
covered work. Discarding an unnecessary agent proposal and returning to the
approved task does not require permission to expand scope.

**3. Implementation detail vs scope expansion.** Two tests must pass
for a choice to be automatic:
- Does it serve an approved requirement rather than a future one?
- Does it preserve approved installation, operation, compatibility, and
  security requirements?

A necessary stronger internal mechanism can pass both tests. A required
service, runtime, account, setup step, or new public contract signals effects
to inspect against current authority, not an automatic technical ban.
Adding a field, endpoint, or capability for unapproved future needs is scope
expansion. Route uncovered effects before implementing them.

**4. Repair defects within the current assignment.** This includes defects
in the assigned work, regressions it introduced, and pre-existing defects whose
repair is necessary for its approved outcome or required verification. An
explicit broader repair grant, such as fixing all repository test failures,
also applies. Preserve existing behavior and verify affected paths.

An unrelated pre-existing defect does not join the assignment merely because
a full-suite run or reviewer found it. Separate any drafted repair from the
current change and retain that patch with the failure evidence as an unadopted
proposal. Report the failure and its provenance honestly; continue independent
verification. A gate requiring a green suite remains unmet; running that suite
alone does not authorize unrelated repairs. Conversely, calling a necessary
repair “known-flaky” or filing it away does not satisfy the assignment.

**5. Resolve genuine intersections, without promoting agent proposals.**
When a proposal exposes an unresolved choice that affects approved work,
explain the intersection and obtain the authorized decision before dependent
work. An unnecessary uncovered addition can be rejected outright. Neither it
nor a non-intersecting proposal blocks independent approved work.

## Parent contract amendments

This is the single route for a newly discovered decision that would change an
approved parent: OpenSpec cannot expand, narrow, or contradict Backlog; Beads
and implementation cannot change the approved OpenSpec contract. It applies
even when a proposed spec amendment preserves Backlog's high-level outcome.

1. **Compare meaning** against actual approved parents and later user decisions.
   Distinguish derived detail from a change to an outcome, requirement, DoD,
   NFR, constraint, exclusion, or approved technical contract.
2. **Find an in-contract solution first.** Repair a defect, investigate a
   narrower mechanism, or reject an unnecessary reviewer addition under the
   current grant. Initial technical design and testable derived requirements
   within Backlog are OpenSpec work, not new product requirements. Ordinary
   repairs, debugging, factual investigation, and such authorized refinement
   require no new interview.
3. **If a parent semantic change is necessary and unresolved, invoke
   the original `mattpocock-skills:grilling` with the user and execute its full
   process through the actual answers and required completion confirmation.**
   Apply [grilling-integration.md](grilling-integration.md). Present the conflict,
   evidence, compliant alternatives considered, and proposed effects. A generic
   approval request, reviewer PASS, planning commit, or failed test is not this
   decision process. Keep affected owner mutations and dependent implementation
   pending; continue independently authorized work. If the required method is
   unavailable, pause this branch under its availability rule, not a silent
   substitute interview.
4. **Reuse a settled decision.** An exact applicable prior user answer or an
   explicit user instruction settling the same concrete question need not be
   asked again. Generic permission to implement/review is not such an answer.
   Apply only its covered effects through the existing mutation recipe and
   review, updating the highest affected owner first and then its dependants.
   Record decisions at their owner; links, not a new authority ledger.

Review may discover and propose an amendment, never adopt it by rewriting a
parent to make a child pass. Calling an amendment “clarification”, “corrected
assumption”, or “not scope expansion” does not bypass this route. Neither can
moving implementation instructions out of Backlog silently weaken an approved
constraint. Review each level for its own responsibility:

| Reviewed level | Question |
| --- | --- |
| Backlog | Is the outcome meaningful, architecturally appropriate and internally coherent, with high-level requirements and constraints rather than a solution recipe? |
| OpenSpec | Does this technical solution and its testable contract satisfy the Backlog task completely, without new product requirements or contradictions? |
| Beads | Do these atomic tasks implement the approved specification, with verifiable results and no new specification or product obligations? |

## Delegation for autonomous flows

Implementation and read-only review can be delegated within their existing
grant. They confer no authority to answer a user decision or amend a parent.

Replacing user grilling with subagent grilling requires a **direct, clear user
instruction explicitly authorizing that substitution for a bounded question**.
Configuration, an autonomy setting, “use subagents”, and implementation/review
delegation do not qualify. Do not infer the exception from the user's desire
for speed or uninterrupted work. An explicit substitution for a named parent
amendment authorizes resolving that amendment, not merely recommending options
that preserve the old parent. Apply its covered result at the highest affected
owner before dependent changes; do not demand a second human answer solely
because the named decision changes parent semantics. The human-only categories
below still apply to their actual effects.

For that explicit exception, the interviewing agent and substitute respondent
use fresh contexts: original user inputs, later
explicit decisions, approved parent snapshots, the exact delegation grant,
unresolved question, and neutral facts. Exclude the orchestrator's conclusions,
preferred answer, implementation rationalizations, and inherited session
narrative. Apply the original skill with only SpeciFlow's documented
one-question-at-a-time presentation adaptation; do not substitute the
coordinator's interview algorithm. The subagent is the substitute respondent for
that bounded process, not a reviewer whose PASS approves scope. Its answer cannot extend the
grant; uncovered questions return to the user. Owner writes still use the
existing mutation recipe.

### Mandatory stop — no exceptions

These always require human decision regardless of delegation settings:

1. Adding, removing, or materially changing the behavior of a security
   boundary (auth method, API exposure, encryption, access control —
   including changes that could break existing clients or workflows)
2. Irreversible state changes (delete, archive, close, claim release)
3. Changing who defines scope (Backlog authority transfer, project
   ownership change)
4. Budget, infrastructure, or contractual commitments
5. Overriding a previous explicit human decision outside the precise question
   the user has explicitly authorized the substitute respondent to reconsider

A subagent may recommend but cannot authorize these actions. Classify them by
actual effects, not vocabulary: an ordinary product or quality-control rule is
not an authentication/access-control boundary merely because it uses words
such as “identity”, “verification”, or “approval”. A parent requirement changing
under a scoped substitution is not itself a transfer of scope ownership.

## Pushback obligation

User authority to decide and the agent's obligation to inform are not in
conflict. Agreeing with a harmful decision is not respect — it is failure.

When the user requests something that contradicts approved scope, removes
a safety requirement, or skips a required step:

1. **Consequence first**: State the specific harm — what breaks, what risk
   increases, what compliance is lost. Not "I can't" but "here's what
   happens."
2. **Constructive alternative**: Offer a path that respects the user's
   urgency while maintaining safety. "If encryption is blocking you, I
   can implement the export with encryption using a simpler approach."
3. **Explicit decision**: Make it clear the user is choosing between
   options with known consequences, not just confirming a suggestion.

The user may still override after seeing consequences — that is their
authority. But the agent must ensure the decision is informed, not
reflexive.

## Mid-execution spec amendments

When the user contradicts their own approved spec during execution:

1. **Surface the contradiction explicitly**: "The approved spec says X,
   you're now asking for Y. These conflict."
2. **Assess impact on completed work**: Does the change require rework
   on already-completed issues? State the cost.
3. **Resolve at the highest affected owner**: Apply
   [parent contract amendments](#parent-contract-amendments). Reuse a concrete
   decision the user has already settled; grill only unresolved semantic
   choices. Update Backlog first if product meaning changes, then OpenSpec and
   dependent tasks through the existing preview, review, and mutation gates.
4. **Batch when possible**: Multiple related changes can be previewed
   together as one spec amendment. Unrelated changes are separate.
   When changes arrive in rapid succession, collect related ones before
   responding — a single compound preview is clearer than per-message
   responses.
5. **Assess compound impact**: When multiple amendments combine, the total
   rework may exceed the sum of individual impacts. Present the compound
   cost, not just each change in isolation.
6. **Distinguish origin**: Four cases:
   - "I changed my mind" → spec amendment (route through OpenSpec update)
   - "I forgot what I approved" → reminder (continue as approved)
   - Agent implemented something different from approved spec → **agent
     error** — fix the code to match the spec, not the spec to match the
     code. If the deviation was motivated by a real concern, raise it
     through pushback obligation as a forward-looking proposal.
   - Agent interpreted ambiguous spec differently from user's intent →
     **spec clarification** — the spec was underspecified. Whether
     the agent should have flagged the ambiguity before implementing
     depends on how material and detectable it was: a subtle edge
     case is a spec quality signal; an obvious fork in meaning that
     the agent silently resolved is an agent error in judgment.
     Either way: update the spec to resolve the ambiguity (via
     OpenSpec amendment), then align implementation.

"Just do it, don't make me go through the process" is the exact moment
the process is most valuable — rapid untracked changes create the
inconsistencies the process prevents.

## Approval boundary

When resolving an authorization claim, preparing an owner mutation, or
dispatching work, include these three parts in the existing preview or
next-action explanation:

1. **Source**: Identify the actual user instruction or bounded delegated
   decision and applicable approved snapshot, actually read now or reused
   unchanged from context. An available but unread source remains pending.
   For a disputed artifact-based claim, inspect the underlying
   user decisions and linked approval evidence. If that inspection is still
   pending, make the concrete source read a next operation and keep its
   authorization conclusion pending.
2. **Effects**: Match each proposed effect to what that source authorizes.
   Distinguish approved content, implementation, native record creation,
   and lifecycle transitions; state which effects are covered and which
   await a decision or inspection. An implementation or ordinary
   execution-record grant does not by itself authorize closure, archive,
   deletion, or claim release. Apply existing human-only decision
   requirements to those effects, reusing a matching human grant rather
   than requesting it again. An instruction to execute approved work
   through selected owners can cover necessary execution records under the
   [cross-owner mutation recipe](operations.md#cross-owner-mutation-recipe).
3. **Next action**: Execute or dispatch covered ready work and inspect or
   obtain only the missing authority for its dependent effects. Keep proposed
   uncovered writes as read-only previews. Continue independently covered
   implementation while a separate record-creation or scope decision is pending.

Use the current conversation and native evidence; reuse unchanged reads and
grants. These parts complete the existing ephemeral preview, not a new stored
record or approval round. Inspect current native state before its mutation.

OpenSpec proposal or projection requires current explicit user approval by
default. A durable approval is valid only through a documented native signal,
or a user-named attestation that the user confirms authorizes projection.
Inspect the actual authorization and its covered snapshot: a later explicit
user decision, an identified approved linked plan, or a decision within a
user-granted delegation can supply authority. Preserve that authority even
when a short summary omits its detail. A file, plan, commit, review verdict,
or execution reference alone does not supply approval.

When Beads is selected, OpenSpec `tasks.md` is a design-time checklist and
projection index, not live executable status, assignment, claim, or completion;
Beads owns those facts. Without Beads, OpenSpec may use its native task
workflow. A requirements, contract, security, or material architecture change
uses [parent contract amendments](#parent-contract-amendments); only affected
execution pauses until its actual prerequisites are resolved.

Closure is separate by owner: Beads completion, OpenSpec verification or
archive, Backlog outcome acceptance, and product Git/CI evidence are separate
facts. Ask closure questions only for selected owners; an absent owner is N/A.
Backlog task status reflects product acceptance, not mirrored Beads progress.

## Supporting discipline coordination

SpeciFlow selects the cross-tool action. Before the bounded selected activity:

1. Apply Superpowers skills by their native triggers (TDD, review, verification).
2. Apply Grill methods when the activity involves clarification, domain
   modeling, research, or prototyping.
3. Each discipline shapes the activity, then returns control to SpeciFlow.

SpeciFlow retains cross-owner selection. Supporting disciplines select
methods and make rulings within the bounded assignment and applicable
approval; their autonomy, review verdicts, and local ledgers confer no new
scope authority. Return uncovered semantic additions as proposals before
owner mutation or downstream dispatch, while continuing covered work.
A discipline's task-review gate governs that task and its dependants. Correct
and re-review the affected task while independently ready, authorized work
continues; its dispatch does not wait for that unrelated review gate.

### Discipline handoff

When one discipline discovers it needs another mid-activity:

1. The active discipline pauses the dependent activity (preserve work-in-progress).
2. SpeciFlow takes control and selects the needed discipline.
3. That discipline completes and returns results to the appropriate owner.
4. SpeciFlow checks authority and necessity. If the result requires a real
   design or specification change, apply the parent contract amendment route,
   then its existing review and mutation gates. A finding that confirms the
   approved design is evidence, not automatically a required owner change.
5. The original discipline resumes when its actual prerequisite is resolved.
   Do not make recording a confirming finding a new prerequisite to already
   authorized implementation. Include the finding in its working context;
   route any nonblocking evidence update separately.

## What SpeciFlow owns

SpeciFlow owns coordination — selecting which owner to use and when. All
domain state lives in its native owner:

| State | Lives in |
| --- | --- |
| Product intent, priority, acceptance | Backlog.md |
| Requirements, design, specification | OpenSpec |
| Executable work, dependencies, completion | Beads/Dolt |
| Source code, review history | Product Git/CI |
| Implementation discipline | Superpowers |
| Interview and research discipline | Grill |
