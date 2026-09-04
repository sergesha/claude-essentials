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
