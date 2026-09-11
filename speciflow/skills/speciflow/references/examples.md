# Worked examples

These show SpeciFlow responding to natural requests. The user never names
internal tools; SpeciFlow detects the SDLC phase and proposes actions.

## Example 1 — New feature request

**User:** "We need OAuth2 support in our API."

**SpeciFlow detects:** New capability → needs product scope (Backlog) and
requirements (OpenSpec). No execution work yet.

**Response:**

1. Check Backlog for an existing OAuth2 task → not found.
2. Propose: create a Backlog framing task capturing the outcome, scope, and
   acceptance criteria.
3. After approval, create the task through the Backlog CLI.
4. Propose: start an OpenSpec change linked to that task for requirements,
   threat model, and API design.
5. Wait for approval before each write.

**Key principle:** SpeciFlow selects owners by the concern, not by the user
naming a tool. "We need X" is product intent → Backlog. Requirements and
design → OpenSpec.

## Example 2 — Design is done, time to build

**User:** "The auth spec is approved. Let's break it into implementation tasks."

**SpeciFlow detects:** Cross-owner transition from spec (OpenSpec) to
execution (Beads). Intent preservation applies.

**Response:**

1. Read the approved OpenSpec change to identify requirements and tasks.
2. Verify each requirement has a clear acceptance criterion.
3. Present an item-by-item preview of proposed Beads issues with:
   - bounded work description
   - verification criteria
   - dependency edges
   - source reference back to the approved spec
4. Wait for explicit approval of the preview.
5. Create only approved items through the Beads CLI.
6. Report returned issue IDs.

**Key principle:** The transition preserves upstream intent — every approved
requirement must appear in the proposed issues. A gap stops the projection
and gets reported.

## Example 3 — Status check

**User:** "How's the auth work going?"

**SpeciFlow detects:** Status request → diagnostics. Determine which owners
are relevant from context.

**Response:**

1. Identify selected owners from the request scope — likely all three
   (Backlog, OpenSpec, Beads) since the user asks about an end-to-end feature.
2. Query each selected owner through its native interface:
   - Backlog: task status, priority, acceptance
   - OpenSpec: spec state, open changes, validation
   - Beads: issue status, blockers, dependencies, completion
3. Report per-owner results with evidence (roots, revisions, read times).
4. Derive next action from current state — advisory only, not authorization.

**Key principle:** Status is read-only and owner-separated. SpeciFlow never
aggregates into a single "percentage complete" or invents synthetic state.

## Example 4 — Stuck on a decision

**User:** "I'm not sure whether to use JWT or opaque tokens. Need to think this through."

**SpeciFlow detects:** Unresolved design decision → clarification needed.
This is where an interview method (grilling/domain-modeling) applies.

**Response:**

1. Identify the decision scope: token format is an API design choice owned
   by OpenSpec.
2. Invoke the applicable original grilling method through the host's native
   skill interface with bounded context: the approved scope, the specific
   question, and established constraints.
3. The interview explores trade-offs, challenges assumptions, and surfaces
   hidden requirements.
4. When shared understanding is reached, the decision and rationale return
   to the current OpenSpec context.
5. SpeciFlow selects the next owner action (update the spec, proceed to
   decomposition, etc.).

**Key principle:** The interview method supplies the discipline; SpeciFlow
retains cross-owner selection. The decision belongs to OpenSpec, not to the
interview tool.

## Example 5 — Scope discipline (what NOT to do)

**During code review, the reviewer says:** "This auth flow should also
support SAML SSO and add rate limiting to the token endpoint."

**Wrong response (scope creep):** Create tasks for SAML and rate limiting,
add them to the spec, start implementation planning.

**Correct SpeciFlow response:**

1. Recognize these are **new capabilities** not in the approved Backlog scope.
2. Return them as **non-blocking scope proposals** to Backlog.
3. Assess whether each proposal **intersects** with current work:

| Proposal | Intersection | Action |
| --- | --- | --- |
| SAML SSO | None — independent auth flow | Continue current work |
| Rate limiting on token endpoint | Potential — may require reworking the endpoint we are building now | Flag conflict to user: continue or pause this part until scope decision? |

4. Report: "Review raised two scope proposals. SAML SSO is independent —
   current work continues. Rate limiting intersects with the token endpoint
   we are building — continue as-is, or pause that part for a scope decision?"

