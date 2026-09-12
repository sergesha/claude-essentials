# Wayfinder binding

Apply this reference only after the human explicitly invokes the
`wayfinder` entrypoint from `mattpocock/skills`. SpeciFlow binds
Wayfinder to native owners — it does not invoke, simulate, or replace
Wayfinder itself.

## Setup

1. Resolve the original `wayfinder` source and read its full closure
   (same process as [grilling-integration.md](grilling-integration.md)
   step 1).
2. Pass the context below through Wayfinder's `Other` tracker extension.
3. The original method controls wayfinding; this reference only binds
   owners and operations.

### Context to pass

```text
Wayfinding tracker: verified Beads/Dolt root + installed map/child/
  dependency/readiness/claim/comment/close/history operations
Planning owners: verified Backlog and OpenSpec roots, source revisions
Product authority: approved Backlog outcome, scope, priority, exclusions
Semantic authority: current OpenSpec change and question references
Permitted effects: existing authorization for owner writes and commits
Return condition: finish bounded activity, return control to SpeciFlow
```

## Owner mapping

Use one Beads graph:

| Wayfinder concept | Native owner |
| --- | --- |
| Map (parent/index) | Beads issue with `wayfinder:map` label |
| Destination, Notes, Out of scope | Summarize and link Backlog intent (not scope authority) |
| Fog | Unresolved OpenSpec questions |
| Sharp questions / prerequisites | Beads child issues with `wayfinder:<type>` labels |
| Decisions-so-far | Links to child issues and their OpenSpec results |
| Execution state (readiness, claim, completion) | Beads native fields |
| Semantic answers and rationale | OpenSpec context |

## Operations

### Creating the map and children

1. Create the map issue first.
2. Create all children, then wire blockers.
3. Suppress parent label inheritance (`--no-inherit-labels`).
4. Verify each child's parent, type label, source reference, and
   absence of `wayfinder:map` before continuing.
5. Use returned native IDs only.

### Before every write

1. Verify the canonical Beads root.
2. Preview the exact payload and effects.
3. Apply semantic review and existing authorization.
4. Inspect the native result after execution.
5. Include Dolt commits and audit records (`.beads/interactions.jsonl`)
   in the preview and post-check.

### Selecting next work

SpeciFlow selects the next cross-owner action from current native state:

1. Honor a child the human names.
2. If only a map is named, query child records, blockers, readiness,
   and claims.
3. Present ready candidates as:
   `[<title>](<link>) — <OpenSpec question reference>`
4. Equal candidates → present all and let the human choose.

### Agent roles

- **Primary agent**: performs all owner writes, claims work atomically.
- **Research agent**: applies `research` method, returns cited evidence
  only — no claims, comments, closes, or map updates.

## Completion

Resolve by the child's bounded acceptance:

| Child type | Completes when |
| --- | --- |
| Research or prerequisite only | Accepted evidence linked; semantic answer stays pending |
| Promises a semantic decision | Human supplies answer + rationale, recorded and verified in OpenSpec |

After the selected activity, return control to SpeciFlow.

## Fresh session resume

A new session requires a fresh human Wayfinder invocation:

1. Re-resolve source and closure.
2. Verify all owner roots and revisions.
3. Query native map, children, dependencies, readiness, claims,
   comments, close state, history, commits, and audits.
4. Follow owner references and verify current revisions.
5. Handovers and transcripts are pointers to inspect, not state to
   restore.

## Restrictions

If the user forbids Beads writes: continue only reads, original-method
discussion, or independently authorized OpenSpec edits. Name the
unavailable capability precisely.
