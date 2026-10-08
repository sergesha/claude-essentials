# Doctor — cross-level consistency check

Doctor verifies that the planning and execution system is consistent
from top (Backlog intent) to bottom (implementation evidence). It is
read-only — it reports findings but never mutates owner state.

## When to invoke

**Manual:** The user asks for a consistency check, health report, or
"is everything aligned?"

**Automatic (at milestone points):**
- Before sufficiency/completeness checks in iterative planning
- Before cross-owner transitions (OpenSpec → Beads projection)
- Before **epic-level** closures (archive full OpenSpec spec, mark
  Backlog task Done) — NOT for individual Beads issue close
- As part of a full status report (diagnostics + doctor together)

**NOT triggered by:** closing a single Beads issue with passing code
review and evidence. Individual task closure uses evidence verification
only — full cross-level doctor runs at epic/milestone boundaries.

## Checks

### 1. Coverage (top-down)

Identify the selected stage and owner transition before evaluating coverage:

| Checkpoint | Coverage evidence |
| --- | --- |
| Product/specification refinement | Applicable intent is retained in the proposed refinement, including bounded unknowns and prerequisites. Future issues or implementation evidence need not exist. |
| Before executable projection | The proposed issue set retains applicable approved intent and dependencies. Empty current Beads state is expected for a first projection. |
| After projection / during execution | Actual native issues retain the authorized projected scope; deferred parts retain their approved boundary and resumption condition. |
| Acceptance or closure | Actual completed outcomes and current verification satisfy the selected owner's closure criteria. A ready slice cannot establish whole-scope completion. |

A **gap** is approved intent lost, weakened or prematurely terminated at the
selected boundary. Approval of an incomplete list is not approval to discard an
upstream requirement. Report the exact unmatched intent and stop the affected
transition. Retained pending/deferred scope is not lost scope and does not block
independent authorized ready work. Doctor remains read-only.

### 2. Orphans (bottom-up)

Trace from Beads up through each level:

```
For each Beads issue:
  → Has a source reference to an OpenSpec requirement?
  → That requirement traces to a Backlog criterion?
```

An untraceable item requires checking its semantics against approved intent.
A missing link leaves provenance unconfirmed; demonstrated work outside approved
intent is an **orphan**. Report the missing trace or actual unmatched scope.

### 3. Scope alignment

Check for semantic drift, not just counts:

- Do any Beads issues introduce **new user-facing capabilities** not
  in OpenSpec?
- Do any OpenSpec requirements add **new user obligations** (runtime
  dependencies, manual steps, services) not in Backlog?
- Are there **new exclusions** that appeared without explicit approval?

Report ratios as context (e.g., "7 issues for 3 requirements") but do
not flag ratio alone as drift. Good decomposition naturally fans out.
A drift finding requires a concrete capability, obligation or exclusion outside
approved intent. A missing reference alone leaves traceability unconfirmed.

### 4. Stale items

Open work without progress:

- Open Beads issues with no activity (commits, comments) in 7+ days
- Draft OpenSpec changes with no updates in 7+ days
- In-progress Backlog tasks with no downstream activity

Report: which items, how long stale.

### 5. Consistency

Check for contradictions within and across levels:

- OpenSpec requirements that contradict each other
- Beads issues with conflicting verification criteria
- Completed work that contradicts an open requirement

Report: which items conflict, what the contradiction is.

### 6. Evidence integrity

