---
name: speciflow
description: Use when a user needs to coordinate across Backlog.md, OpenSpec, Beads/Dolt, Superpowers, and Grill — turning ideas into tracked specs, projecting specs into tasks, checking status, or resolving design decisions.
disable-model-invocation: true
---

# SpeciFlow

## Absolute boundary

**SpeciFlow coordinates Backlog.md, OpenSpec, Beads/Dolt, and Product Git/CI
through their native interfaces. Superpowers and Grill (the original
`mattpocock/skills` family) are supporting disciplines for selected work.**

**SpeciFlow owns no process or state.** Use native owners through their native
interfaces; do not create or assume a `speciflow` command. Native owner edits
remain authoritative. Supporting disciplines never acquire owner authority.
Never copy owner content into a SpeciFlow schema.

## Quick start

SpeciFlow coordinates your installed planning and execution tools through
their native interfaces. Start with what you have:

| What you have | What SpeciFlow can do |
| --- | --- |
| Nothing installed yet | Show a setup outline, help install tools one at a time |
| Backlog.md | Track product intent, capture requirements, manage priority |
| Backlog.md + OpenSpec | Add spec authoring, requirement refinement, design review |
| All three + Beads/Dolt | Full cross-owner flow: spec → decomposition → executable graph |
| + Superpowers | TDD, code review, verification, implementation discipline |
| + Grill | Design interviews, domain modeling, research, prototyping |

Invoke SpeciFlow when you need to coordinate across these tools — for
example, turning a product idea into a tracked spec, or projecting an
approved design into implementation tasks. Ask for `status` to see what
is available and what is missing.

## Required reading

This file is the entry point, not the operating procedure. Before answering or
acting, read [references/ownership.md](references/ownership.md) and every applicable row below
completely. The routes are additive; an init request that includes a state
report also needs the reporting route.

| Request or activity | Required references |
| --- | --- |
| New idea, feature request, or rough plan that needs refinement | [references/iterative-planning.md](references/iterative-planning.md), [references/grilling-integration.md](references/grilling-integration.md), [references/operations.md](references/operations.md) |
| Setup or initialization | [references/storage.md](references/storage.md), [references/operations.md](references/operations.md), [references/initialization.md](references/initialization.md) |
| Storage selection | [references/storage.md](references/storage.md) |
| Native operations or any proposed mutation | [references/operations.md](references/operations.md) |
| Consistency check, alignment, "is everything on track?" | [references/doctor.md](references/doctor.md), [references/diagnostics.md](references/diagnostics.md) |
| Status, next, views, or export | [references/operations.md](references/operations.md), [references/diagnostics.md](references/diagnostics.md), [references/transitions.md](references/transitions.md) |
| Cross-owner refinement, projection, application, promotion, archive, closure, or any state report | [references/transitions.md](references/transitions.md), [references/doctor.md](references/doctor.md) |
| Clarification, design interview, ambiguity, domain terms, research, or prototype selection | [references/grilling-integration.md](references/grilling-integration.md) |
| Human-invoked original Wayfinder chart, resolve, or fresh resume | [references/grilling-integration.md](references/grilling-integration.md), [references/wayfinding.md](references/wayfinding.md), [references/operations.md](references/operations.md), [references/transitions.md](references/transitions.md) |
| User-requested host installation | [references/installation.md](references/installation.md) |
| Original grilling-family installation, availability, or update compatibility | [references/installation.md](references/installation.md), [references/grilling-integration.md](references/grilling-integration.md) |

For worked examples of these routes in natural SDLC scenarios, see
[references/examples.md](references/examples.md).

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

## Supporting disciplines

SpeciFlow coordinates five components. Three are product owners (Backlog,
OpenSpec, Beads) and two are supporting disciplines:

- **Superpowers**: TDD, debugging, verification, implementation review.
  Apply by native trigger before the bounded selected activity.
- **Grill** (mattpocock/skills family): interviews, domain modeling,
  research, prototyping. Apply when the activity involves clarification
  or design exploration.

Both install and invoke through the host's native skill interface — same
pattern. Each shapes the bounded activity, then returns control to SpeciFlow.
SpeciFlow retains cross-owner selection.

Architectural or creative planning uses `superpowers:brainstorming`; follow
its selected path including `superpowers:writing-plans`. Any SpeciFlow edit
uses `superpowers:writing-skills`.

When clarification, domain work, research, or prototyping applies, use
[references/grilling-integration.md](references/grilling-integration.md).

Wayfinder is a human-invoked entrypoint. After explicit invocation, apply
[references/wayfinding.md](references/wayfinding.md) for its native owner
binding.
