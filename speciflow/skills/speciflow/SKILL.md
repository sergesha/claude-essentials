---
name: speciflow
description: Use when a user explicitly asks to coordinate work across Backlog.md, OpenSpec, Beads/Dolt, and Superpowers.
disable-model-invocation: true
---

# SpeciFlow

## Absolute boundary

**SpeciFlow is only a skill around Backlog.md, OpenSpec, Beads/Dolt, and Superpowers.**

**SpeciFlow owns no process or state.** Use the four tools through their native interfaces; do not create or assume a `speciflow` command. Native tool edits remain authoritative. Never copy their content into a SpeciFlow schema.

## Composition contract

Read [references/ownership.md](references/ownership.md) before coordinating the
tools. It assigns each concern and closure fact to a native owner.

For a product initiative, use Backlog framing tasks, OpenSpec detailed
specifications and design-time tasks, and Beads implementation issues at their
respective granularity. Apply the conditional artifact guidance in ownership;
an installed tool does not require work to be invented for it.

Before any cross-owner refinement, projection, application, promotion,
archive, closure, or state report, read
[references/transitions.md](references/transitions.md). Complete its ephemeral
intent-preservation check before proposing the target mutation; unmatched
approved intent is a stop, not a downstream repair opportunity.

For storage selection or explicit initialization requests, read [references/storage.md](references/storage.md).

For those requests, use the private storage helper at its resolved sibling path, [scripts/storage.py](scripts/storage.py). It is not a public CLI. Invoke it only while this skill has been explicitly invoked: `storage.py resolve PROJECT [--base BASE]` and, after approval, `storage.py init PROJECT [--base BASE]`. Use its compact JSON only for storage preparation, then continue to selected native-owner initialization; storage preparation is not a terminal lifecycle.

For operational requests across the four tools, read [references/operations.md](references/operations.md).
It contains fixed native root selection, planning-Git/Beads-Dolt separation,
commit guardrails, and the optional pinned OpenSpec-to-Beads preview flow.

For status, next, on-demand visualization, or export requests, read [references/diagnostics.md](references/diagnostics.md).

For a user-requested host installation, read [references/installation.md](references/installation.md). It provides one Codex path and one Claude path; SpeciFlow ships no installer, CLI, runtime, or daemon.

For every proposed mutation:

1. Inspect native state.
2. Propose exactly one concrete next action and explain its effects.
3. Check explicit user authorization, including applicable earlier instructions.
   If it covers this action, proceed; otherwise wait for approval.

Before every semantic Backlog, OpenSpec, or Beads mutation, and any related
planning or executable coordination mutation, state
`Review: required|skipped — reason`. Review is required for a semantic
mutation; skip it only for demonstrably read-only or mechanical work and name
the reason. Follow the review, root binding, exact-preview approval, and
projection rules in
[references/operations.md](references/operations.md).

If a required native tool or integration is missing, report it and offer one safe loading or installation action. A component with implicit triggers must be invocation-scoped and explicit-only. For `openspec-to-beads`, offer only an invocation-scoped explicit-only load with automatic and proactive activation disabled; never offer unqualified installation. Wait for explicit approval before loading or installing it.

SpeciFlow selects the cross-tool action. Apply every applicable Superpowers
skill by its native trigger before the selected activity; architectural or
creative planning uses `superpowers:brainstorming`, approved designs use
`superpowers:writing-plans`, and any SpeciFlow edit uses
`superpowers:writing-skills`. Superpowers may apply its discipline only to the
bounded selected activity, then must return control to SpeciFlow.

Do not define SpeciFlow phases, statuses, tasks, readiness, assignments, a second graph, queues, or cursors. Do not let Superpowers select cross-tool actions or the next Bead.