**Key principle:** Review can flag defects against approved scope. Anything
that adds new scope is a proposal that goes through Backlog approval. When
a proposal intersects with current work, surface the conflict to the user
rather than choosing to continue or pause unilaterally. Non-intersecting
proposals never block current work.

## Example 6 — Mid-execution spec amendment

**Mid-implementation, the user says:** "Actually, make the API async
instead of synchronous. The approved spec says sync but I changed my mind."

**SpeciFlow response:**

1. Surface the contradiction: "The approved spec explicitly says synchronous
   processing. You're asking for async — these conflict."
2. Assess impact: "Issues #1-#2 are complete with sync implementation.
   Switching to async requires reworking both, plus the in-progress issue #3.
   Compound cost: this is a deeper redesign than changing one endpoint."
3. Route through spec: "I'll preview the spec amendment for your approval
   before changing any implementation."
4. Present: spec diff showing sync→async change, affected requirements,
   rework scope.
5. Wait for explicit approval of the spec amendment.
6. Only after approval: update implementation to match new spec.

**Key principle:** The user is the authority, but "just do it" is the exact
moment the process is most valuable. The spec amendment protects against
cascading untracked changes. Always route through the spec, even when the
user who approved the original is the one changing it.

## Example 7 — Delegated decision in autonomous flow

**Setup:** The user pre-authorized: "For non-security scope proposals,
a subagent can evaluate intersection and recommend continue/pause."

**During autonomous execution, a review raises:** "The token endpoint
should also validate PKCE challenge."

**SpeciFlow delegates to a clean-context subagent:**

The subagent receives: approved OAuth2 scope, the current token endpoint
spec, and the PKCE proposal. No current implementation details (clean
context avoids sunk-cost bias).

The subagent evaluates:
- PKCE is a security-adjacent concern → check mandatory stop list
- PKCE doesn't add a new security boundary, it strengthens an approved
  one → delegatable
- PKCE directly affects the token endpoint under construction → intersects

**Subagent returns:** "Intersecting proposal. PKCE affects the approved
token endpoint. Recommend pause on token endpoint work until scope decision."

**SpeciFlow acts:** Pauses the intersecting portion, continues independent
work, queues the scope proposal for human review at next checkpoint.

**Contrast — if the proposal were "add admin dashboard":** No intersection
with OAuth2 work. Subagent returns "non-intersecting, continue." Current
work proceeds, proposal logged for Backlog.

## Example 8 — Iterative planning from a rough idea

**User:** "I want to add a recommendation engine to our e-commerce platform.
Not sure exactly what that means yet — let's figure it out."

**SpeciFlow detects:** Rough idea, needs refinement → iterative planning.

**Iteration 1 (Backlog — broad strokes):**

Agent captures: "Add product recommendations to improve conversion."

Grill interview (adversary mode):
- "What kind of recommendations? Similar products? Frequently bought
  together? Personalized?"
- "Where do they appear? Product page? Cart? Email?"
- "What data do you have? Purchase history? Browse behavior? Ratings?"

User answers: "Personalized, on the product page, we have purchase history."

Agent switches to facilitator: refines Backlog task with these specifics.
Commit: `plan(backlog): iteration 1 — personalized product-page recs from purchase history`

Sufficiency check: Outcome ✓, Scope boundaries ✗ (no exclusions yet),
Acceptance ✗, Unknowns ✗ → next iteration.

**Iteration 2 (Backlog — filling gaps):**

Grill interview:
- "What's NOT included? Email recs? Cart cross-sell? Homepage?"
- "How do you measure success? CTR? Conversion lift? Revenue per session?"
- "What about cold-start users with no purchase history?"

User answers: "Only product page for now. Measure by CTR. Cold-start shows
trending items."

Agent refines with exclusions and acceptance criteria.
Commit: `plan(backlog): iteration 2 — scope boundaries and success metrics`

Sufficiency check: All four criteria met → transition to OpenSpec.

**Phase 2 (OpenSpec — specification):**

Grill interview stress-tests requirements:
- "You said personalized from purchase history — collaborative filtering or
  content-based? Each has different data requirements."
- "What's the latency budget? Recommendations computed on request or
  precomputed?"
- "How many recommendations per page? What if the model has low confidence?"

Agent helps formalize into testable requirements. Commit each iteration.
When spec is complete → approve → project to Beads.

**Key principle:** The user started with "I want recommendations" and through
2 Backlog iterations + OpenSpec iterations arrived at a specific, testable
spec. Each version is committed. Grill drove the exploration at every stage.
