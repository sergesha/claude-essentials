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
acting, read [references/ownership.md](references/ownership.md) and every applicable row below
completely. The routes are additive; an init request that includes a state
report also needs the reporting route.

| Request or activity | Required references |
| --- | --- |
| Setup or initialization | [references/storage.md](references/storage.md), [references/operations.md](references/operations.md), [references/initialization.md](references/initialization.md) |
| Storage selection | [references/storage.md](references/storage.md) |
| Native operations or any proposed mutation | [references/operations.md](references/operations.md) |
| Status, next, views, or export | [references/operations.md](references/operations.md), [references/diagnostics.md](references/diagnostics.md), [references/transitions.md](references/transitions.md) |
| Cross-owner refinement, projection, application, promotion, archive, closure, or any state report | [references/transitions.md](references/transitions.md) |
| User-requested host installation | [references/installation.md](references/installation.md) |

Creating Beads issues from OpenSpec remains a cross-owner projection when
its exact preview is already approved; that continuation still reads
[references/transitions.md](references/transitions.md).

Resolve each link relative to the file containing it, using its literal target.
Before answering or acting, determine each required file's availability from
the actual read output in this invocation, not a handover's historical status:

1. Complete successful current read, with the returned content rather than
   just a success code or claim: apply that file; its reading requirement
   is satisfied even if the handover reports an earlier failure.
2. Missing file or truncated current output: correct the path from its literal
   link or this bundle's listing, or read the remaining content, then evaluate
   the actual new output.
3. Still unavailable after the attempted recovery: report the missing
   instruction and stop the dependent action or report. The fallback reply
   identifies what could not be completed and may repeat directly supplied
   observations as facts. Do not derive owner selections, health results or
   next-action recommendations from other references, including in a partial
   status table. Unrelated activities are unaffected.

Example: earlier `skill/ownership.md` returned `No such file or directory`;
current `references/ownership.md` was read completely. Result: the instruction
is available and must be applied, not reported missing.
A plausible answer from another reference does not replace an unread file.
This check needs no user approval or persisted checklist.

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