Use [operations.md](operations.md#evidence-at-existing-gates) to check the
applicable evidence against the current activity/artifact. Closed Beads issues,
archived OpenSpec changes and accepted Backlog outcomes each require their own
supporting evidence; one owner's state does not prove another's completion.

Unavailable evidence means **unconfirmed**, not proof that a step was skipped.
An observed transition contrary to its required gate is a **false close**; cite
the actual action and failed prerequisite. Conflicting records need resolution.
None passes a dependent closure check. Reopening is a separate native lifecycle
action under applicable authorization, not an automatic response to unreadable logs.

## Output format

```json
{
  "coverage": {
    "backlog_to_openspec": {"covered": 4, "total": 4, "gaps": []},
    "openspec_to_beads": {"covered": 3, "total": 4, "gaps": ["idempotency keys"]},
    "beads_to_evidence": {"covered": 2, "total": 3, "gaps": ["subscription mgmt"]}
  },
  "unconfirmed_provenance": [
    {"level": "beads", "item": "#7 add caching", "reason": "OpenSpec reference not found; semantic check pending"}
  ],
  "scope_alignment": {
    "backlog_criteria": 4,
    "openspec_requirements": 5,
    "beads_issues": 7,
    "new_capabilities": [],
    "new_user_obligations": [],
    "note": "Counts are context only; inspect the reported missing trace before judging scope."
  },
  "stale": [],
  "consistency": [],
  "evidence": {
    "false_closes": []
  },
  "summary": "2 coverage gaps; 1 item has unconfirmed provenance. Counts establish no scope drift."
}
```

Render as a table, structured list, or JSON — adapt to context.
Include the summary line always.

## Severity levels

| Level | Meaning | Action |
| --- | --- | --- |
| **Gap** | Approved intent lost at the selected boundary | Stop affected transition; identify unmatched intent |
| **Orphan** | Work without approved intent — possible scope drift | Flag as scope proposal or justify trace |
| **Drift** | Scope expanded beyond Backlog approval | Return excess to Backlog as proposals |
| **Stale** | Forgotten work | Surface for re-prioritize or close |
| **Conflict** | Contradictory requirements or evidence | Stop affected work, resolve contradiction |
| **Unconfirmed** | Required evidence cannot be verified | Obtain evidence; dependent gate remains pending |
| **False close** | Observed transition contrary to a required gate | Report observed violation; use authorized native correction |

## Doctor in autonomous flows

When doctor runs during an autonomous pipeline, classify each finding
using the delegation authority table from
[ownership.md](ownership.md#delegation-for-autonomous-flows):

| Finding | Authority level |
| --- | --- |
| **Unconfirmed evidence / observed false close** | Stop dependent acceptance; obtain evidence or correct the observed violation under applicable authorization |
| **Unconfirmed provenance / demonstrated orphan** | Delegatable — establish the semantic trace or identify concrete work outside approved intent |
| **Coverage gap at selected boundary** | Stop affected transition; retain independent authorized work |
| **Possible semantic drift** | Delegatable — compare the concrete change with approved intent; counts alone are not a finding |
| **Stale item** | Automatic — report and continue |
| **Consistency conflict** | Mandatory stop — contradictions block affected work |

## Deep analysis (temporal)

When invoked manually (`doctor --deep`) or when the snapshot check
finds drift signals, analyze how the planning artifacts evolved over
time using git history.

### What to check

1. **Scope growth rate**: Count requirements/issues at each committed
   version. Use the trend as navigation context for the semantic diff; growth
   alone does not establish scope creep.

   ```
   v1: 3 requirements → v2: 4 → v3: 4 → v4: 6 → v5: 8
   Signal: growth accelerated at v4 — investigate what changed
   ```

2. **Intent drift**: Diff the current Backlog task against its first
   committed version. Has the core outcome shifted? A drift in the
   outcome itself (not just added detail) is a fundamental concern.

3. **Quiet additions**: Items added in commits without explicit scope
   amendment markers (`plan(backlog): scope amendment`). Inspect their meaning
   and actual approval; an absent commit-message marker proves neither hidden
   scope expansion nor missing authorization.

4. **Requirement inflation**: Are individual requirements growing more
   complex? Compare word count per requirement across versions. A
   requirement that doubled in size may have absorbed scope from
   elsewhere.

5. **Oscillation**: Items added → removed → re-added across versions
   signal an unresolved decision being deferred rather than made.

### Output

```json
{
  "temporal": {
    "versions_analyzed": 5,
    "scope_growth": {"v1": 3, "v2": 4, "v3": 4, "v4": 6, "v5": 8},
    "growth_pattern": "accelerating after v4",
    "intent_drift": "outcome unchanged — still 'invoice processing'",
    "quiet_additions": ["email notifications (v3)", "admin dashboard (v5)"],
    "inflated_requirements": ["data extraction: 12 words → 47 words"],
    "oscillations": []
  }
}
```

### When NOT to run deep analysis

- Single-iteration projects (no history to analyze)
- Bug fixes and mechanical changes
- When snapshot doctor found no drift signals

Deep analysis is expensive — run it when there's reason to suspect
gradual drift, not as a routine check.

## What doctor does NOT do

- Does not create, close, or modify any owner artifact
- Does not resolve conflicts (reports them for user decision)
- Does not define scope (Backlog is the sole scope authority)
- Does not aggregate into a single "health percentage"
- Does not invent traceability records or coverage tables that persist
