---
name: speciflow
description: Use when a user explicitly asks to coordinate work across Backlog.md, OpenSpec, Beads/Dolt, and Superpowers.
disable-model-invocation: true
---

# SpeciFlow

## Absolute boundary

**SpeciFlow is only a skill around Backlog.md, OpenSpec, Beads/Dolt, and Superpowers.**

**SpeciFlow owns no process or state.** Use the four tools through their native interfaces; do not create or assume a `speciflow` command. Native tool edits remain authoritative. Never copy their content into a SpeciFlow schema.

## Required reading

This file is the entry point, not the operating procedure. Before answering or
acting, read [ownership](references/ownership.md) and every applicable row below
completely. The routes are additive; an init request that includes a state
report also needs the reporting route. If a read is truncated, finish that
file before using it.

| Request or activity | Required references |
| --- | --- |
| Setup or initialization | [storage](references/storage.md), [operations](references/operations.md), [initialization](references/initialization.md) |
| Storage selection | [storage](references/storage.md) |
| Native operations or any proposed mutation | [operations](references/operations.md) |
| Status, next, views, or export | [operations](references/operations.md), [diagnostics](references/diagnostics.md) |
| Cross-owner refinement, projection, application, promotion, archive, closure, or any state report | [transitions](references/transitions.md) |
| User-requested host installation | [installation](references/installation.md) |

Ownership defines task granularity and approval authority; operations defines
root binding, exact previews, effect-based review, native commits, and bounded
projection. Initialization supplies the setup preview; diagnostics supplies
requested live reports and views.
Apply those procedures, including already granted authorization, rather than
deriving a workflow from this routing table.

For storage requests, resolve the sibling private helper
[scripts/storage.py](scripts/storage.py) and use it only during explicit skill
invocation, following storage.md. It prepares collision metadata, not a native
owner; continue through the selected owners' setup outline.

## Missing tools

If a required native tool or integration is missing, report it. When the user
forbids installation or repair, finish with the findings and confirmation that
nothing was changed; installation is at most a future option, not a current
approval question. Otherwise offer one safe loading or installation action and
wait for explicit approval before performing it. A component with implicit
triggers must be invocation-scoped and explicit-only. For `openspec-to-beads`,
offer only an invocation-scoped explicit-only load with automatic and proactive
activation disabled; never offer unqualified installation.

## Superpowers boundary

SpeciFlow selects the cross-tool action. Apply every applicable Superpowers
skill by its native trigger before the selected activity; architectural or
creative planning uses `superpowers:brainstorming`, approved designs use
`superpowers:writing-plans`, and any SpeciFlow edit uses
`superpowers:writing-skills`. Superpowers may apply its discipline only to the
bounded selected activity, then must return control to SpeciFlow.

Do not define SpeciFlow phases, statuses, tasks, readiness, assignments, a second graph, queues, or cursors. Do not let Superpowers select cross-tool actions or the next Bead.
