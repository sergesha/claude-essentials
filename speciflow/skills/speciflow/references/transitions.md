# Cross-owner transitions

Use this contract whenever state owned by one native owner is refined,
projected, applied, promoted, archived, closed, or reported through another.

## Intent preservation

Before proposing the target mutation, compare the authoritative upstream
artifact with the proposed downstream result read-only and in memory. Identify
the inspected source revision or the user's approved dirty snapshot, the target
owner and transition, every applicable approved outcome, requirement,
constraint, exclusion, and unresolved dependency, and the executable
downstream path that retains each item. Persist none of this comparison.

A downstream artifact may refine approved intent with implementation detail,
but must not narrow or replace an approved outcome, omit required work because
it is blocked, turn a dependency or intermediate milestone into the terminal
result, hide an unresolved dependency, or introduce an exclusion, deferral, or
substitution without approval from the owner of the upstream intent.

Proceed only when every applicable item is retained. Otherwise stop and report
the authoritative source, target transition, and exact unmatched, weakened,
deferred, substituted, or prematurely terminated intent. Do not silently edit,
reinterpret, or repair another owner's artifact.

## Native lifecycle fidelity

Before proposing a mutation, inspect the installed owner's version, help or
instructions, bound native root, current artifact state, and available
lifecycle operations. Installed documentation wins over remembered syntax and
examples in this skill.

Classify the requested owner-state effect before selecting its mechanism. A
change that promotes or applies pending content into canonical state is a
lifecycle transition regardless of its filesystem mechanism or label. Use the
owner's documented operation for every lifecycle transition it owns, including
create, update, validate, apply, archive, close, claim, and commit when
available. Direct artifact editing is allowed only when the installed owner
documents those artifacts as its editing interface and the change stays within
the artifact's current lifecycle state. If the required lifecycle-transition
operation is missing, incompatible, or unavailable, report the capability as
unsupported or broken instead of simulating it with file copy, direct or
generic editing, a guessed status, or another tool.

After execution, inspect the native owner state again. One owner's validation,
approval, commit, or completion never implies another owner's approval,
archive, claim, closure, acceptance, or completion.

## Supporting-tool containment

First identify the concern's sole semantic owner through
[ownership.md](ownership.md). Permission or approval to store or copy
supporting material is separate from semantic authority: a copy remains
non-authoritative regardless of copy approval and cannot be named final,
canonical, or approved for a concern owned elsewhere. Only that owner can
incorporate and approve it through its documented interface and lifecycle. One
tool's verdict never supplies another owner's transition.
