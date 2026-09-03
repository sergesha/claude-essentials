# Ownership

Each concern has one authoritative owner:

| Owner | Sole authority |
| --- | --- |
| Backlog.md | Product intent and priority |
| OpenSpec | Proposals, requirements, design, and specification tasks |
| Beads/Dolt | Executable graph, dependencies, readiness, claim, blockers, and completion |
| Superpowers | TDD, debugging, verification, review, and implementation discipline |
| Product Git and CI | Source changes, review history, dirty state, and implementation evidence |

Backlog.md solely owns product intent and priority.

OpenSpec solely owns proposals, requirements, design, and specification tasks.

Beads/Dolt solely owns the executable graph, dependencies, readiness, claim, blockers, and completion.

Superpowers solely owns TDD, debugging, verification, review, and implementation discipline.

Product Git and CI own implementation evidence; code evidence is never Beads
status or OpenSpec design approval.

Native tool edits remain authoritative. Never copy owner content into a SpeciFlow schema.
Use native IDs or typed references when available; otherwise use an artifact
path plus commit SHA, or the current user's approval of a dirty snapshot.
Missing stable links are manual confirmed operations, not SpeciFlow IDs.

## Approval boundary

Inspect native state, propose exactly one concrete next action, explain its effects, and wait for explicit user approval before any mutation.

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

## Superpowers containment

SpeciFlow selects the cross-tool action. Superpowers may apply only the bounded
implementation discipline needed for already selected work. Superpowers must return control to SpeciFlow after the bounded leaf execution step.
It reports observed effects. Superpowers must not select cross-tool actions or the next Bead.
It must not acquire Backlog, OpenSpec, Beads, Git, or Dolt ownership.

## SpeciFlow does not own domain state

SpeciFlow must not define phases, statuses, tasks, readiness, assignments, a second graph, queues, or cursors. It must not replace an owner, duplicate an owner model, or create another source of truth.
