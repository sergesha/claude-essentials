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
- Before lifecycle closures (close Beads issue, archive OpenSpec)
- As part of a status report (diagnostics + doctor together)

## Checks

### 1. Coverage (top-down)

Trace from Backlog down through each level:

```
For each Backlog acceptance criterion:
  → Has at least one OpenSpec requirement addressing it?
  → Each requirement has at least one Beads issue?
  → Each completed issue has Git/CI evidence?
```

**Gap** = a criterion, requirement, or issue lost during refinement.
Report: which item, at which level, what's missing below it.

### 2. Orphans (bottom-up)

Trace from Beads up through each level:

```
For each Beads issue:
  → Has a source reference to an OpenSpec requirement?
  → That requirement traces to a Backlog criterion?
```

**Orphan** = work without approved upstream intent. This is the primary
scope drift detector. Report: which item has no upstream trace.

### 3. Scope alignment

Compare the total scope at each level:

- Count Backlog acceptance criteria
- Count OpenSpec requirements
- Count Beads issues

A significant expansion (more issues than requirements, or more
requirements than criteria) may indicate scope drift. Report the
ratio and flag if issues exceed requirements by more than 2x.

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

For completed work, verify evidence exists:

- Closed Beads issues → Git commits or CI artifacts exist
- Archived OpenSpec → all requirements addressed in Beads
- Done Backlog tasks → all acceptance criteria passed

**False close** = lifecycle transition without supporting evidence.
Report: which item, what evidence is missing.

## Output format

```json
{
  "coverage": {
    "backlog_to_openspec": {"covered": 4, "total": 4, "gaps": []},
    "openspec_to_beads": {"covered": 3, "total": 4, "gaps": ["idempotency keys"]},
    "beads_to_evidence": {"covered": 2, "total": 3, "gaps": ["subscription mgmt"]}
  },
  "orphans": [
    {"level": "beads", "item": "#7 add caching", "reason": "no OpenSpec reference"}
  ],
  "scope_alignment": {
    "backlog_criteria": 4,
    "openspec_requirements": 5,
    "beads_issues": 7,
    "drift_signal": "beads issues exceed requirements — verify 2 extra are within scope"
  },
  "stale": [],
  "consistency": [],
  "evidence": {
    "false_closes": []
  },
  "summary": "1 coverage gap, 1 orphan, possible scope drift (7 issues vs 4 criteria)"
}
```

Render as a table, structured list, or JSON — adapt to context.
Include the summary line always.

## Severity levels

| Level | Meaning | Action |
| --- | --- | --- |
| **Gap** | Coverage hole — something approved is not being worked on | Surface to user for decision |
| **Orphan** | Work without approved intent — possible scope drift | Flag as scope proposal or justify trace |
| **Drift** | Scope expanded beyond Backlog approval | Return excess to Backlog as proposals |
| **Stale** | Forgotten work | Surface for re-prioritize or close |
| **Conflict** | Contradictory requirements or evidence | Stop affected work, resolve contradiction |
| **False close** | Lifecycle transition without evidence | Reopen or provide evidence |

## Doctor in autonomous flows

When doctor runs during an autonomous pipeline, classify each finding
using the delegation authority table from
[ownership.md](ownership.md#delegation-for-autonomous-flows):

| Finding | Authority level |
| --- | --- |
| **False close** (no evidence) | Mandatory stop — evidence integrity is non-negotiable |
| **Orphan** (no upstream trace) | Delegatable — subagent can evaluate if trace was missed or if it's real drift |
| **Coverage gap** (approved item not projected) | Mandatory stop — missing approved work requires human decision |
| **Scope drift signal** (ratio alert) | Delegatable — subagent can analyze whether extras are justified |
| **Stale item** | Automatic — report and continue |
| **Consistency conflict** | Mandatory stop — contradictions block affected work |

## Deep analysis (temporal)

When invoked manually (`doctor --deep`) or when the snapshot check
finds drift signals, analyze how the planning artifacts evolved over
time using git history.

### What to check

1. **Scope growth rate**: Count requirements/issues at each committed
   version. Plot the trend. Accelerating growth = progressive creep.

   ```
   v1: 3 requirements → v2: 4 → v3: 4 → v4: 6 → v5: 8
   Signal: growth accelerated at v4 — investigate what changed
   ```

2. **Intent drift**: Diff the current Backlog task against its first
   committed version. Has the core outcome shifted? A drift in the
   outcome itself (not just added detail) is a fundamental concern.

3. **Quiet additions**: Items added in commits without explicit scope
   amendment markers (`plan(backlog): scope amendment`). If a requirement
   appeared in a regular iteration commit without being flagged as
   scope expansion, it was snuck in.

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
