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

For storage selection or explicit initialization requests, read [references/storage.md](references/storage.md).

For those requests, use the private storage helper at its resolved sibling path, [scripts/storage.py](scripts/storage.py). It is not a public CLI. Invoke this helper only while this skill has been explicitly invoked; send one UTF-8 JSON request for `resolve`, `preview`, or `init`, and use its one JSON result only to support this skill's storage guidance.

For operational requests across the four tools, read [references/operations.md](references/operations.md).
It contains fixed native root selection, planning-Git/Beads-Dolt separation,
commit guardrails, and the optional pinned OpenSpec-to-Beads preview flow.

For status, next, on-demand visualization, or export requests, read [references/diagnostics.md](references/diagnostics.md).

For a user-requested host installation, read [references/installation.md](references/installation.md). It provides one Codex path and one Claude path; SpeciFlow ships no installer, CLI, runtime, or daemon.

For every proposed mutation:

1. Inspect native state.
2. Propose exactly one concrete next action and explain its effects.
3. Wait for explicit user approval before mutating anything.

If a required native tool or integration is missing, report it and offer one safe loading or installation action. A component with implicit triggers must be invocation-scoped and explicit-only. For `openspec-to-beads`, offer only an invocation-scoped explicit-only load with automatic and proactive activation disabled; never offer unqualified installation. Wait for explicit approval before loading or installing it.

SpeciFlow selects the cross-tool action. Superpowers may apply implementation discipline only as a bounded leaf step for work already selected by SpeciFlow; afterward, it must return control to SpeciFlow.

Do not define SpeciFlow phases, statuses, tasks, readiness, assignments, a second graph, queues, or cursors. Do not let Superpowers select cross-tool actions or the next Bead.
