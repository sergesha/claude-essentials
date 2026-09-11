# Cross-owner transitions

Use this contract whenever state owned by one native owner is refined,
projected, applied, promoted, archived, closed, or reported through another.

## Intent preservation

Before proposing the target mutation, compare the authoritative upstream
artifact with the proposed downstream result read-only and in memory. Identify
the inspected source revision or the user's approved dirty snapshot, the target
owner and transition, every applicable approved outcome, requirement,
constraint, exclusion, and unresolved dependency, and the target-owned
refinement that retains each item at the authorized granularity. Require an
executable downstream path when projecting executable work. For product
framing or specification work whose implementation is explicitly deferred,
retain the outcome, prerequisite, and resumption condition without inventing
execution issues. Persist none of this comparison.

A downstream artifact may refine approved intent with implementation detail,
but must not narrow or replace an approved outcome, omit required work because
it is blocked, turn a dependency or intermediate milestone into the terminal
result, hide an unresolved dependency, or introduce an exclusion, deferral, or
substitution without approval from the owner of the upstream intent
(Backlog owns product intent; OpenSpec owns specification intent).

Proceed only when every applicable item is retained. Otherwise stop and report
the authoritative source, target transition, and exact unmatched, weakened,
deferred, substituted, or prematurely terminated intent. Do not silently edit,
reinterpret, or repair another owner's artifact.

## Native lifecycle fidelity

Before proposing a mutation:

1. **Inspect** the installed owner's version, help, native root, current
   artifact state, and available lifecycle operations. Installed docs win
   over remembered syntax.
2. **Classify** the effect: a change that promotes pending content into
   canonical state is a lifecycle transition (create, apply, archive,
   close, claim, commit) regardless of its filesystem mechanism.
3. **Select mechanism**:
   - Lifecycle transitions → use the owner's documented lifecycle operation.
   - In-state edits → use the documented editing interface.
   - Missing lifecycle operation → report as unsupported, stop.
4. **Verify** native owner state after execution. Each owner's transitions
   are independent — one owner's completion does not imply another's.

## Supporting-tool containment

First identify the concern's sole semantic owner (Backlog owns product
intent; OpenSpec owns specification; Beads owns execution; Superpowers owns
implementation discipline; Git/CI owns source evidence). Permission or
approval to store or copy
supporting material is separate from semantic authority: a copy remains
non-authoritative regardless of copy approval and cannot be named final,
canonical, or approved for a concern owned elsewhere. Only that owner can
incorporate and approve it through its documented interface and lifecycle. One
tool's verdict never supplies another owner's transition.

Treat interview decisions, research findings, prototype artifacts, branches,
and commits as supporting results. Return definitions, design answers, and
rationale to the current OpenSpec context; return product-scope proposals to
Backlog; keep execution state in Beads and source evidence in Product Git/CI.
Persist only authorized owner-native artifacts and links. The result of an
original method is not approval for transfer into production, external
publication, a new owner write, or any lifecycle transition.

For human-invoked Wayfinder, apply [wayfinding.md](wayfinding.md). A child
scoped only to bounded research or prerequisite work may complete in Beads when
its own acceptance is met, while its proposed semantic answer remains pending.
Any child that promises to resolve a semantic decision, regardless of label,
does not resolve until the user-provided answer and rationale have been recorded
and verified in the current OpenSpec context under the applicable authorization.
The Beads comment and map entry link that owner result; they do not copy it or
approve it. Beads close, OpenSpec acceptance, map update, Backlog scope change,
prototype promotion, and Product Git evidence remain separate effects.
