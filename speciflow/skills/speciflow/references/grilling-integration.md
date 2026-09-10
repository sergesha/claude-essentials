# Original grilling integration

Use the original `mattpocock/skills` methods as an external supporting
discipline. SpeciFlow selects the bounded activity and its native owner; the
original method supplies the interview, domain, research, or prototype
practice. It does not become an owner or a SpeciFlow implementation.

## Selection

Select by the requested activity and the subject's owner, not by a matching
word. A product concept named `Export`, for example, stays a product or design
question unless the user requests the SpeciFlow diagnostic export operation.

| Need in the selected activity | Original reusable method |
| --- | --- |
| Clarify or stress-test a bounded plan, decision, or idea | `grilling` |
| Challenge domain terms, scenarios, or decisions against code and project language | `domain-modeling`, with `grilling` when an interview is also needed |
| Resolve a blocking factual question from primary sources | `research` |
| Give the user a concrete UI or logic artifact to judge | `prototype` |

Start a short check by narrowing the selected question. Deepen when the user
asks, when an answer exposes unresolved dependent decisions, when project
sources conflict with the proposed meaning, or when a missing fact or concrete
experience prevents a responsible decision. Preserve the agreed scope and end
when that bounded question and its dependent branches are resolved; do not set
a fixed number of questions or rounds.

`grill-me`, `grill-with-docs`, `wayfinder`, and
`setup-matt-pocock-skills` are user-only upstream entrypoints. Only the human
may invoke them. Never call one from SpeciFlow or reproduce a hidden invocation
by reading and executing its instructions. A prior direct invocation continues
within the same unchanged authorized activity.

## Actual source and closure resolution

Resolve the applicable installed skill to the original
[`mattpocock/skills`](https://github.com/mattpocock/skills) source and record
its qualified host identity in the invocation context. Record its actual
revision when the host exposes one; otherwise record revision metadata as
unavailable. Missing Git or version metadata does not invalidate a successfully
resolved and completely read original skill. Names alone are insufficient,
especially for `research`. Inspect the resolved entrypoint and every linked
file or invoked reusable skill in its current closure before use. Hold that
actual loaded closure stable for the bounded activity.

Invoke the resolved original method through the host's native skill interface;
calling an ordinary question “grilling” is not loading the method. In Codex,
successful local discovery followed by a complete read of the resolved
`SKILL.md` is native loading and application; a separate tool literally named
`Skill` is not required. This does not relax user-only entrypoints: reading one
never counts as the human invoking it. Use the method's current instructions
directly. Do not copy its question algorithm, templates, prototype branches,
or frontier logic into SpeciFlow.

## Invocation context

Pass these instructions in memory when invoking a reusable original method:

```text
Selected activity and question:
Approved scope and authoritative source revisions:
Established decisions and unresolved prerequisites:
Permitted effects and existing authorization:
Applicable original upstream entrypoint and dependencies:
Return condition and destination owner:
```

Fill the first two slots with the concrete product subject and owner source,
not a keyword-derived SpeciFlow operation. Fill the effects slot from observed
closure effects and the authorization already in force. These slots are agent
context for one invocation, not a user questionnaire, file, schema, queue, or
stored SpeciFlow state.

At the start of a new bounded interview, completely read the selected available
authoritative owner snapshot for that activity before asking questions. Use its
current scope and facts, together with immutable facts supplied directly in the
invocation and exact prior authorization, to populate these slots as established
context. Read it again when the selected source or revision changes.

## Composition with Superpowers

For design work, `superpowers:brainstorming` owns the surrounding design
discipline while the selected original `grilling` method leads the interview.
Within that bounded interview, preserve the loaded original method's dependency
ordering and default round composition; brainstorming must not silently
serialize independently open decisions or invent a dependency between their
outcomes and possible implementation mechanisms. Keep already established
outcomes distinct from unresolved implementation detail. Follow the original
method's question and recommendation format. An explicit user preference for
another question format takes precedence. Facts available from authorized
sources are investigated rather than delegated back to the user; decisions
remain with the user.

When the return condition is met and the user confirms shared understanding,
return the result to SpeciFlow. SpeciFlow selects any owner action. Apply the
Superpowers skill triggered by that selected implementation, review, or
verification activity before carrying it out.

## Effects and return

The interview itself authorizes no mutation. Resolve the actual closure before
invocation because `domain-modeling`, `research`, and `prototype` may entail
delegation, an artifact write, a branch, a commit, or product-source edits.
Put each proposed effect into the exact preview required by
[operations.md](operations.md), then apply its existing review and approval
rules. Existing authorization remains valid for an unchanged covered effect.

Definitions, design answers, and their rationale return to the current
OpenSpec context. Read existing glossary, context, ADR, and code sources and
identify their project-defined role; do not make a copy canonical merely by
writing it. A product-scope proposal returns to Backlog for approval. Research
findings and prototype links go only into authorized native artifacts as
evidence or inputs to the owner decision.

Agreement on meaning, semantic review, and authorization for the exact
mutation are separate. Research delegation or artifact capture, prototype
branch/commit capture, transfer into production, external publication, and a
new owner write are separate effects. Permission to interview, research, or
prototype does not grant the later effects.

Return established decisions with rationale, unresolved prerequisites,
evidence and prototype links, and proposed owner changes. Apply
[transitions.md](transitions.md) whenever a result crosses owners. The method's
result does not itself approve, apply, close, claim, publish, or commit
anything.

## Missing or incompatible capability

If original provenance is conflicting or unqualified, applicable content is
incomplete, the host cannot apply the loaded method, or a required effect
cannot be resolved, report the exact capability as missing or incompatible and
stop that dependent method. An unavailable revision field or absent literal
`Skill` tool is not such a failure when the host has qualified and completely
loaded the original method. Continue any unaffected owner-native work within
scope. Do not substitute a same-named skill, vendor upstream source, invent an
invocation, install or configure a host, or create runtime or state.
Installation remains a separate explicit user-requested activity through
[installation.md](installation.md).
