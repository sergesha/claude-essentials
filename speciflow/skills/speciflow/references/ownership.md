# Ownership

Each concern has one authoritative owner:

| Owner | Sole authority |
| --- | --- |
| Backlog.md | Product intent and priority |
| OpenSpec | Proposals, requirements, design, and specification tasks |
| Beads/Dolt | Executable graph, dependencies, readiness, claim, blockers, and completion |
| Superpowers | TDD, debugging, verification, review, and implementation discipline |
| Product Git and CI | Source changes, review history, dirty state, and implementation evidence |

Product Git and CI own implementation evidence; code evidence is never Beads
status or OpenSpec design approval.

## Native artifacts and granularity

Use selected tools to the extent the work needs:

| Concern | Native representation |
| --- | --- |
| Committed product initiative | A Backlog task expressing the outcome, scope, product acceptance criteria, and priority. Link an existing brief for detail. |
| Specification and technical refinement | An OpenSpec change with requirements, scenarios, design, and, when authorized, its native design-time task checklist. |
| Concrete implementation | Beads issues with bounded work, verification, executable dependencies, and references to the approved OpenSpec source. |

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

## Approved refinement

Every cross-owner result must remain a refinement of the applicable approved
upstream intent. Provenance identifies the source but does not prove complete
coverage. The downstream owner may add detail within its authority, but may not
narrow an outcome, discard required work at a blocker or intermediate
milestone, or conceal an unresolved dependency.

Only the owner of the upstream intent may approve an exclusion, deferral, or
substitution. Validation, review, downstream acceptance, and implementation
evidence do not supply that approval. Use the ephemeral comparison in
[transitions.md](transitions.md); never persist a coverage table or SpeciFlow
traceability record.

## Review authority

Backlog.md is the sole scope authority. A blocking defect is only a conflict
with approved scope or applicable native methodology. A reviewer must return
new capabilities, actors, services, protocols, platforms, dependencies,
security boundaries, success criteria, requirements, or tasks as non-blocking
scope proposals; it may not promote them into blockers or executable work.
They enter an owner artifact only after an explicitly approved and reviewed
Backlog scope mutation.

## Approval boundary

Inspect native state, preview one concrete owner action and its effects, and
check explicit user authorization before mutation. Applicable earlier approval
and user instructions approving intermediate actions remain effective; do not
ask again for an action they cover. If authority or scope is missing, ask.

OpenSpec proposal or projection requires current explicit user approval by
default. A durable approval is valid only through a documented native signal,
or a user-named attestation that the user confirms authorizes projection.
Validation, files, review activity, and an ordinary Git commit never implies approval.

When Beads is selected, OpenSpec `tasks.md` is a design-time checklist and
projection index, not live executable status, assignment, claim, or completion;
Beads owns those facts. Without Beads, OpenSpec may use its native task
workflow. A requirements, contract, security, or material architecture change
stops execution for renewed OpenSpec or user approval.

Closure is separate by owner: Beads completion, OpenSpec verification or
archive, Backlog outcome acceptance, and product Git/CI evidence are separate
facts. Ask closure questions only for selected owners; an absent owner is N/A.
Backlog task status reflects product acceptance, not mirrored Beads progress.

## Superpowers coordination

SpeciFlow selects the cross-tool action. Apply applicable Superpowers skills by
their native triggers before the bounded selected activity, then return control
to SpeciFlow and report observed effects. Superpowers must not select
cross-tool actions or the next Bead.
It must not acquire Backlog, OpenSpec, Beads, Git, or Dolt ownership.

## SpeciFlow does not own domain state

SpeciFlow must not define phases, statuses, tasks, readiness, assignments, a second graph, queues, or cursors. It must not replace an owner, duplicate an owner model, or create another source of truth.
